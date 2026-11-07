"""No host PR outbox path runs ``incus exec``: every entry point reads and writes
the host outbox store (`broker.db`) only, with the instance found by ``incus list``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from jailbee import branch_pr, pr, pr_flow, pr_outbox
from jailbee.incus import Incus
from jailbee.outbox import service
from jailbee.outbox.delete import DeleteSelection, plan_delete
from jailbee.outbox.models import ProposalId
from jailbee.outbox.publish import PublishOptions, publish_selected
from jailbee.outbox_io import JournalStore
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
        {"name": FULL, "created_at": CREATED, "config": {"volatile.uuid": UUID}}
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
        journal_store=JournalStore(cfg.repo_root / "journals"),
        options=PublishOptions(publisher="user"),
        confirm=lambda _n: True,
    )


def _drop(cfg: Any, incus: Any) -> object:
    name = stage(manifest(COMMENT))
    outbox = pr_outbox.read_outbox(incus, FULL, uid=1000)
    return pr_outbox.drop_manifest(incus, FULL, outbox, name, uid=1000)


def _outbox_view_and_delete(cfg: Any, incus: Any) -> object:
    name = stage(manifest(COMMENT))
    journals = JournalStore(cfg.repo_root / "journals")
    view = service.load_container(cfg, incus, FULL, journal_store=journals)
    plan = plan_delete(view, ProposalId("pr", name), DeleteSelection())
    return service.execute_delete(cfg, incus, FULL, plan, journal_store=journals)


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


ENTRY_POINTS: dict[str, Callable[[Any, Any], object]] = {
    "jb review apply": _review_apply,
    "jb pr offer after push": _offer_after_pr,
    "jb outbox apply": _outbox_publish,
    "jb review drop": _drop,
    "jb outbox ls/drop": _outbox_view_and_delete,
    "jb pr description from the outbox": _record_consumed,
    "jb pr outbox references": _references,
    "jb pr push source list": _pending_sources,
}


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
def test_no_incus_exec_on_a_host_pr_outbox_path(cfg, incus, entry):
    result = ENTRY_POINTS[entry](cfg, incus)
    assert result not in (1, None, [], {}), result  # the path ran to its end
    for name in EXEC_METHODS:
        getattr(incus, name).assert_not_called()


def test_the_guard_covers_every_exec_method():
    assert {"exec", "exec_pipe", "exec_with_input", "exec_bytes", "exec_capped"} <= set(
        EXEC_METHODS
    )
