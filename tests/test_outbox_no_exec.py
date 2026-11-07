"""No host outbox path runs ``incus exec``: every entry point, issue and PR, reads
and writes the host outbox store (`broker.db`) only, with the instance found by
``incus list``. The container may be stopped.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from jailbee import branch_pr, issue_github, issue_outbox, pr, pr_flow, pr_outbox
from jailbee.incus import Incus
from jailbee.outbox import service
from jailbee.outbox.delete import DeleteSelection, plan_delete
from jailbee.outbox.models import ProposalId
from jailbee.outbox.publish import PublishOptions, publish_selected
from jailbee.outbox_io import proposal_digest
from jailbee.pr_flow import PrScope
from tests.test_pr_outbox_store import (
    COMMENT,
    CREATED,
    DESCRIPTION,
    FULL,
    REPO,
    UUID,
    manifest,
    stage,
)

EXEC_METHODS = sorted(name for name in vars(Incus) if name.startswith("exec"))


@pytest.fixture
def incus(mocker):
    """An Incus whose every ``exec*`` fails the test."""
    fake = mocker.Mock(spec=Incus)
    fake.list_containers.return_value = [
        {
            "name": FULL,
            "status": "Stopped",
            "created_at": CREATED,
            "config": {"volatile.uuid": UUID},
        }
    ]
    fake.config_get.return_value = None
    for name in EXEC_METHODS:
        getattr(fake, name).side_effect = AssertionError(f"incus.{name} on a host outbox path")
    return fake


@pytest.fixture
def cfg(make_cfg, tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture(autouse=True)
def github(mocker, cfg):
    info = pr.PrInfo(12, "topic", "a" * 40, "OPEN", "main")

    def target(_cfg, _incus, _container, parsed, **_kw):
        return pr_outbox.Target(parsed, info, False, PrScope.for_repo(cfg))

    mocker.patch.object(pr_outbox, "resolve_target", side_effect=target)
    mocker.patch.object(pr_outbox, "scope_slug", return_value=REPO)
    mocker.patch.object(pr, "gh_login", return_value="me")
    mocker.patch.object(pr, "add_issue_comment", return_value="https://github.com/x/pull/12#c")
    targets = {".": issue_outbox.RepoTarget(".", cfg.repo_root, REPO)}
    mocker.patch.object(issue_outbox, "resolve_repo_targets", return_value=targets)
    mocker.patch.object(issue_github, "current_login", return_value="me")
    mocker.patch.object(issue_github, "list_labels", return_value={})
    snapshot = issue_github.IssueSnapshot(
        7, "Title", "Body", (), "open", f"https://github.com/{REPO}/issues/7", False
    )
    mocker.patch.object(issue_github, "get_issue", return_value=snapshot)
    mocker.patch.object(
        issue_github,
        "add_comment",
        return_value=issue_github.MutationReceipt(7, f"https://github.com/{REPO}/issues/7#c"),
    )


def stage_issue(name: str = "001.json") -> str:
    from tests.issue_store_support import seed

    text = json.dumps(
        {"version": 1, "actions": [{"type": "comment", "repo": ".", "issue": 7, "body": "Hi"}]}
    )
    seed({name: text}, uuid=UUID)
    return name


def _review_apply(cfg: Any, incus: Any) -> object:
    name = stage(manifest(COMMENT))
    return pr_outbox.offer_pending_comments(
        cfg,
        incus,
        FULL,
        "c1",
        pr_number=None,
        confirm=lambda _n: True,
        manifest_names=(name,),
        publisher="user",
    )


def _offer_after_pr(cfg: Any, incus: Any) -> object:
    stage(manifest(COMMENT))
    return pr_outbox.offer_pending_comments(
        cfg, incus, FULL, "c1", pr_number=12, confirm=lambda _n: True, publisher="user"
    )


def _outbox_publish(cfg: Any, incus: Any) -> object:
    name = stage(manifest(COMMENT))
    return publish_selected(
        cfg,
        incus,
        FULL,
        ProposalId("pr", name),
        options=PublishOptions(publisher="user"),
        confirm=lambda _n: True,
    )


def _drop(cfg: Any, incus: Any) -> object:
    name = stage(manifest(COMMENT))
    outbox = pr_outbox.read_outbox(incus, FULL, uid=1000)
    return pr_outbox.drop_manifest(incus, FULL, outbox, name, uid=1000)


def _outbox_view_and_delete(cfg: Any, incus: Any) -> object:
    name = stage(manifest(COMMENT))
    view = service.load_container(cfg, incus, FULL)
    plan = plan_delete(view, ProposalId("pr", name), DeleteSelection())
    return service.execute_delete(cfg, incus, FULL, plan)


def _record_consumed(cfg: Any, incus: Any) -> object:
    name = stage(manifest(DESCRIPTION, pr_number=None))
    source = pr_outbox.pending_pr_text(
        cfg, incus, FULL, scope=PrScope.for_repo(cfg), source_branch="topic", uid=1000
    )
    pr_flow.validate_outbox_source(cfg, incus, FULL, source)
    pr_flow.record_outbox_consumption(cfg, incus, FULL, source, "https://github.com/x/pull/30")
    assert pr_outbox.read_outbox(incus, FULL, uid=1000).manifest_names == []
    return name


def _references(cfg: Any, incus: Any) -> object:
    stage(manifest(COMMENT))
    return pr_outbox.outbox_pr_references(incus, FULL, uid=1000, repo=REPO, exclude=set())


def _pending_sources(cfg: Any, incus: Any) -> object:
    source = manifest(DESCRIPTION, pr_number=None)
    source.update(source="refs/heads/topic", source_sha="b" * 40)
    stage(source)
    return branch_pr.pending_source_manifests(cfg, incus, FULL)


def _resolve(cfg: Any, incus: Any) -> object:
    from jailbee.outbox import publication_db, store_db

    name = stage(manifest(COMMENT))
    (row,) = store_db.read_proposals(publication_db.default_root(), UUID, "pr")
    digest = proposal_digest(row.name, row.manifest, row.bodies)
    pubs = publication_db.open_publications()
    with pubs.claim(UUID, "pr", name) as claim:
        pubs.begin(claim, digest=digest, action_count=1, container_full=FULL, now=1.0)
        pubs.mark_prepared(claim, digest, 0, repo=REPO, now=1.0)
    removed = pr_outbox.resolve_uncertain(incus, FULL, name, 0, landed_url="https://github.com/x/1")
    return f"removed={removed}"


def _issue_apply(cfg: Any, incus: Any) -> object:
    name = stage_issue()
    batch = issue_outbox.prepare_batch(cfg, incus, FULL, [name])
    issue_outbox.revalidate_batch(batch)
    report = issue_outbox.apply_batch(
        batch, incus=incus, container_prefix=cfg.container_prefix, publisher="user"
    )
    assert not issue_outbox.store_outbox(UUID).manifest_names
    return report


def _outbox_publish_issue(cfg: Any, incus: Any) -> object:
    name = stage_issue()
    failures = publish_selected(
        cfg,
        incus,
        FULL,
        ProposalId("issue", name),
        options=PublishOptions(publisher="user"),
        confirm=lambda _n: True,
    )
    assert not issue_outbox.store_outbox(UUID).manifest_names
    return f"failures={failures}"


def _outbox_view_and_delete_issue(cfg: Any, incus: Any) -> object:
    name = stage_issue()
    view = service.load_container(cfg, incus, FULL)
    plan = plan_delete(view, ProposalId("issue", name), DeleteSelection())
    return service.execute_delete(cfg, incus, FULL, plan)


def _child_outbox_count(cfg: Any, incus: Any) -> object:
    from jailbee.orchestration import child_outbox

    stage_issue()
    stage(manifest(COMMENT))
    pending = child_outbox.probe(UUID)
    assert pending.count == 2
    return pending


def _orchestrator_publish_issue(cfg: Any, incus: Any) -> object:
    """`jailbee inbox send publish`: the broker's precheck, then its worker."""
    from types import SimpleNamespace
    from unittest import mock

    from jailbee.orchestration import publishing
    from jailbee.orchestration.grant import PublishRights
    from jailbee.outbox import publication_db

    name = stage_issue()
    rights = PublishRights(issues=("comment",))
    ctx = SimpleNamespace(cfg=cfg, sender=SimpleNamespace(key=UUID, full=FULL, short="c1"))
    request = SimpleNamespace(outbox="issue", manifest=name)
    kind, actions, foreign = publishing._precheck(ctx, request, rights)  # type: ignore[arg-type]  # duck-typed context
    assert actions, "nothing to publish"
    entry = {
        "ref": "r1",
        "container_key": UUID,
        "outbox": "issue",
        "manifest": name,
        "actions": actions,
        "kind": kind,
        "foreign": foreign,
        "rights": rights.model_dump(mode="json"),
    }
    root = publication_db.default_root()
    with mock.patch.object(publishing, "Incus", return_value=incus):
        publishing._run(cfg, root, FULL, "c1", entry)
    result = publishing._get_result(root, UUID, "r1")
    assert result is not None and result["state"] == "done", result
    assert not issue_outbox.store_outbox(UUID).manifest_names
    return result


def _outbox_clear(cfg: Any, incus: Any) -> object:
    from unittest import mock

    from jailbee.outbox.commands import clear_selected

    stage_issue()
    stage(manifest(COMMENT))
    # The scoped inventory: the container carries its repo's base profile.
    (listed,) = incus.list_containers.return_value
    listed["profiles"] = [f"{cfg.container_prefix}-base"]
    with mock.patch("jailbee.config.load_repo_config", return_value=cfg):
        status = clear_selected(cfg, incus, [FULL], confirm=lambda _plans: True)
    assert status == 0
    assert not issue_outbox.store_outbox(UUID).manifest_names
    assert pr_outbox.read_outbox(incus, FULL, uid=1000).manifest_names == []
    return f"status={status}"


ENTRY_POINTS: dict[str, Callable[[Any, Any], object]] = {
    "jb review apply": _review_apply,
    "jb pr offer after push": _offer_after_pr,
    "jb outbox apply": _outbox_publish,
    "jb review drop": _drop,
    "jb outbox ls/drop": _outbox_view_and_delete,
    "jb pr description from the outbox": _record_consumed,
    "jb pr outbox references": _references,
    "jb pr push source list": _pending_sources,
    "jb review resolve": _resolve,
    "jb issue apply": _issue_apply,
    "jb outbox apply (issue)": _outbox_publish_issue,
    "jb outbox ls/drop (issue)": _outbox_view_and_delete_issue,
    "orchestrator stop/destroy/finish outbox count": _child_outbox_count,
    "orchestrator send publish (issue)": _orchestrator_publish_issue,
    "jb outbox clear": _outbox_clear,
}


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_no_incus_exec_on_a_host_outbox_path(cfg, incus, entry):
    result = ENTRY_POINTS[entry](cfg, incus)
    assert result not in (1, None, [], {}), result  # the path ran to its end
    for name in EXEC_METHODS:
        getattr(incus, name).assert_not_called()


def test_the_guard_covers_every_exec_method():
    assert {"exec", "exec_pipe", "exec_with_input", "exec_bytes", "exec_capped"} <= set(
        EXEC_METHODS
    )
