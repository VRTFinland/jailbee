"""Grant-controlled publication: durable acceptance, final policy and settlement."""

import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from jailbee import issue_outbox, pr_outbox
from jailbee.orchestration import labels, publishing
from jailbee.orchestration.actions import dispatch
from jailbee.orchestration.grant import Grant, PublishRights
from jailbee.orchestration.protocol import PublishManifest
from jailbee.outbox_io import ContainerIdentity, JournalStore
from tests.test_orchestration_actions import _ctx, _node


@pytest.fixture
def rig(tmp_path, mocker):
    ctx = _ctx(
        tmp_path,
        _node(
            "proj-o", **{labels.GRANT: Grant(publish=PublishRights(issues=("comment",))).to_label()}
        ),
    )
    files = {
        "a.json": json.dumps(
            {
                "version": 1,
                "actions": [{"type": "comment", "repo": ".", "issue": 12, "body": "hello"}],
            }
        )
    }
    mocker.patch.object(
        issue_outbox,
        "read_issue_outbox",
        side_effect=lambda *a, **kw: issue_outbox.OutboxSnapshot(files),
    )
    mocker.patch.object(
        publishing, "JournalStore", return_value=JournalStore(tmp_path / "journals")
    )
    mocker.patch.object(
        publishing, "container_identity", return_value=ContainerIdentity("proj-o", "created")
    )
    mocker.patch.object(
        issue_outbox,
        "resolve_repo_targets",
        return_value={".": issue_outbox.RepoTarget(".", tmp_path, "org/repo")},
    )
    threads = []

    class Thread:
        def __init__(self, *, target, args, daemon):
            self.target, self.args = target, args

        def start(self):
            threads.append(self)

    mocker.patch.object(publishing.threading, "Thread", Thread)
    publish = mocker.patch.object(
        publishing,
        "publish_selected",
        side_effect=lambda *a, **kw: (
            kw["result_sink"]("https://github.com/org/repo/issues/12#issuecomment-1") or 0
        ),
    )
    m = PublishManifest(version=1, id="pub-1", type="publish", outbox="issue", manifest="a.json")
    return ctx, m, files, threads, publish


def accept(rig):
    ctx, m, _, threads, _ = rig
    assert publishing.handle_publish(ctx, m).state == "started"
    assert threads == []  # Not before the broker journals/replies/deletes.
    publishing.start(ctx, m)
    assert len(threads) == 1
    threads[0].target(*threads[0].args)


def test_publish_started_then_done_with_urls(rig):
    accept(rig)
    replies = []
    publishing.settle(rig[0], lambda ref, out: replies.append((ref, out)))
    assert replies[0][0] == "pub-1"
    assert replies[0][1].state == "done"
    assert "#issuecomment-1" in replies[0][1].detail
    publishing.settle(rig[0], lambda *a: pytest.fail("duplicate result"))


def test_uncovered_issue_action_refused_whole(rig):
    ctx, m, files, threads, publish = rig
    files["a.json"] = (
        files["a.json"]
        .replace('"comment"', '"create"')
        .replace('"issue": 12,', '"ref": "new", "title": "title",')
    )
    out = publishing.handle_publish(ctx, m)
    assert out.state == "refused" and "create" in out.detail
    assert not threads
    publish.assert_not_called()


def test_foreign_repo_needs_foreign_right(rig):
    ctx, m, files, *_ = rig
    files["a.json"] = files["a.json"].replace('"repo": "."', '"repo": "github:foreign/lib"')
    out = publishing.handle_publish(ctx, m)
    assert out.state == "refused" and "foreign/lib" in out.detail


def test_in_lock_policy_refusal_reported_failed(rig):
    ctx, _m, _, _, publish = rig

    def execute(*args, **kw):
        from jailbee.issue_manifest import ExistingIssue, StateAction

        action = Mock(
            status="pending",
            action=StateAction(".", ExistingIssue(12), "closed", "completed", "open"),
            repo=issue_outbox.RepoTarget(".", ctx.root, "org/repo"),
        )
        kw["policy"](Mock(manifests=[Mock(actions=[action])]))

    publish.side_effect = execute
    accept(rig)
    replies = []
    publishing.settle(ctx, lambda ref, out: replies.append(out))
    assert replies[0].state == "failed" and "state" in replies[0].detail


def test_second_publish_while_inflight_refused(rig):
    ctx, m, *_ = rig
    publishing.handle_publish(ctx, m)
    before = (ctx.root / "publish-budget.json").read_text()
    out = publishing.handle_publish(ctx, m.model_copy(update={"id": "pub-2"}))
    assert out.state == "refused" and "already running" in out.detail
    assert (ctx.root / "publish-budget.json").read_text() == before


def test_rate_cap_refuses_and_reserves_nothing(rig):
    ctx, m, *_ = rig
    (ctx.root / "publish-budget.json").write_text(
        json.dumps({ctx.sender.full: [ctx.now.isoformat()] * 30})
    )
    before = (ctx.root / "publish-budget.json").read_text()
    assert publishing.handle_publish(ctx, m).state == "refused"
    assert (ctx.root / "publish-budget.json").read_text() == before


def test_rate_budget_reserved_and_not_refunded_on_failure(rig):
    rig[4].side_effect = RuntimeError("network failed")
    accept(rig)
    publishing.settle(rig[0], lambda *a: None)
    assert len(json.loads((rig[0].root / "publish-budget.json").read_text())["proj-o"]) == 1


def test_recover_turns_inflight_into_uncertain_failure_once(rig):
    ctx, m, *_ = rig
    publishing.handle_publish(ctx, m)
    publishing.recover(ctx.root)
    publishing.recover(ctx.root)
    replies = []
    publishing.settle(ctx, lambda ref, out: replies.append(out))
    publishing.recover(ctx.root)
    publishing.settle(ctx, lambda *a: pytest.fail("duplicate"))
    assert len(replies) == 1
    assert replies[0].state == "failed" and "outcome uncertain" in replies[0].detail
    rig[4].assert_not_called()


def test_in_lock_growth_cannot_exceed_reserved_budget(rig):
    from jailbee.issue_manifest import CommentAction, ExistingIssue

    ctx = rig[0]

    def execute(*args, **kw):
        action = Mock(
            status="pending",
            action=CommentAction(".", ExistingIssue(12), "hello"),
            repo=issue_outbox.RepoTarget(".", ctx.root, "org/repo"),
        )
        kw["policy"](Mock(manifests=[Mock(actions=[action, action])]))

    rig[4].side_effect = execute
    accept(rig)
    replies = []
    publishing.settle(ctx, lambda ref, out: replies.append(out))
    assert replies[0].state == "failed" and "budget" in replies[0].detail


def test_review_policy_refuses_submodule_repository(rig, mocker):
    from jailbee.orchestration.publishing import _review_policy

    ctx = rig[0]
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/org/repo.git")
    manifest = pr_outbox.parse_manifest(
        "a.json",
        json.dumps(
            {
                "version": 1,
                "repo": "org/submodule",
                "pr": 12,
                "head_sha": "a" * 40,
                "actions": [{"type": "comment", "body": "x"}],
            }
        ),
        {},
    )
    with pytest.raises(pr_outbox.GateError, match="own repository"):
        _review_policy(PublishRights(pr_reviews=True), 1, ctx.cfg, ctx.incus, ctx.sender.full)(
            manifest
        )


def test_review_policy_requires_explicit_binding_not_discovery(rig, mocker):
    ctx = rig[0]
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/org/repo.git")
    ctx.incus.config_get_checked.return_value = None
    manifest = pr_outbox.parse_manifest("a.json", json.dumps({"version": 1, "repo": "org/repo", "pr": 12, "head_sha": "a" * 40, "actions": [{"type": "comment", "body": "x"}]}), {})
    with pytest.raises(pr_outbox.GateError, match="bound"):
        publishing._review_policy(PublishRights(pr_reviews=True), 1, ctx.cfg, ctx.incus, ctx.sender.full)(manifest)


def test_rate_budget_prunes_expired_reservations(rig):
    ctx, m, *_ = rig
    (ctx.root / "publish-budget.json").write_text(
        json.dumps({ctx.sender.full: [(ctx.now - timedelta(hours=2)).isoformat()] * 30})
    )
    assert publishing.handle_publish(ctx, m).state == "started"
    assert json.loads((ctx.root / "publish-budget.json").read_text())[ctx.sender.full] == [
        ctx.now.isoformat()
    ]


def test_settlement_delivery_failure_preserves_result(rig):
    accept(rig)

    def fail(*args):
        raise RuntimeError("delivery failed")

    with pytest.raises(RuntimeError):
        publishing.settle(rig[0], fail)
    replies = []
    publishing.settle(rig[0], lambda ref, out: replies.append(out))
    assert len(replies) == 1


def test_settle_appends_audit_line(rig):
    accept(rig)
    publishing.settle(rig[0], lambda *a: None)
    rows = [
        json.loads(line) for line in (rig[0].root / "publish-log.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["orchestrator"] == "proj-o"
    assert rows[0]["ref"] == "pub-1" and rows[0]["outcome"] == "done"
    assert rows[0]["actions"] == [{"kind": "comment", "repo": "org/repo"}]
    assert rows[0]["urls"] == ["https://github.com/org/repo/issues/12#issuecomment-1"]


@pytest.mark.parametrize("revoke", [True, False])
def test_revoked_or_cleared_grant_still_settles_inflight(rig, revoke):
    accept(rig)
    ctx = rig[0]
    ctx.sender = replace(ctx.sender, config={} if revoke else {labels.GRANT: Grant().to_label()})
    replies = []
    publishing.settle(ctx, lambda ref, out: replies.append(out))
    assert replies[0].state == "done"
    out = dispatch(ctx, rig[1].model_copy(update={"id": "pub-2"}))
    assert out.state == "refused"


@pytest.mark.parametrize("source", [True, False])
def test_pr_worker_uses_safe_domain_options(rig, mocker, source):
    ctx, m, *_ = rig
    worker_incus = Mock()
    worker_incus.config_get_checked.side_effect = lambda full, key: "12" if key == "user.jailbee.pr" else None
    mocker.patch.object(publishing, "Incus", return_value=worker_incus)
    rights = PublishRights(pull_requests=source, pr_reviews=not source)
    ctx.sender = replace(ctx.sender, config={labels.GRANT: Grant(publish=rights).to_label()})
    manifest = {
        "version": 1,
        "repo": "org/repo",
        "pr": None if source else 12,
        "head_sha": None if source else "a" * 40,
        "actions": [{"type": "description", "body": "B"}]
        if source
        else [{"type": "comment", "body": "x"}],
    }
    if source:
        manifest |= {"source": "refs/heads/topic", "source_sha": "a" * 40, "base": "main"}
    mocker.patch.object(
        pr_outbox,
        "read_outbox",
        return_value=pr_outbox.Outbox(
            {"a.json": json.dumps(manifest)}, identity=ContainerIdentity("proj-o", "created")
        ),
    )
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/org/repo.git")

    def publish(*args, **kw):
        assert kw["confirm"]("Publish foreign PR?") is False if source else kw["confirm"](1) is True
        if source:
            assert kw["force"] is False and kw["own_branches_only"] is True
        else:
            assert kw["options"].foreign is False and kw["options"].draft is False
            kw["policy"](pr_outbox.parse_manifest("a.json", json.dumps(manifest), {}))
        kw["result_sink"]("opened PR #12: https://github.com/org/repo/pull/12")
        return 0

    if source:
        mocker.patch.object(publishing.branch_pr, "publish_source_manifest", side_effect=publish)
    else:
        rig[4].side_effect = publish
    m = m.model_copy(update={"outbox": "pr"})
    accept((ctx, m, *rig[2:]))
    replies = []
    publishing.settle(ctx, lambda ref, out: replies.append(out))
    assert replies[0].state == "done" and "/pull/12" in replies[0].detail


@pytest.mark.parametrize("source", [True, False])
def test_pr_manifest_needs_matching_right(rig, mocker, source):
    ctx, m, *_ = rig
    manifest = {
        "version": 1,
        "repo": "org/repo",
        "pr": None if source else 12,
        "head_sha": None if source else "a" * 40,
        "actions": [{"type": "description", "title": "T", "body": "B"}]
        if source
        else [{"type": "comment", "body": "x"}],
    }
    if source:
        manifest |= {"source": "refs/heads/topic", "source_sha": "a" * 40, "base": "main"}
    mocker.patch.object(
        pr_outbox,
        "read_outbox",
        return_value=pr_outbox.Outbox(
            {"a.json": json.dumps(manifest)}, identity=ContainerIdentity("proj-o", "created")
        ),
    )
    out = publishing.handle_publish(ctx, m.model_copy(update={"outbox": "pr"}))
    assert out.state == "refused"
    assert ("pull_requests" if source else "pr_reviews") in out.detail
