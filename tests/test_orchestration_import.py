from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jailbee.git import GitError
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.orchestration import importing, labels
from jailbee.orchestration.actions import (
    CHILD_EXEC_TIMEOUT_S,
    CHILD_TRANSPORT_TIMEOUT_S,
    ActionContext,
    Node,
    done,
    refused,
)
from jailbee.orchestration.grant import Grant
from jailbee.orchestration.protocol import ImportManifest
from jailbee.sync import MergeInContainerResult, PushResult, SyncError
from tests.conftest import make_config

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

REF = "refs/jailbee/from/fix-1/fix-1"
SHA = "b" * 40
CHILD = "myrepo-fix-1"


def _result(new_oid: str = SHA) -> MergeInContainerResult:
    push = PushResult(
        source="fix-1", source_ref="refs/heads/fix-1", container_ref=REF, old_oid=None,
        new_oid=new_oid,
    )  # fmt: skip
    return MergeInContainerResult(
        push=push, container_branch="main", fast_forward_only=False, head_oid="c" * 40
    )


def _ctx(tmp_path: Path, child_running: bool = True) -> ActionContext:
    sender = Node("myrepo-orch", "orch", True, {labels.GRANT: Grant().to_label()})
    child = Node(CHILD, "fix-1", child_running, {labels.SPAWNED_BY: "myrepo-orch"})
    return ActionContext(
        cfg=make_config(tmp_path, container_prefix="myrepo"),
        gcfg=GlobalConfig(),
        incus=MagicMock(),
        root=tmp_path,
        sender=sender,
        nodes={sender.full: sender, child.full: child},
        now=datetime(2026, 10, 25, tzinfo=UTC),
    )


def _manifest(child: str = "fix-1") -> ImportManifest:
    return ImportManifest(version=1, id="i1", type="import", child=child)


@pytest.fixture
def merge(mocker: MockerFixture) -> MagicMock:
    return mocker.patch(
        "jailbee.orchestration.importing.sync.merge_container_into_container",
        return_value=_result(),
    )


def test_import_transports_and_labels(tmp_path: Path, merge: MagicMock) -> None:
    ctx = _ctx(tmp_path)
    out = importing.handle_import(ctx, _manifest())
    merge.assert_called_once_with(
        ctx.cfg,
        ctx.incus.with_exec_timeout.return_value,  # type: ignore[attr-defined]
        "fix-1",
        "orch",
        plain=True,
        ff_local_branch=False,
        timeout=CHILD_TRANSPORT_TIMEOUT_S,
    )
    ctx.incus.with_exec_timeout.assert_called_once_with(CHILD_EXEC_TIMEOUT_S)  # type: ignore[attr-defined]
    calls = [c.args for c in ctx.incus.config_set.call_args_list]  # type: ignore[attr-defined]
    assert calls == [
        (CHILD, labels.LAST_IMPORT, SHA),
        (CHILD, labels.STATE, labels.STATE_IMPORTED),
    ]
    assert out == done(f"imported as {REF} at {'b' * 12}")


def test_import_of_stopped_child_refused(tmp_path: Path, merge: MagicMock) -> None:
    ctx = _ctx(tmp_path, child_running=False)
    assert importing.handle_import(ctx, _manifest()) == refused(
        "fix-1 is not running; restart it first"
    )
    merge.assert_not_called()


def test_import_of_non_child_refused(tmp_path: Path, merge: MagicMock) -> None:
    out = importing.handle_import(_ctx(tmp_path), _manifest("x"))
    assert out == refused("`x` is not a child of this orchestrator")
    merge.assert_not_called()


@pytest.mark.parametrize(
    "exc",
    [
        SyncError("uncommitted changes in 'orch'"),
        GitError("uncommitted changes in 'orch'"),
        IncusError("uncommitted changes in 'orch'"),
    ],
)
def test_import_failure_refused_without_labels(
    tmp_path: Path, merge: MagicMock, exc: Exception
) -> None:
    merge.side_effect = exc
    ctx = _ctx(tmp_path)
    out = importing.handle_import(ctx, _manifest())
    assert out == refused("uncommitted changes in 'orch'")
    ctx.incus.config_set.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.parametrize("oid", ["", "abc", "B" * 40, "b" * 41])
def test_import_refuses_an_oid_that_is_not_full_hex(
    tmp_path: Path, merge: MagicMock, oid: str
) -> None:
    merge.return_value = _result(oid)
    ctx = _ctx(tmp_path)
    out = importing.handle_import(ctx, _manifest())
    assert out.state == "refused"
    assert REF in out.detail
    ctx.incus.config_set.assert_not_called()  # type: ignore[attr-defined]


def test_import_label_write_failure_refused(tmp_path: Path, merge: MagicMock) -> None:
    ctx = _ctx(tmp_path)
    ctx.incus.config_set.side_effect = IncusError("etag")  # type: ignore[attr-defined]
    out = importing.handle_import(ctx, _manifest())
    assert out == refused("imported but could not record the import: etag")


def test_import_state_is_written_last(tmp_path: Path, merge: MagicMock) -> None:
    ctx = _ctx(tmp_path)
    ctx.incus.config_set.side_effect = [None, IncusError("x")]  # type: ignore[attr-defined]
    importing.handle_import(ctx, _manifest())
    keys = [c.args[1] for c in ctx.incus.config_set.call_args_list]  # type: ignore[attr-defined]
    assert keys == [labels.LAST_IMPORT, labels.STATE]


def test_import_handler_registered_by_importing_actions_only() -> None:
    code = "from jailbee.orchestration import actions; assert callable(actions.HANDLERS['import'])"
    subprocess.run([sys.executable, "-c", code], check=True)
    subprocess.run([sys.executable, "-c", "import jailbee.orchestration.importing"], check=True)


def test_import_timeout_refused_without_labels(tmp_path: Path, merge: MagicMock) -> None:
    ctx = _ctx(tmp_path)
    merge.side_effect = GitError("git push timed out after 600s")
    out = importing.handle_import(ctx, _manifest())
    assert out == refused("git push timed out after 600s")
    ctx.incus.config_set.assert_not_called()  # type: ignore[attr-defined]
