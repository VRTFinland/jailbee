from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jailbee import lifecycle
from jailbee.forking import ForkSource
from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, labels
from jailbee.orchestration.actions import ActionContext, Node, Outcome
from jailbee.orchestration.grant import ChildRights, Grant
from jailbee.orchestration.protocol import SpawnManifest
from jailbee.orchestration.state import QueuedRequest, load_queue, save_queue
from tests.conftest import make_config

if TYPE_CHECKING:
    from pathlib import Path


class Rig:
    def __init__(self, tmp_path: Path, limit: int = 1, running: int = 0) -> None:
        self.cfg = make_config(tmp_path).model_copy(update={"container_prefix": "proj"})
        self.sender = Node(
            full="proj-orch",
            short="orch",
            running=True,
            config={labels.GRANT: Grant.model_validate({"max_running_children": limit}).to_label()},
        )
        self.children = [
            Node(
                full=f"proj-c{i}",
                short=f"c{i}",
                running=True,
                config={labels.SPAWNED_BY: "proj-orch"},
            )
            for i in range(running)
        ]
        self.incus = MagicMock()
        self.incus.exists.return_value = False
        self.incus.config_get.return_value = ""
        self.root = tmp_path

    def ctx(self) -> ActionContext:
        return ActionContext(
            cfg=self.cfg,
            gcfg=GlobalConfig(),
            incus=self.incus,
            root=self.root,
            sender=self.sender,
            nodes={n.full: n for n in [self.sender, *self.children]},
            now=datetime(2026, 10, 25, tzinfo=UTC),
        )


def _m(branch: str = "fix/1", kind: str = "fork", mid: str = "a1") -> SpawnManifest:
    return SpawnManifest.model_validate(
        {
            "version": 1,
            "id": mid,
            "type": "spawn",
            "kind": kind,
            "branch": branch,
            "prompt": "do it",
            "rights": ChildRights().model_dump(mode="json"),
        }
    )


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    fork = ForkSource("proj-orch", "main", "c0ffee", None)
    m = {
        "jobs": MagicMock(return_value=set()),
        "prepare": MagicMock(return_value=fork),
        "pin": MagicMock(return_value="ref"),
        "start": MagicMock(return_value=Outcome("started", "ok")),
    }
    monkeypatch.setattr(actions, "live_create_jobs", m["jobs"])
    monkeypatch.setattr(actions.forking, "prepare_fork", m["prepare"])
    monkeypatch.setattr(actions.forking, "pin_fork_commit", m["pin"])
    monkeypatch.setattr(actions, "start_child", m["start"])
    return m


def _full(rig: Rig, branch: str) -> str:
    return str(lifecycle.derive_container_name(rig.cfg, branch))


def test_spawn_under_limit_starts(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=2, running=1)
    out = actions.handle_spawn(rig.ctx(), _m())
    assert out.state == "started"
    seams["start"].assert_called_once()
    assert load_queue(tmp_path) == {}


def test_spawn_at_limit_queues_with_position(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=1, running=1)
    first = actions.handle_spawn(rig.ctx(), _m("fix/1", mid="a1"))
    second = actions.handle_spawn(rig.ctx(), _m("fix/2", mid="a2"))
    assert first == Outcome("queued", "queued at position 1")
    assert second == Outcome("queued", "queued at position 2")
    seams["start"].assert_not_called()
    q = load_queue(tmp_path)["proj-orch"]
    assert [r.manifest_id for r in q] == ["a1", "a2"]
    assert q[0].commit == "c0ffee"


def test_fork_commit_pinned_at_queue_time(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=1, running=1)
    actions.handle_spawn(rig.ctx(), _m("fix/1"))
    seams["prepare"].assert_called_once_with(rig.cfg, rig.incus, "orch")
    seams["pin"].assert_called_once_with(rig.cfg, _full(rig, "fix/1"), "c0ffee")


def test_new_kind_does_not_fork(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=2)
    actions.handle_spawn(rig.ctx(), _m(kind="new"))
    seams["prepare"].assert_not_called()
    assert seams["start"].call_args.kwargs["fork"] is None


def _queue(rig: Rig, branches: list[str], kind: str = "fork") -> None:
    reqs = [
        QueuedRequest(
            manifest_id=f"m{i}",
            child_full=_full(rig, b),
            kind=kind,
            commit="c0ffee" if kind == "fork" else None,
            queued_at="t",
            branch=b,
            base=None,
            prompt="p",
            rights=ChildRights().model_dump(mode="json"),
        )
        for i, b in enumerate(branches)
    ]
    save_queue(rig.root, {"proj-orch": reqs})


def test_drain_starts_fifo_when_slot_frees(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=2, running=1)
    _queue(rig, ["fix/1", "fix/2", "fix/3"])
    results = actions.drain_queue(rig.ctx())
    assert [r.manifest_id for r, _ in results] == ["m0"]
    assert [o.state for _, o in results] == ["started"]
    plan = seams["start"].call_args.args[1]
    assert plan.child_full == _full(rig, "fix/1")
    fork = seams["start"].call_args.kwargs["fork"]
    assert fork.commit == "c0ffee"
    assert [r.manifest_id for r in load_queue(tmp_path)["proj-orch"]] == ["m1", "m2"]


def test_creating_child_counts_as_running(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=3)
    seams["jobs"].return_value = {_full(rig, "fix/9")}
    assert actions.running_children(rig.ctx()) == 1


def test_running_and_creating_deduplicated(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=3, running=1)
    seams["jobs"].return_value = {"proj-c0", "proj-new"}
    assert actions.running_children(rig.ctx()) == 2


def test_lowered_limit_starts_nothing(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=1, running=3)
    _queue(rig, ["fix/1"])
    assert actions.drain_queue(rig.ctx()) == []
    seams["start"].assert_not_called()
    rig.incus.stop.assert_not_called()
    assert len(load_queue(tmp_path)["proj-orch"]) == 1


def test_name_taken_at_dequeue_refused(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=2)
    _queue(rig, ["fix/1"])
    rig.incus.exists.return_value = True
    ((req, out),) = actions.drain_queue(rig.ctx())
    assert out.state == "refused"
    assert (
        out.detail == f"container `{lifecycle.short_name(rig.cfg, req.child_full)}` already exists"
    )
    seams["start"].assert_not_called()
    assert load_queue(tmp_path) == {}


def test_refused_entry_does_not_use_a_slot(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=1)
    _queue(rig, ["fix/1", "fix/2"])
    seams["start"].side_effect = [Outcome("refused", "no"), Outcome("started", "ok")]
    results = actions.drain_queue(rig.ctx())
    assert [o.state for _, o in results] == ["refused", "started"]


def test_unknown_kind_refused(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=1)
    _queue(rig, ["fix/1"], kind="bogus")
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out.state == "refused"


def test_starter_seam_dispatches_restart(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, limit=1)
    _queue(rig, ["fix/1"], kind="restart")
    starter = MagicMock(return_value=Outcome("started", "restarted"))
    monkeypatch.setitem(actions.STARTERS, "restart", starter)
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out.detail == "restarted"
    starter.assert_called_once()


def test_revoked_grant_drops_queue_with_status(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=1)
    _queue(rig, ["fix/1", "fix/2"])
    rig.sender = Node(full="proj-orch", short="orch", running=True, config={})
    dropped = actions.drop_queue(rig.ctx(), actions.GRANT_REVOKED)
    assert [r.manifest_id for r in dropped] == ["m0", "m1"]
    assert actions.GRANT_REVOKED == "orchestration grant revoked"
    assert load_queue(tmp_path) == {}
    assert actions.drain_queue(rig.ctx()) == []


def test_handler_registered() -> None:
    assert actions.HANDLERS["spawn"] is actions.handle_spawn


def test_new_spawn_queues_behind_nonempty_queue(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path, limit=3)
    _queue(rig, ["fix/1"])
    out = actions.handle_spawn(rig.ctx(), _m("fix/2", mid="a2"))
    assert out == Outcome("queued", "queued at position 2")
    seams["start"].assert_not_called()


def test_drain_without_grant_does_nothing(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=2)
    _queue(rig, ["fix/1"])
    rig.sender = Node(full="proj-orch", short="orch", running=True, config={})
    assert actions.drain_queue(rig.ctx()) == []
    seams["start"].assert_not_called()
    assert len(load_queue(tmp_path)["proj-orch"]) == 1


def test_drain_dequeues_new_kind_without_fork(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    rig = Rig(tmp_path, limit=2)
    _queue(rig, ["fix/1"], kind="new")
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out.state == "started"
    assert seams["start"].call_args.kwargs["fork"] is None


def _jobs(monkeypatch: pytest.MonkeyPatch, rows: dict[str, MagicMock]) -> None:
    monkeypatch.setattr("sqlmodel.Session", MagicMock())
    monkeypatch.setattr("jailbee.db.get_engine", MagicMock())
    monkeypatch.setattr("jailbee.background.list_all_jobs", lambda _s: rows)


def _job(pid: int, phase: str = "cloning") -> MagicMock:
    return MagicMock(op_kind="create", container_prefix="proj", pid=pid, phase=phase)


def test_live_jobs_counts_alive_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    _jobs(monkeypatch, {"proj-x": _job(4242)})
    monkeypatch.setattr("jailbee.background.worker_alive", lambda _p: True)
    assert actions.live_create_jobs("proj") == {"proj-x"}


def test_live_jobs_skips_terminal_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.background import TERMINAL_PHASES

    phase = sorted(TERMINAL_PHASES)[0]
    _jobs(monkeypatch, {"proj-x": _job(4242, phase)})
    monkeypatch.setattr("jailbee.background.worker_alive", lambda _p: True)
    assert actions.live_create_jobs("proj") == set()


def test_live_jobs_skips_pid_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _jobs(monkeypatch, {"proj-x": _job(0)})
    monkeypatch.setattr("jailbee.background.worker_alive", lambda _p: True)
    assert actions.live_create_jobs("proj") == set()
