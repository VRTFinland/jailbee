import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from jailbee.incus import IncusError
from jailbee.orchestration import inbox_state, labels
from jailbee.orchestration.actions import ActionContext, Node


def context(tmp_path):
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    parent = Node("p-orch", "orch", True, {})
    child = Node("p-kid", "kid", True, {labels.SPAWNED_BY: parent.full})
    return ActionContext(
        cfg=MagicMock(),
        gcfg=MagicMock(),
        incus=MagicMock(),
        root=tmp_path,
        sender=parent,
        nodes={parent.full: parent, child.full: child},
        now=now,
    )


def delivered(ctx):
    return json.loads(ctx.incus.exec_with_input.call_args.args[2])


def test_snapshot_persists_start_across_broker_restart(tmp_path):
    ctx = context(tmp_path)
    inbox_state.sync_children(ctx)
    first = delivered(ctx)
    assert first["children"] == [
        {"name": "kid", "running": True, "started_at": ctx.now.timestamp()}
    ]
    ctx.now += timedelta(minutes=10)
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"] == first["children"]
    assert delivered(ctx)["updated_at"] == ctx.now.timestamp()


def test_snapshot_stop_restart_and_removal(tmp_path):
    ctx = context(tmp_path)
    inbox_state.sync_children(ctx)
    start = ctx.now.timestamp()
    ctx.now += timedelta(minutes=1)
    ctx.nodes = {"p-kid": Node("p-kid", "kid", False, {labels.SPAWNED_BY: "p-orch"})}
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["running"] is False
    assert delivered(ctx)["children"][0]["started_at"] == start
    ctx.now += timedelta(minutes=1)
    ctx.nodes = context(tmp_path).nodes
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["started_at"] == ctx.now.timestamp()
    ctx.nodes = {}
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"] == []


def test_snapshot_uses_incus_boot_timestamp_and_new_instance(tmp_path):
    from jailbee.orchestration.broker import _nodes
    from tests.conftest import make_config

    ctx = context(tmp_path)
    cfg = make_config(tmp_path).model_copy(update={"container_prefix": "p"})
    ctx.nodes = _nodes(
        [
            {
                "name": "p-kid",
                "status": "Running",
                "last_used_at": "2026-10-30T11:00:00Z",
                "config": {labels.SPAWNED_BY: "p-orch", "volatile.uuid": "one"},
            }
        ],
        {"p": cfg},
    )
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["started_at"] == ctx.now.timestamp() - 3600
    ctx.nodes = {
        "p-kid": Node("p-kid", "kid", True, {labels.SPAWNED_BY: "p-orch", "volatile.uuid": "two"})
    }
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["started_at"] == ctx.now.timestamp()


def test_snapshot_failed_write_can_retry(tmp_path):
    ctx = context(tmp_path)
    ctx.incus.exec_with_input.side_effect = IncusError("stopped")
    with pytest.raises(IncusError):
        inbox_state.sync_children(ctx)
    ctx.now += timedelta(seconds=5)
    ctx.incus.exec_with_input.side_effect = None
    inbox_state.sync_children(ctx)
    assert delivered(ctx)["children"][0]["started_at"] == ctx.now.timestamp() - 5
