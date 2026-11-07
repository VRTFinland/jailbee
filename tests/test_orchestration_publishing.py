"""Grant-controlled publication: durable acceptance, final policy and settlement."""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from jailbee import branch_pr, issue_outbox, pr_outbox
from jailbee.orchestration import labels, publishing
from jailbee.orchestration.actions import dispatch
from jailbee.orchestration.grant import Grant, PublishRights
from jailbee.orchestration.protocol import PublishManifest
from jailbee.orchestration.store import open_state_store
from jailbee.outbox_io import ContainerIdentity
from tests.test_orchestration_actions import _ctx, _node


@pytest.fixture
def rig(tmp_path, mocker):
    ctx = _ctx(
        tmp_path,
        _node(
            "proj-o", **{labels.GRANT: Grant(publish=PublishRights(issues=("comment",))).to_label()}
        ),
    )
    ctx.incus.config_get.side_effect = lambda full, key: ctx.sender.config.get(key)
    files = {
        "a.json": json.dumps(
            {
                "version": 1,
                "actions": [{"type": "comment", "repo": ".", "issue": 12, "body": "hello"}],
            }
        )
    }
    # The precheck reads the sender's proposals from the host outbox store by its key.
    mocker.patch.object(
        issue_outbox, "store_outbox", side_effect=lambda uuid: issue_outbox.OutboxSnapshot(files)
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
            kw["audit_sink"]({"kind": "comment", "repo": "org/repo"})
            or kw["result_sink"]("https://github.com/org/repo/issues/12#issuecomment-1")
            or 0
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


@pytest.mark.parametrize(
    "name,document",
    [
        ("publish-inflight.json", {"proj-o": {"ref": "old"}}),
        ("publish-known.json", {"proj-o": "key"}),
        ("publish-accepted.json", {"key": ["old"]}),
        ("publish-budget.json", {"proj-o": ["2026-11-06T12:00:00+00:00"]}),
    ],
)
def test_documents_import_once_and_quarantine_corrupt_state(rig, caplog, name, document):
    ctx = rig[0]
    legacy = ctx.root / name
    legacy.write_text(json.dumps(document))
    assert publishing._load(ctx.root, name) == document
    assert legacy.with_name(name + ".migrated").exists()
    legacy.write_text(json.dumps({"unexpected": "value"}))
    assert publishing._load(ctx.root, name) == document
    publishing._save(ctx.root, name, {"new": "value"})
    assert publishing._load(ctx.root, name) == {"new": "value"}
    assert not legacy.exists()

    other = ctx.root / "other"
    other.mkdir()
    (other / name).write_text("broken")
    assert publishing._load(other, name) == {}
    assert (other / (name + ".corrupt")).exists()
    assert "corrupt" in caplog.text


def test_known_senders_imports_once_and_quarantines_corrupt_state(rig, caplog):
    ctx = rig[0]
    legacy = ctx.root / "publish-known.json"
    legacy.write_text(json.dumps({"proj-old": "old-key"}))
    assert publishing.known_senders(ctx.root) == {"proj-old"}
    assert legacy.with_name(legacy.name + ".migrated").exists()
    legacy.write_text(json.dumps({"unexpected": "key"}))
    assert publishing.known_senders(ctx.root) == {"proj-old"}
    publishing.remember(ctx)
    assert publishing.known_senders(ctx.root) == {"proj-old", ctx.sender.full}
    assert not legacy.exists()

    other = ctx.root / "other"
    other.mkdir()
    (other / "publish-known.json").write_text("broken")
    assert publishing.known_senders(other) == set()
    assert (other / "publish-known.json.corrupt").exists()
    assert "corrupt" in caplog.text


def test_legacy_results_import_and_quarantine(rig, caplog):
    ctx, m, *_ = rig
    assert publishing.handle_publish(ctx, m).state == "started"
    digest = hashlib.sha256(ctx.sender.key.encode()).hexdigest()
    path = ctx.root / "publish-results" / f"{digest}-{m.id}.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"state": "done", "detail": "legacy"}))
    (path.parent / "bad.json").write_text("broken")
    replies = []
    publishing.settle(ctx, lambda ref, out: replies.append(out.detail))
    assert replies == ["legacy"]
    assert (ctx.root / "publish-results.migrated" / "bad.json.corrupt").exists()
    assert "corrupt" in caplog.text
    assert not path.parent.exists()


def test_malformed_legacy_result_recovers_as_uncertain(rig, caplog):
    ctx, m, *_ = rig
    assert publishing.handle_publish(ctx, m).state == "started"
    digest = hashlib.sha256(ctx.sender.key.encode()).hexdigest()
    path = ctx.root / "publish-results" / f"{digest}-{m.id}.json"
    path.parent.mkdir()
    path.write_text("{}")
    publishing.recover(ctx.root)
    replies = []
    publishing.settle(ctx, lambda ref, out: replies.append(out))
    assert len(replies) == 1 and "uncertain" in replies[0].detail
    assert (ctx.root / "publish-results.migrated" / (path.name + ".corrupt")).exists()
    assert "corrupt" in caplog.text


def test_handle_publish_rereads_revoked_label(rig):
    ctx, m, *_ = rig
    ctx.incus.config_get.side_effect = None
    ctx.incus.config_get.return_value = None
    out = publishing.handle_publish(ctx, m)
    assert out.state == "refused"
    assert not (ctx.root / "publish-budget.json").exists()


def test_uncertain_uncovered_action_is_checked(rig):
    from types import SimpleNamespace

    from jailbee.issue_manifest import ExistingIssue, StateAction
    from jailbee.issue_outbox import ResolvedAction, ResolvedIssue

    action = ResolvedAction(
        0,
        StateAction(".", ExistingIssue(12), "closed", "completed", "open"),
        issue_outbox.RepoTarget(".", rig[0].root, "org/repo"),
        ResolvedIssue(12),
        "uncertain",
    )
    batch = SimpleNamespace(manifests=(SimpleNamespace(actions=(action,)),))
    with pytest.raises(issue_outbox.IssueGateError, match="state"):
        publishing._issue_policy(PublishRights(issues=("comment",)), 1)(batch)


@pytest.mark.parametrize(
    "binding,description,allowed",
    [
        ("label", True, False),
        ("adopted", True, False),
        ("authored", True, True),
        ("label", False, True),
    ],
)
def test_review_description_requires_authored_binding(rig, mocker, binding, description, allowed):
    ctx = rig[0]
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/org/repo.git")
    entries = {
        "topic": {
            "head": "topic",
            "pr": 12,
            "base": "main",
            "source": "refs/heads/topic",
            "source_sha": "a" * 40,
            "author": True,
            "adopted": binding == "adopted",
        }
    }
    ctx.incus.config_get_checked.side_effect = lambda full, key: (
        json.dumps(entries)
        if key == branch_pr.BRANCH_PRS_LABEL and binding != "label"
        else "12"
        if key == "user.jailbee.pr"
        else None
    )
    payload = {
        "version": 1,
        "repo": "org/repo",
        "pr": 12,
        "head_sha": "a" * 40,
        "actions": [{"type": "description" if description else "comment", "body": "body"}],
    }
    manifest = pr_outbox.parse_manifest("a.json", json.dumps(payload), {})
    check = publishing._review_policy(
        PublishRights(pr_reviews=True), 1, ctx.cfg, ctx.incus, ctx.sender.full
    )
    if allowed:
        check(manifest)
    else:
        with pytest.raises(pr_outbox.GateError, match="authored"):
            check(manifest)


def test_recovered_uncertain_review_blocks_new_request(rig, mocker):
    ctx, m, *_ = rig
    m = m.model_copy(update={"outbox": "pr"})
    mocker.patch.object(
        publishing,
        "_precheck",
        return_value=("review", [{"kind": "comment", "repo": "org/repo"}], False),
    )
    assert publishing.handle_publish(ctx, m).state == "started"
    publishing.recover(ctx.root)
    publishing.settle(ctx, lambda *args: None)
    out = publishing.handle_publish(ctx, m.model_copy(update={"id": "pub-2"}))
    assert out.state == "refused" and "uncertain" in out.detail


def test_cut_worker_recovery_reports_uncertain(rig):
    ctx, m, *_ = rig
    assert publishing.handle_publish(ctx, m).state == "started"
    # A joined-but-still-running worker has not written any result at exit.
    publishing.recover(ctx.root)
    replies = []
    publishing.settle(ctx, lambda ref, out: replies.append(out))
    assert len(replies) == 1 and replies[0].state == "failed"
    assert "outcome uncertain" in replies[0].detail


def test_settle_without_prune_refuses_namesake(rig):
    accept(rig)
    ctx = rig[0]
    ctx.sender = replace(ctx.sender, config={"volatile.uuid": "new-uuid"})
    publishing.settle(ctx, lambda *args: pytest.fail("old result delivered to namesake"))


def test_abandoned_worker_late_result_is_audited(rig):
    ctx, m, _, threads, _ = rig
    publishing.handle_publish(ctx, m)
    publishing.start(ctx, m)
    publishing.prune(ctx.root, {}, ctx.now)
    threads[0].target(*threads[0].args)
    publishing.prune(ctx.root, {}, ctx.now)
    rows = [json.loads(line) for line in (ctx.root / "publish-log.jsonl").read_text().splitlines()]
    assert any(row["outcome"] == "abandoned-result" for row in rows)
    assert not list((ctx.root / "publish-results").glob("*.json"))


def test_worker_transport_refuses_namesake_before_sidecar_write(mocker):
    import subprocess

    from jailbee.incus import Incus, IncusError

    transport = mocker.patch(
        "jailbee.incus.subprocess.run",
        return_value=subprocess.CompletedProcess([], 0, stdout="new-uuid\n", stderr=""),
    )
    incus = Incus()
    incus.guard_instance("proj-o", "old-uuid")
    with pytest.raises(IncusError, match="identity"):
        incus.exec_with_input("proj-o", ["bash", "-c", "write sidecar"], "receipt")
    assert all("exec" not in call.args[0] for call in transport.call_args_list)


def test_audit_uses_actual_domain_actions_not_precheck(rig):
    ctx = rig[0]

    def publish(*args, **kw):
        kw["audit_sink"]({"kind": "comment", "repo": "org/actual"})
        kw["result_sink"]("https://github.com/org/actual/issues/12")
        return 0

    rig[4].side_effect = publish
    accept(rig)
    publishing.settle(ctx, lambda *args: None)
    row = json.loads((ctx.root / "publish-log.jsonl").read_text())
    assert row["actions"] == [{"kind": "comment", "repo": "org/actual"}]


def test_settlement_audits_before_delivery(rig):
    accept(rig)

    def reply(ref, out):
        assert (rig[0].root / "publish-log.jsonl").exists()

    publishing.settle(rig[0], reply)


def test_rate_refusal_is_audited(rig):
    ctx, m, *_ = rig
    (ctx.root / "publish-budget.json").write_text(
        json.dumps({ctx.sender.full: [ctx.now.isoformat()] * 30})
    )
    assert publishing.handle_publish(ctx, m).state == "refused"
    row = json.loads((ctx.root / "publish-log.jsonl").read_text())
    assert row["outcome"] == "refused" and "rate" in row["detail"]


def test_vanished_publish_is_abandoned_not_delivered_to_namesake(rig):
    accept(rig)
    ctx = rig[0]
    publishing.prune(ctx.root, {ctx.sender.full: "new-uuid"}, ctx.now)
    assert publishing.inflight(ctx.root) == {}
    assert publishing.known_senders(ctx.root) == set()
    row = json.loads((ctx.root / "publish-log.jsonl").read_text())
    assert row["outcome"] == "abandoned"
    publishing.settle(ctx, lambda *a: pytest.fail("old result delivered"))


def test_publish_started_then_done_with_urls(rig):
    receipts = [{"index": 0, "kind": "create", "issue": 42, "url": "https://example.test/42"}]

    def publish(*args, **kwargs):
        kwargs["receipt_sink"](receipts[0])
        kwargs["result_sink"]("https://github.com/org/repo/issues/12#issuecomment-1")
        return 0

    rig[4].side_effect = publish
    accept(rig)
    replies = []
    publishing.settle(rig[0], lambda ref, out: replies.append((ref, out)))
    assert replies[0][0] == "pub-1"
    assert replies[0][1].state == "done"
    assert "#issuecomment-1" in replies[0][1].detail
    assert replies[0][1].results == receipts
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
    before = json.dumps(open_state_store(ctx.root).kv_get("publish-budget.json"))
    out = publishing.handle_publish(ctx, m.model_copy(update={"id": "pub-2"}))
    assert out.state == "refused" and "already running" in out.detail
    assert json.dumps(open_state_store(ctx.root).kv_get("publish-budget.json")) == before


def test_rate_cap_refuses_and_reserves_nothing(rig):
    ctx, m, *_ = rig
    (ctx.root / "publish-budget.json").write_text(
        json.dumps({ctx.sender.full: [ctx.now.isoformat()] * 30})
    )
    before = {ctx.sender.full: [ctx.now.isoformat()] * 30}
    assert publishing.handle_publish(ctx, m).state == "refused"
    assert open_state_store(ctx.root).kv_get("publish-budget.json") == before


def test_rate_budget_reserved_and_not_refunded_on_failure(rig):
    rig[4].side_effect = RuntimeError("network failed")
    accept(rig)
    publishing.settle(rig[0], lambda *a: None)
    assert len(open_state_store(rig[0].root).kv_get("publish-budget.json")["proj-o"]) == 1


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
    manifest = pr_outbox.parse_manifest(
        "a.json",
        json.dumps(
            {
                "version": 1,
                "repo": "org/repo",
                "pr": 12,
                "head_sha": "a" * 40,
                "actions": [{"type": "comment", "body": "x"}],
            }
        ),
        {},
    )
    with pytest.raises(pr_outbox.GateError, match="bound"):
        publishing._review_policy(
            PublishRights(pr_reviews=True), 1, ctx.cfg, ctx.incus, ctx.sender.full
        )(manifest)


def test_rate_budget_prunes_expired_reservations(rig):
    ctx, m, *_ = rig
    (ctx.root / "publish-budget.json").write_text(
        json.dumps({ctx.sender.full: [(ctx.now - timedelta(hours=2)).isoformat()] * 30})
    )
    assert publishing.handle_publish(ctx, m).state == "started"
    assert open_state_store(ctx.root).kv_get("publish-budget.json")[ctx.sender.full] == [
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
    worker_incus.config_get_checked.side_effect = lambda full, key: (
        "12" if key == "user.jailbee.pr" else None
    )
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


def test_broker_publish_records_orchestrator(rig):
    accept(rig)
    publish = rig[4]
    assert publish.call_args.kwargs["options"].publisher == "orchestrator"


def test_broker_source_publish_records_orchestrator(rig, mocker):
    ctx, m, *_ = rig
    manifest = {
        "version": 1,
        "repo": "org/repo",
        "pr": None,
        "head_sha": None,
        "actions": [{"type": "description", "body": "B"}],
        "source": "refs/heads/topic",
        "source_sha": "a" * 40,
        "base": "main",
    }
    ctx.sender = replace(
        ctx.sender,
        config={labels.GRANT: Grant(publish=PublishRights(pull_requests=True)).to_label()},
    )
    mocker.patch.object(
        pr_outbox,
        "read_outbox",
        return_value=pr_outbox.Outbox(
            {"a.json": json.dumps(manifest)}, identity=ContainerIdentity("proj-o", "created")
        ),
    )
    mocker.patch("jailbee.git.get_remote_url", return_value="https://github.com/org/repo.git")
    source = mocker.patch.object(publishing.branch_pr, "publish_source_manifest", return_value=0)
    accept((ctx, m.model_copy(update={"outbox": "pr"}), *rig[2:]))
    assert source.call_args.kwargs["publisher"] == "orchestrator"


def _assign_action(rig, add=(), remove=()):

    from jailbee.issue_manifest import AssigneesAction, ExistingIssue
    from jailbee.issue_outbox import ResolvedAction, ResolvedIssue

    return ResolvedAction(
        0,
        AssigneesAction(".", ExistingIssue(12), tuple(add), tuple(remove), ()),
        issue_outbox.RepoTarget(".", rig[0].root, "org/repo"),
        ResolvedIssue(12),
        "pending",
    )


def _assign_batch(rig, add=(), remove=(), login="alice", extra=()):
    from types import SimpleNamespace

    actions = (*extra, _assign_action(rig, add, remove))
    return SimpleNamespace(login=login, manifests=(SimpleNamespace(actions=actions),))


def _check(rig, kinds, **kw):
    publishing._issue_policy(PublishRights(issues=kinds), 1)(_assign_batch(rig, **kw))


def test_assign_self_refuses_other_login(rig):
    with pytest.raises(issue_outbox.IssueGateError, match="assign"):
        _check(rig, ("assign_self",), add=("bob",))
    with pytest.raises(issue_outbox.IssueGateError, match="assign"):
        _check(rig, ("assign_self",), remove=("bob",))
    with pytest.raises(issue_outbox.IssueGateError, match="assign"):
        _check(rig, ("assign_self",), add=("@me",), remove=("bob",))


def test_assign_self_accepts_resolved_login(rig):
    _check(rig, ("assign_self",), add=("@me",))
    _check(rig, ("assign_self",), add=("Alice",))  # literal publisher login, any case
    _check(rig, ("assign_self",), remove=("alice",))
    _check(rig, ("assign_self",), add=("@me",), remove=())


def test_assign_grant_covers_self_and_other(rig):
    _check(rig, ("assign",), add=("@me",))
    _check(rig, ("assign",), add=("bob",), remove=("carol",))


def test_assign_self_with_unknown_login_covers_nothing(rig):
    with pytest.raises(issue_outbox.IssueGateError):
        _check(rig, ("assign_self",), add=("@me",), login="")


def _comment_action(rig):
    from jailbee.issue_manifest import CommentAction, ExistingIssue
    from jailbee.issue_outbox import ResolvedAction, ResolvedIssue

    return ResolvedAction(
        1,
        CommentAction(".", ExistingIssue(12), "hi"),
        issue_outbox.RepoTarget(".", rig[0].root, "org/repo"),
        ResolvedIssue(12),
        "pending",
    )


def test_issue_policy_refuses_uncovered_assign(rig):
    batch = _assign_batch(rig, add=("@me",), extra=(_comment_action(rig),))
    # the covered comment alone passes; the uncovered assign refuses the whole batch
    from types import SimpleNamespace

    only_comment = SimpleNamespace(
        login="alice", manifests=(SimpleNamespace(actions=(_comment_action(rig),)),)
    )
    publishing._issue_policy(PublishRights(issues=("comment",)), 2)(only_comment)
    with pytest.raises(issue_outbox.IssueGateError, match="assign"):
        publishing._issue_policy(PublishRights(issues=("comment", "state", "labels")), 2)(batch)
    with pytest.raises(issue_outbox.IssueGateError, match="assign"):
        _check(rig, (), add=("alice",))


def test_assign_self_grant_refuses_batch_mixing_self_and_other_actions(rig):
    from types import SimpleNamespace

    mine = _assign_action(rig, add=("@me",))
    theirs = _assign_action(rig, add=("bob",))
    batch = SimpleNamespace(login="alice", manifests=(SimpleNamespace(actions=(mine, theirs)),))
    rights = PublishRights(issues=("assign_self",))
    with pytest.raises(issue_outbox.IssueGateError, match="assign"):
        publishing._issue_policy(rights, 2)(batch)
    ok = SimpleNamespace(login="alice", manifests=(SimpleNamespace(actions=(mine,)),))
    publishing._issue_policy(rights, 2)(ok)


def test_needed_kind_distinguishes_self_other_and_empty(rig):
    def need(**kw):
        batch = _assign_batch(rig, **kw)
        return publishing._needed_kind(batch.manifests[0].actions[0].action, batch)

    assert need(add=("@me",)) == "assign_self"
    assert need(add=("ALICE",), remove=("alice",)) == "assign_self"
    assert need(add=("bob",)) == "assign"
    assert need(add=("@me",), remove=("bob",)) == "assign"
    assert need(add=("@me",), login="") == "assign"
    assert need() == "assign"  # nothing to compare fails closed


@pytest.mark.parametrize(
    "kinds,state",
    [(("assign_self",), "started"), (("assign",), "started"), (("comment",), "refused")],
)
def test_precheck_requires_some_assign_right(rig, kinds, state):
    ctx, m, files, *_ = rig
    files["a.json"] = json.dumps(
        {
            "version": 1,
            "actions": [
                {
                    "type": "assignees",
                    "repo": ".",
                    "issue": 12,
                    "add": ["@me"],
                    "expected": {"assignees": []},
                }
            ],
        }
    )
    ctx.sender.config[labels.GRANT] = Grant(publish=PublishRights(issues=kinds)).to_label()
    assert publishing.handle_publish(ctx, m).state == state


def _assign_manifest(files, add):
    files["a.json"] = json.dumps(
        {
            "version": 1,
            "actions": [
                {
                    "type": "assignees",
                    "repo": ".",
                    "issue": 12,
                    "add": add,
                    "expected": {"assignees": []},
                }
            ],
        }
    )


def test_refund_is_not_repeated_after_delivery_failure(rig):
    ctx, _m, files, _, publish = rig
    _assign_manifest(files, ["bob"])
    ctx.sender.config[labels.GRANT] = Grant(
        publish=PublishRights(issues=("assign_self",))
    ).to_label()
    publish.side_effect = lambda *a, **kw: kw["policy"](_assign_batch(rig, add=("bob",)))
    accept(rig)
    store = open_state_store(ctx.root)
    stamp = ctx.now.isoformat()
    store.kv_put("publish-budget.json", {ctx.sender.full: [stamp, stamp]})
    import sqlite3

    with sqlite3.connect(store.path) as con:
        con.execute(
            "CREATE TRIGGER fail_refund BEFORE UPDATE ON state "
            "WHEN NEW.key='publish-inflight.json' AND NEW.value LIKE '%refunded%' "
            "BEGIN SELECT RAISE(ABORT, 'interrupted'); END"
        )
    with pytest.raises(Exception, match="interrupted"):
        publishing.settle(ctx, lambda *args: None)
    assert store.kv_get("publish-budget.json") == {ctx.sender.full: [stamp, stamp]}
    with sqlite3.connect(store.path) as con:
        con.execute("DROP TRIGGER fail_refund")
    with pytest.raises(RuntimeError):
        publishing.settle(ctx, lambda *args: (_ for _ in ()).throw(RuntimeError("delivery")))
    publishing.settle(ctx, lambda *args: None)
    assert store.kv_get("publish-budget.json") == {ctx.sender.full: [stamp]}


def test_assign_self_refusal_in_the_lock_leaves_the_budget_unchanged(rig):
    ctx, _m, files, _, publish = rig
    _assign_manifest(files, ["bob"])
    ctx.sender.config[labels.GRANT] = Grant(
        publish=PublishRights(issues=("assign_self",))
    ).to_label()
    publish.side_effect = lambda *a, **kw: kw["policy"](_assign_batch(rig, add=("bob",)))
    accept(rig)
    replies = []
    publishing.settle(ctx, lambda ref, out: replies.append(out))
    assert replies[0].state == "failed" and "assign" in replies[0].detail
    budget = open_state_store(ctx.root).kv_get("publish-budget.json")
    assert budget.get(ctx.sender.full, []) == []


def test_publish_failure_after_the_policy_passed_keeps_the_reservation(rig):
    ctx, _m, files, _, publish = rig
    _assign_manifest(files, ["alice"])
    ctx.sender.config[labels.GRANT] = Grant(
        publish=PublishRights(issues=("assign_self",))
    ).to_label()

    def execute(*a, **kw):
        kw["policy"](_assign_batch(rig, add=("alice",)))  # passes
        raise RuntimeError("GitHub failed after the policy")

    publish.side_effect = execute
    accept(rig)
    replies = []
    publishing.settle(ctx, lambda ref, out: replies.append(out))
    assert replies[0].state == "failed" and "GitHub failed" in replies[0].detail
    assert len(open_state_store(ctx.root).kv_get("publish-budget.json")[ctx.sender.full]) == 1
