from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, labels
from jailbee.orchestration.actions import ActionContext, Node, Outcome
from jailbee.orchestration.grant import Grant
from jailbee.orchestration.protocol import DestroyManifest, RestartManifest, StopManifest
from jailbee.orchestration.state import load_queue
from tests.conftest import make_config

if TYPE_CHECKING:
    from pathlib import Path


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        running: bool = False,
        limit: int = 1,
        config: dict[str, str] | None = None,
    ) -> None:
        self.cfg = make_config(tmp_path).model_copy(update={"container_prefix": "proj"})
        self.sender = Node(
            "proj-orch",
            "orch",
            True,
            {labels.GRANT: Grant.model_validate({"max_running_children": limit}).to_label()},
        )
        self.child = Node(
            "proj-c1", "c1", running, {labels.SPAWNED_BY: "proj-orch", **(config or {})}
        )
        self.stranger = Node("proj-x", "x", True, {})
        self.incus = MagicMock()
        self.incus.config_get.return_value = ""
        self.root = tmp_path

    def ctx(self) -> ActionContext:
        nodes = {n.full: n for n in [self.sender, self.child, self.stranger]}
        return ActionContext(
            self.cfg, GlobalConfig(), self.incus, self.root, self.sender, nodes,
            datetime(2026, 10, 25, tzinfo=UTC),
        )  # fmt: skip


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    m = {
        "jobs": MagicMock(return_value=set()),
        "stop": MagicMock(return_value=False),
        "boot": MagicMock(),
        "destroy": MagicMock(),
        "head": MagicMock(return_value="aaa"),
    }
    monkeypatch.setattr(actions, "live_create_jobs", m["jobs"])
    monkeypatch.setattr(actions.stopping, "stop_container", m["stop"])
    monkeypatch.setattr(actions.lifecycle, "boot_container", m["boot"])
    monkeypatch.setattr(actions.lifecycle, "destroy_container", m["destroy"])
    monkeypatch.setattr(actions.sync, "_container_head_oid", m["head"])
    monkeypatch.setattr(actions.lifecycle, "container_repo_dir", MagicMock(return_value="/r"))
    return m


def _restart(child: str = "c1") -> RestartManifest:
    return RestartManifest.model_validate(
        {"version": 1, "id": "r1", "type": "restart", "child": child, "prompt": "go on"}
    )


def _destroy(force: bool = False, child: str = "c1") -> DestroyManifest:
    return DestroyManifest.model_validate(
        {"version": 1, "id": "d1", "type": "destroy", "child": child, "force": force}
    )


def test_stop_running_child(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, running=True)
    m = StopManifest.model_validate({"version": 1, "id": "s1", "type": "stop", "child": "c1"})
    out = actions.handle_stop(rig.ctx(), m)
    assert out == Outcome("done", "child `c1` stopped")
    seams["stop"].assert_called_once_with(rig.incus, "proj-c1", show_progress=False)


def test_stop_stopped_child_and_stranger(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path)
    m = StopManifest.model_validate({"version": 1, "id": "s1", "type": "stop", "child": "c1"})
    assert actions.handle_stop(rig.ctx(), m) == Outcome("done", "child `c1` stopped")
    seams["stop"].assert_not_called()
    other = StopManifest.model_validate({"version": 1, "id": "s2", "type": "stop", "child": "x"})
    assert actions.handle_stop(rig.ctx(), other).state == "refused"
    seams["stop"].assert_not_called()


def test_restart_refuses_running_child(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, running=True, limit=3)
    out = actions.handle_restart(rig.ctx(), _restart())
    assert out == Outcome("refused", "child `c1` is running; stop it first")
    seams["boot"].assert_not_called()


def test_restart_boots_with_preambled_prompt(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=2)
    out = actions.handle_restart(rig.ctx(), _restart())
    assert out == Outcome("done", "child `c1` restarted")
    seams["boot"].assert_called_once_with(
        rig.cfg,
        rig.incus,
        "proj-c1",
        restart=False,
        initial_prompt=actions.child_preamble("orch") + "\n\ngo on",
    )


def test_restart_queues_when_full(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=1)
    seams["jobs"].return_value = {"proj-other"}  # a live create fills the only slot
    out = actions.handle_restart(rig.ctx(), _restart())
    assert out == Outcome("queued", "queued at position 1")
    seams["boot"].assert_not_called()
    q = load_queue(tmp_path)["proj-orch"]
    assert [(r.kind, r.child_full) for r in q] == [("restart", "proj-c1")]
    assert q[0].prompt.endswith("\n\ngo on")


def test_drain_starts_queued_restart(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=1)
    seams["jobs"].return_value = {"proj-other"}
    actions.handle_restart(rig.ctx(), _restart())
    seams["jobs"].return_value = set()
    results = actions.drain_queue(rig.ctx())
    assert [o.state for _, o in results] == ["started"]
    seams["boot"].assert_called_once()
    assert load_queue(tmp_path).get("proj-orch", []) == []


def test_restart_queues_behind_nonempty_queue(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=2)
    seams["jobs"].return_value = {"proj-o1", "proj-o2"}
    actions.handle_restart(rig.ctx(), _restart())  # queued: slots full
    seams["jobs"].return_value = set()  # a slot frees, but the queue is not empty
    again = RestartManifest.model_validate(
        {"version": 1, "id": "r2", "type": "restart", "child": "c2", "prompt": "p"}
    )
    ctx = rig.ctx()
    c2 = Node("proj-c2", "c2", False, {labels.SPAWNED_BY: "proj-orch"})
    ctx.nodes = {**ctx.nodes, c2.full: c2}
    out = actions.handle_restart(ctx, again)
    assert out == Outcome("queued", "queued at position 2")
    seams["boot"].assert_not_called()


def test_destroy_refused_without_import(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, running=True)
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out == Outcome(
        "refused",
        "child `c1` has commits that were never imported (use force: true to destroy anyway)",
    )
    seams["destroy"].assert_not_called()


def test_destroy_refused_when_head_moved_after_import(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path, running=True, config={labels.LAST_IMPORT: "old"})
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out.state == "refused"
    assert "never imported" in out.detail
    seams["destroy"].assert_not_called()


def test_destroy_allowed_when_head_equals_last_import(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path, running=True, config={labels.LAST_IMPORT: "aaa"})
    out = actions.handle_destroy(rig.ctx(), _destroy())
    assert out == Outcome("done", "child `c1` destroyed")
    seams["destroy"].assert_called_once_with(rig.cfg, rig.incus, "proj-c1", force=True)


def test_destroy_force_skips_guard(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, running=True)
    out = actions.handle_destroy(rig.ctx(), _destroy(force=True))
    assert out.state == "done"
    seams["head"].assert_not_called()
    seams["destroy"].assert_called_once()


def test_destroy_stopped_child_uses_done_sha(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    ok = Rig(tmp_path, config={labels.LAST_IMPORT: "bbb", labels.DONE_SHA: "bbb"})
    assert actions.handle_destroy(ok.ctx(), _destroy()).state == "done"
    seams["head"].assert_not_called()
    bad = Rig(tmp_path, config={labels.LAST_IMPORT: "bbb", labels.DONE_SHA: "ccc"})
    assert actions.handle_destroy(bad.ctx(), _destroy()).state == "refused"
    none = Rig(tmp_path, config={labels.LAST_IMPORT: "bbb"})
    assert actions.handle_destroy(none.ctx(), _destroy()).state == "refused"


def test_destroy_stranger_refused(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path)
    out = actions.handle_destroy(rig.ctx(), _destroy(force=True, child="x"))
    assert out.state == "refused"
    seams["destroy"].assert_not_called()
