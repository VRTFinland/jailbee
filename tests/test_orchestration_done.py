from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from jailbee.git import GitError
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.orchestration import labels, messages
from jailbee.orchestration.actions import (
    CHILD_EXEC_TIMEOUT_S,
    CHILD_TRANSPORT_TIMEOUT_S,
    ActionContext,
    Node,
    Outcome,
    done,
    refused,
)
from jailbee.orchestration.grant import Grant
from jailbee.orchestration.protocol import MessageManifest
from jailbee.sync import FetchResult, SyncError
from tests.conftest import make_config

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

SHA = "a" * 40
CHILD = "myrepo-fix-1"


class Rig:
    def __init__(self, ctx: ActionContext, mocker: MockerFixture) -> None:
        self.ctx = ctx
        self.incus: MagicMock = ctx.incus  # type: ignore[assignment]  # built as a MagicMock
        self.dirty = mocker.patch(
            "jailbee.orchestration.messages.sync._container_status_dirty", return_value=False
        )
        self.branch = mocker.patch(
            "jailbee.orchestration.messages.sync._container_current_branch",
            return_value="fix-1",
        )
        self.fetch = mocker.patch(
            "jailbee.orchestration.messages.sync.fetch_from_container",
            return_value=FetchResult(
                branch="fix-1", old_oid=None, new_oid=SHA, base_oid=None, commits_added=1
            ),
        )
        self.repo_dir = mocker.patch(
            "jailbee.orchestration.messages.lifecycle.container_repo_dir",
            return_value="/home/dev/repo",
        )
        self.deliver = mocker.patch(
            "jailbee.orchestration.messages.deliver", return_value="delivered"
        )


def _make(tmp_path: Path, mocker: MockerFixture, orch_running: bool = True) -> Rig:
    orch = Node("myrepo-orch", "orch", orch_running, {labels.GRANT: Grant().to_label()})
    sender = Node(CHILD, "fix-1", True, {labels.SPAWNED_BY: "myrepo-orch"})
    ctx = ActionContext(
        cfg=make_config(tmp_path, container_prefix="myrepo"),
        gcfg=GlobalConfig(),
        incus=MagicMock(),
        root=tmp_path,
        sender=sender,
        nodes={orch.full: orch, sender.full: sender},
        now=datetime(2026, 10, 25, tzinfo=UTC),
    )
    return Rig(ctx, mocker)


def _done(**kw: Any) -> MessageManifest:
    base: dict[str, Any] = {
        "version": 1,
        "id": "d1",
        "type": "message",
        "kind": "done",
        "body": "all finished",
    }
    base.update(kw)
    return MessageManifest.model_validate(base)


def _untouched(rig: Rig) -> None:
    rig.incus.config_set.assert_not_called()
    rig.deliver.assert_not_called()


def test_done_with_dirty_tree_bounced(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.dirty.return_value = True
    out = messages.handle_message(rig.ctx, _done())
    assert out == refused("uncommitted changes: commit everything before reporting done")
    rig.fetch.assert_not_called()
    _untouched(rig)


def test_done_dirty_probe_failure_refused(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.dirty.side_effect = SyncError("probe failed")
    assert messages.handle_message(rig.ctx, _done()) == refused("probe failed")
    rig.fetch.assert_not_called()
    _untouched(rig)


def test_done_repo_dir_failure_refused(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.repo_dir.side_effect = IncusError("gone")
    assert messages.handle_message(rig.ctx, _done()) == refused("gone")
    _untouched(rig)


def test_done_on_detached_head_bounced(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.branch.return_value = None
    out = messages.handle_message(rig.ctx, _done())
    assert out == refused("detached HEAD: check out your branch before reporting done")
    rig.fetch.assert_not_called()
    _untouched(rig)


def test_done_fetches_sets_labels_and_relays(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    out = messages.handle_message(rig.ctx, _done())
    rig.fetch.assert_called_once_with(
        rig.ctx.cfg,
        rig.incus.with_exec_timeout.return_value,
        "fix-1",
        branch="fix-1",
        tags="none",
        timeout=CHILD_TRANSPORT_TIMEOUT_S,
    )
    calls = [c.args for c in rig.incus.config_set.call_args_list]
    assert calls == [(CHILD, labels.DONE_SHA, SHA), (CHILD, labels.STATE, labels.STATE_DONE)]
    msg = rig.deliver.call_args.args[3]
    assert (msg.kind, msg.body, msg.from_) == ("done", "all finished", "fix-1")
    assert rig.deliver.call_args.kwargs["seq"] == 0
    assert out == done(f"done reported to orch at {'a' * 12}")


def test_done_while_orchestrator_stopped_still_records_and_queues(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker, orch_running=False)
    rig.deliver.return_value = "parked"
    out = messages.handle_message(rig.ctx, _done())
    assert rig.deliver.call_args.kwargs["running"] is False
    rig.incus.config_set.assert_any_call(CHILD, labels.DONE_SHA, SHA)
    rig.incus.config_set.assert_any_call(CHILD, labels.STATE, labels.STATE_DONE)
    assert out == Outcome(
        "queued", f"orch is not running; done at {'a' * 12} is delivered when it starts"
    )


@pytest.mark.parametrize("exc", [SyncError("boom"), GitError("boom"), IncusError("boom")])
def test_done_fetch_failure_refused_without_labels(
    tmp_path: Path, mocker: MockerFixture, exc: Exception
) -> None:
    rig = _make(tmp_path, mocker)
    rig.fetch.side_effect = exc
    out = messages.handle_message(rig.ctx, _done())
    assert out == refused("could not fetch your branch to the host: boom")
    _untouched(rig)


@pytest.mark.parametrize("oid", ["", "abc123", "A" * 40, "a" * 41])
def test_done_refuses_an_oid_that_is_not_full_hex(
    tmp_path: Path, mocker: MockerFixture, oid: str
) -> None:
    rig = _make(tmp_path, mocker)
    rig.fetch.return_value = FetchResult(
        branch="fix-1", old_oid=None, new_oid=oid, base_oid=None, commits_added=1
    )
    out = messages.handle_message(rig.ctx, _done())
    assert out.state == "refused"
    _untouched(rig)


def test_done_label_write_failure_refused_without_relay(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    rig = _make(tmp_path, mocker)
    rig.incus.config_set.side_effect = IncusError("etag")
    out = messages.handle_message(rig.ctx, _done())
    assert out == refused("could not record the done report: etag")
    rig.deliver.assert_not_called()


def test_done_state_is_written_after_sha(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.incus.config_set.side_effect = [None, IncusError("x")]
    messages.handle_message(rig.ctx, _done())
    keys = [c.args[1] for c in rig.incus.config_set.call_args_list]
    assert keys == [labels.DONE_SHA, labels.STATE]


def test_done_bounds_every_child_probe_and_the_fetch(tmp_path: Path, mocker: MockerFixture) -> None:
    """A hostile child must not stall the single broker loop: every exec into it
    and the ext:: fetch from it run with a timeout."""
    rig = _make(tmp_path, mocker)
    messages.handle_message(rig.ctx, _done())
    rig.incus.with_exec_timeout.assert_called_once_with(CHILD_EXEC_TIMEOUT_S)
    bounded = rig.incus.with_exec_timeout.return_value
    assert rig.repo_dir.call_args.args[1] is bounded
    assert rig.dirty.call_args.args[0] is bounded
    assert rig.branch.call_args.args[0] is bounded
    assert rig.fetch.call_args.args[1] is bounded
    assert rig.fetch.call_args.kwargs["timeout"] == CHILD_TRANSPORT_TIMEOUT_S
    # Labels go through the plain wrapper.
    bounded.config_set.assert_not_called()
    assert rig.incus.config_set.call_count == 2


def test_done_fetch_timeout_refused_without_labels(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.fetch.side_effect = GitError("git fetch timed out after 600s")
    out = messages.handle_message(rig.ctx, _done())
    assert out == refused("could not fetch your branch to the host: git fetch timed out after 600s")
    _untouched(rig)


def test_done_probe_timeout_refused_without_labels(tmp_path: Path, mocker: MockerFixture) -> None:
    rig = _make(tmp_path, mocker)
    rig.repo_dir.side_effect = IncusError("`incus exec ...` timed out after 60s")
    out = messages.handle_message(rig.ctx, _done())
    assert out.state == "refused"
    assert "timed out" in out.detail
    _untouched(rig)
