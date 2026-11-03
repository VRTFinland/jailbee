from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, labels
from jailbee.orchestration.actions import ActionContext, Node, Outcome, dispatch
from jailbee.orchestration.grant import Grant, RepoCap
from jailbee.orchestration.protocol import (
    ChildRights,
    DestroyManifest,
    ImportManifest,
    MessageManifest,
    SpawnManifest,
    StopManifest,
)
from tests.conftest import make_config

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


def _node(full: str, running: bool = True, **config: str) -> Node:
    return Node(full=full, short=full.removeprefix("proj-"), running=running, config=config)


def _ctx(
    tmp_path: Path, sender: Node, nodes: list[Node] | None = None, **kw: object
) -> ActionContext:
    cfg = make_config(tmp_path)
    allnodes: Mapping[str, Node] = {n.full: n for n in [sender, *(nodes or [])]}
    return ActionContext(
        cfg=cfg,
        gcfg=GlobalConfig(),
        incus=MagicMock(),
        root=tmp_path,
        sender=sender,
        nodes=allnodes,
        now=datetime(2026, 10, 25, tzinfo=UTC),
        **kw,  # type: ignore[arg-type]  # test helper forwards optional overrides
    )


def _grant(**kw: object) -> str:
    return Grant.model_validate(kw).to_label()


def _spawn() -> SpawnManifest:
    return SpawnManifest(
        version=1,
        id="a1",
        type="spawn",
        kind="fork",
        branch="b",
        prompt="p",
        rights=ChildRights(),
    )


def test_child_preamble_requests_background_waiter() -> None:
    assert "jailbee inbox wait --timeout none" in actions.child_preamble("orch")


def test_child_preamble_discourages_progress_notes() -> None:
    assert "send no progress notes" in actions.child_preamble("orch")


def test_role_child_wins_over_grant(tmp_path: Path) -> None:
    s = _node("proj-x", **{labels.SPAWNED_BY: "proj-p", labels.GRANT: _grant()})
    assert _ctx(tmp_path, s).role() == "child"


def test_role_none_without_labels(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _node("proj-x"))
    assert ctx.role() == "none"
    assert dispatch(ctx, _spawn()) is None


def test_role_orchestrator_and_bad_grant(tmp_path: Path) -> None:
    assert _ctx(tmp_path, _node("proj-x", **{labels.GRANT: _grant()})).role() == "orchestrator"
    bad = _ctx(tmp_path, _node("proj-x", **{labels.GRANT: "{nope"}))
    assert bad.raw_grant() is None
    assert bad.role() == "none"


def test_child_may_only_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called = MagicMock()
    monkeypatch.setitem(actions.HANDLERS, "spawn", called)
    ctx = _ctx(tmp_path, _node("proj-x", **{labels.SPAWNED_BY: "proj-p"}))
    out = dispatch(ctx, _spawn())
    assert out == Outcome("refused", "children may only send messages")
    called.assert_not_called()


def test_operation_not_granted_refused(tmp_path: Path) -> None:
    grant = _grant(operations=["spawn", "message", "stop"])
    ctx = _ctx(tmp_path, _node("proj-o", **{labels.GRANT: grant}))
    out = dispatch(ctx, DestroyManifest(version=1, id="d1", type="destroy", child="c"))
    assert out == Outcome("refused", "operation `destroy` is not in this container's grant")


def test_dispatch_refresh_needs_operation(tmp_path: Path) -> None:
    from jailbee.orchestration.protocol import RefreshManifest

    ctx = _ctx(tmp_path, _node("proj-o", **{labels.GRANT: _grant(operations=["spawn", "message"])}))
    m = RefreshManifest(version=1, id="r1", type="refresh", child="c", services=["db"])
    assert dispatch(ctx, m) == Outcome(
        "refused", "operation `refresh` is not in this container's grant"
    )


def test_unknown_child_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def stop(ctx: ActionContext, m: StopManifest) -> Outcome:  # type: ignore[override]  # narrower manifest
        res = ctx.child(m.child)
        return res if isinstance(res, Outcome) else actions.done("stopped")

    monkeypatch.setitem(actions.HANDLERS, "stop", stop)  # type: ignore[arg-type]  # narrower manifest
    ctx = _ctx(
        tmp_path,
        _node("proj-o", **{labels.GRANT: _grant()}),
        [_node("proj-c", **{labels.SPAWNED_BY: "proj-o"}), _node("proj-other")],
    )
    assert [n.short for n in ctx.children()] == ["c"]
    out = dispatch(ctx, StopManifest(version=1, id="s1", type="stop", child="other"))
    assert out == Outcome("refused", "`other` is not a child of this orchestrator")
    ok = dispatch(ctx, StopManifest(version=1, id="s2", type="stop", child="c"))
    assert ok == Outcome("done", "stopped")


def test_child_lookup_refuses(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        _node("proj-o", **{labels.GRANT: _grant()}),
        [_node("proj-c", **{labels.SPAWNED_BY: "proj-o"}), _node("proj-other")],
    )
    for bad in ("other", "", "a/b", "c d", "../c"):
        res = ctx.child(bad)
        assert isinstance(res, Outcome)
        assert res.state == "refused"
    assert ctx.child("other") == Outcome("refused", "`other` is not a child of this orchestrator")


def test_repo_cap_disallow_refuses_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(actions.profiles, "repo_cap", lambda cfg: RepoCap(allowed=False))
    ctx = _ctx(tmp_path, _node("proj-o", **{labels.GRANT: _grant()}))
    assert ctx.grant() is None
    out = dispatch(ctx, _spawn())
    assert out == Outcome("refused", "orchestration is not allowed in this repo")


def test_grant_clamped_by_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions.profiles, "repo_cap", lambda cfg: RepoCap(max_running_children=1))
    ctx = _ctx(tmp_path, _node("proj-o", **{labels.GRANT: _grant(max_running_children=5)}))
    g = ctx.grant()
    assert g is not None
    assert g.max_running_children == 1


def test_unregistered_type_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(actions, "HANDLERS", {})
    ctx = _ctx(tmp_path, _node("proj-o", **{labels.GRANT: _grant()}))
    out = dispatch(ctx, ImportManifest(version=1, id="i1", type="import", child="c"))
    assert out == Outcome("refused", "`import` is not supported by this broker version")


def test_handler_called_for_granted_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = MagicMock(return_value=actions.done("ok"))
    monkeypatch.setitem(actions.HANDLERS, "spawn", handler)
    ctx = _ctx(tmp_path, _node("proj-o", **{labels.GRANT: _grant()}))
    m = _spawn()
    assert dispatch(ctx, m) == Outcome("done", "ok")
    handler.assert_called_once_with(ctx, m)


def test_child_message_reaches_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = MagicMock(return_value=actions.done("ok"))
    monkeypatch.setitem(actions.HANDLERS, "message", handler)
    ctx = _ctx(tmp_path, _node("proj-x", **{labels.SPAWNED_BY: "proj-p"}))
    m = MessageManifest(version=1, id="m1", type="message", body="hi")
    assert dispatch(ctx, m) == Outcome("done", "ok")


def test_failed_helper() -> None:
    assert actions.failed("x") == Outcome("failed", "x")


def _live_job(
    monkeypatch: pytest.MonkeyPatch, *, pid: int, clearable: bool, kind: str = "create"
) -> None:
    monkeypatch.setattr("sqlmodel.Session", MagicMock())
    monkeypatch.setattr("jailbee.db.get_engine", MagicMock())
    row = MagicMock(op_kind=kind, pid=pid, phase="cloning")
    monkeypatch.setattr("jailbee.background.get_job", lambda _s, _n: row)
    monkeypatch.setattr("jailbee.background.clearable", lambda _phase, _pid: clearable)


def test_live_job_kind_returns_kind_for_live_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _live_job(monkeypatch, pid=4242, clearable=False, kind="message")
    assert actions.live_job_kind("proj-x") == "message"


def test_live_job_kind_none_for_failed_or_dead_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _live_job(monkeypatch, pid=4242, clearable=True)
    assert actions.live_job_kind("proj-x") is None
    _live_job(monkeypatch, pid=0, clearable=False)
    assert actions.live_job_kind("proj-x") is None


def test_live_job_kind_none_without_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sqlmodel.Session", MagicMock())
    monkeypatch.setattr("jailbee.db.get_engine", MagicMock())
    monkeypatch.setattr("jailbee.background.get_job", lambda _s, _n: None)
    assert actions.live_job_kind("proj-x") is None


def _delivery_ctx(tmp_path: Path) -> ActionContext:
    return _ctx(
        tmp_path,
        _node("proj-o", **{labels.GRANT: _grant()}),
        [_node("proj-c", **{labels.SPAWNED_BY: "proj-o"})],
    )


def _stamp(tmp_path: Path) -> object:
    import json

    path = tmp_path / "inbox-state" / "proj-o.json"
    if not path.exists():
        return None
    return {c["full"]: c.get("last_to_child_at") for c in json.loads(path.read_text())["children"]}


@pytest.mark.parametrize("state", ["done", "queued", "started"])
def test_dispatch_notes_delivery_on_accepted_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    monkeypatch.setitem(actions.HANDLERS, "message", MagicMock(return_value=Outcome(state, "x")))  # type: ignore[arg-type]
    ctx = _delivery_ctx(tmp_path)
    m = MessageManifest(version=1, id="m1", type="message", to="c", body="hi")
    assert dispatch(ctx, m) == Outcome(state, "x")  # type: ignore[arg-type]
    assert _stamp(tmp_path) == {"proj-c": ctx.now.timestamp()}


def test_dispatch_notes_delivery_on_done_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(actions.HANDLERS, "message", MagicMock(return_value=actions.done("ok")))
    ctx = _delivery_ctx(tmp_path)
    dispatch(ctx, MessageManifest(version=1, id="m1", type="message", to="c", body="hi"))
    assert _stamp(tmp_path) == {"proj-c": ctx.now.timestamp()}


@pytest.mark.parametrize("kind", ["update", "restart"])
def test_dispatch_notes_delivery_on_update_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from jailbee.orchestration.protocol import RestartManifest, UpdateManifest

    monkeypatch.setitem(actions.HANDLERS, kind, MagicMock(return_value=actions.done("ok")))
    ctx = _delivery_ctx(tmp_path)
    m = (
        UpdateManifest(version=1, id="u1", type="update", child="c")
        if kind == "update"
        else RestartManifest(version=1, id="r1", type="restart", child="c", prompt="go")
    )
    dispatch(ctx, m)
    assert _stamp(tmp_path) == {"proj-c": ctx.now.timestamp()}


@pytest.mark.parametrize("state", ["refused", "failed"])
def test_dispatch_does_not_note_refused_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    from jailbee.orchestration.protocol import UpdateManifest

    monkeypatch.setitem(actions.HANDLERS, "update", MagicMock(return_value=Outcome(state, "no")))  # type: ignore[arg-type]
    ctx = _delivery_ctx(tmp_path)
    dispatch(ctx, UpdateManifest(version=1, id="u1", type="update", child="c"))
    assert _stamp(tmp_path) is None


def test_dispatch_does_not_note_unknown_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(actions.HANDLERS, "message", MagicMock(return_value=actions.done("ok")))
    ctx = _delivery_ctx(tmp_path)
    dispatch(ctx, MessageManifest(version=1, id="m1", type="message", to="nobody", body="hi"))
    assert _stamp(tmp_path) is None


def test_dispatch_child_message_to_orchestrator_is_not_a_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(actions.HANDLERS, "message", MagicMock(return_value=actions.done("ok")))
    ctx = _ctx(tmp_path, _node("proj-x", **{labels.SPAWNED_BY: "proj-p"}))
    dispatch(ctx, MessageManifest(version=1, id="m1", type="message", body="hi"))
    assert not (tmp_path / "inbox-state").exists()


def test_dispatch_swallows_note_delivery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration import inbox_state

    def boom(*args: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setitem(actions.HANDLERS, "message", MagicMock(return_value=actions.done("ok")))
    monkeypatch.setattr(inbox_state, "note_delivery", boom)
    ctx = _delivery_ctx(tmp_path)
    m = MessageManifest(version=1, id="m1", type="message", to="c", body="hi")
    assert dispatch(ctx, m) == Outcome("done", "ok")
