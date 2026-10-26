from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jailbee import lifecycle
from jailbee.forking import ForkSource
from jailbee.global_config import GlobalConfig
from jailbee.orchestration import actions, labels, state
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


def test_limit_holds_when_job_row_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jailbee.create_flow import CreateStarted

    rig = Rig(tmp_path, limit=2)
    monkeypatch.setattr(actions, "live_create_jobs", lambda prefix: set())
    pids = iter(range(777, 782))
    monkeypatch.setattr(
        actions,
        "start_create",
        lambda *a, **kw: CreateStarted("proj-x", next(pids), tmp_path / "log"),
    )
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: True, raising=False)
    states = [
        actions.handle_spawn(rig.ctx(), _m(f"fix/{i}", kind="new", mid=f"s{i}")).state
        for i in range(5)
    ]
    assert states == ["started", "started", "queued", "queued", "queued"]


def test_finished_create_holds_slot_until_next_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jailbee.create_flow import CreateStarted

    rig = Rig(tmp_path, limit=1)
    monkeypatch.setattr(actions, "live_create_jobs", lambda prefix: set())
    monkeypatch.setattr(actions, "start_create", lambda *a, **kw: CreateStarted("proj-a", 777, tmp_path / "log"))
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: True)
    assert actions.handle_spawn(rig.ctx(), _m("a", kind="new")).state == "started"
    # Worker succeeds, but this tick's nodes still predate its container.
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: False)
    assert actions.handle_spawn(rig.ctx(), _m("b", kind="new", mid="b1")).state == "queued"
    actions.prune_reservations(tmp_path)
    rig.children = [Node("proj-a", "a", True, {labels.SPAWNED_BY: "proj-orch"})]
    assert actions.drain_queue(rig.ctx()) == []
    rig.children = []  # later stopped/destroyed
    assert [out.state for _, out in actions.drain_queue(rig.ctx())] == ["started"]


def test_duplicate_spawn_cannot_replace_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jailbee.create_flow import CreateStarted

    rig = Rig(tmp_path, limit=2)
    monkeypatch.setattr(actions, "live_create_jobs", lambda prefix: set())
    launch = MagicMock(return_value=CreateStarted("proj-a", 777, tmp_path / "log"))
    monkeypatch.setattr(actions, "start_create", launch)
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: True)
    assert actions.handle_spawn(rig.ctx(), _m("a", kind="new")).state == "started"
    assert actions.handle_spawn(rig.ctx(), _m("a", kind="new", mid="a2")) == Outcome(
        "refused", "container `a` is already being created",
    )
    assert launch.call_count == 1
    assert state.load_reservations(tmp_path)["proj-orch"][0].pid == 777


def test_reserved_name_blocks_queued_spawn_and_restart(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jailbee.orchestration.protocol import RestartManifest

    rig = Rig(tmp_path, limit=3)
    state.save_reservations(tmp_path, {"proj-orch": [state.Reservation("proj-fix-1", 777, "t")]})
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: True)
    _queue(rig, ["fix/1"], kind="new")
    assert [out.state for _, out in actions.drain_queue(rig.ctx())] == ["refused"]
    rig.children = [Node("proj-fix-1", "fix-1", False, {labels.SPAWNED_BY: "proj-orch"})]
    m = RestartManifest(version=1, id="r1", type="restart", child="fix-1", prompt="go")
    assert actions.handle_restart(rig.ctx(), m) == Outcome("refused", "child `fix-1` is still being created")
    _queue(rig, ["fix/1"], kind="restart")
    assert [out.state for _, out in actions.drain_queue(rig.ctx())] == ["refused"]


def test_start_child_records_reservation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jailbee.create_flow import CreateStarted

    rig = Rig(tmp_path, limit=2)
    monkeypatch.setattr(actions, "live_create_jobs", lambda prefix: set())
    monkeypatch.setattr(
        actions, "start_create", lambda *a, **kw: CreateStarted("proj-fix-1", 777, tmp_path / "log")
    )
    assert actions.handle_spawn(rig.ctx(), _m(kind="new")).state == "started"
    assert state.load_reservations(tmp_path) == {
        "proj-orch": [state.Reservation("proj-fix-1", 777, "2026-10-25T00:00:00+00:00")],
    }


def test_dead_reservation_does_not_count(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    state.save_reservations(tmp_path, {"proj-orch": [state.Reservation("proj-x", 777, "t")]})
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: False, raising=False)
    actions.prune_reservations(tmp_path)
    assert actions.running_children(rig.ctx()) == 0


def test_reservation_and_running_child_deduplicated(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, running=1)
    state.save_reservations(tmp_path, {"proj-orch": [state.Reservation("proj-c0", 777, "t")]})
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: True, raising=False)
    assert actions.running_children(rig.ctx()) == 1


@pytest.mark.parametrize("job_live", [False, True])
def test_reserved_not_running_child_counts(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch, job_live: bool
) -> None:
    rig = Rig(tmp_path)
    rig.children = [Node("proj-c0", "c0", False, {labels.SPAWNED_BY: "proj-orch"})]
    seams["jobs"].return_value = {"proj-c0"} if job_live else set()
    state.save_reservations(tmp_path, {"proj-orch": [state.Reservation("proj-c0", 777, "t")]})
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: True, raising=False)
    assert actions.handle_spawn(rig.ctx(), _m(kind="new")).state == "queued"


def test_prune_drops_dead_keeps_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    live = state.Reservation("proj-b", 2, "t")
    state.save_reservations(
        tmp_path, {"proj-orch": [state.Reservation("proj-a", 1, "t")], "proj-other": [live]}
    )
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: pid == 2)
    actions.prune_reservations(tmp_path)
    assert state.load_reservations(tmp_path) == {"proj-other": [live]}
    before = (tmp_path / state.RESERVATIONS_FILE).stat().st_mtime_ns
    actions.prune_reservations(tmp_path)
    assert (tmp_path / state.RESERVATIONS_FILE).stat().st_mtime_ns == before


def test_failed_create_frees_slot_on_next_drain(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    state.save_reservations(tmp_path, {"proj-orch": [state.Reservation("proj-failed", 777, "t")]})
    alive = {777}
    monkeypatch.setattr(actions, "reservation_alive", lambda pid: pid in alive)
    _queue(rig, ["fix/1"], kind="new")
    assert actions.drain_queue(rig.ctx()) == []
    alive.clear()
    actions.prune_reservations(tmp_path)
    assert state.load_reservations(tmp_path) == {}
    assert [out.state for _, out in actions.drain_queue(rig.ctx())] == ["started"]
    assert load_queue(tmp_path) == {}


@pytest.mark.parametrize("running, expected", [(0, "started"), (2, "queued")])
def test_spawn_logs_limit_breakdown(
    tmp_path: Path,
    seams: dict[str, MagicMock],
    caplog: pytest.LogCaptureFixture,
    running: int,
    expected: str,
) -> None:
    rig = Rig(tmp_path, limit=2, running=running)
    with caplog.at_level(logging.INFO, logger=actions.__name__):
        assert actions.handle_spawn(rig.ctx(), _m(kind="new")).state == expected
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        f"spawn proj-fix-1 for proj-orch: running={running} creating(jobs)=0 "
        f"reserved=0 total={running} limit=2 -> {expected}"
    ]


def test_drain_logs_limit_breakdown(
    tmp_path: Path, seams: dict[str, MagicMock], caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(tmp_path, limit=1)
    _queue(rig, ["fix/1", "fix/2"], kind="new")
    with caplog.at_level(logging.INFO, logger=actions.__name__):
        actions.drain_queue(rig.ctx())
    assert [r.getMessage() for r in caplog.records] == [
        "spawn proj-fix-1 for proj-orch: running=0 creating(jobs)=0 "
        "reserved=0 total=0 limit=1 -> started",
        "spawn proj-fix-2 for proj-orch: running=0 creating(jobs)=0 "
        "reserved=0 total=1 limit=1 -> queued",
    ]


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


def _granted(rig: Rig, **grant: object) -> None:
    rig.sender = Node(
        full="proj-orch",
        short="orch",
        running=True,
        config={labels.GRANT: Grant.model_validate(grant).to_label()},
    )


def _queue_rights(rig: Rig, kind: str = "new", **rights: object) -> None:
    req = QueuedRequest(
        manifest_id="m0",
        child_full=_full(rig, "fix/1"),
        kind=kind,
        commit=None,
        queued_at="t",
        branch="fix/1" if kind != "restart" else "",
        base=None,
        prompt="p",
        rights=ChildRights.model_validate(rights).model_dump(mode="json")
        if kind != "restart"
        else {},
    )
    save_queue(rig.root, {"proj-orch": [req]})


def test_queued_spawn_rechecked_when_grant_narrowed(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    """Rights valid at queue time, mount removed from the grant before dequeue."""
    rig = Rig(tmp_path)
    rig.cfg = rig.cfg.model_copy(update={"optional_mounts": {"data": MagicMock()}})
    _queue_rights(rig, mounts=["data"])
    _granted(rig, max_running_children=1)  # grantable mounts now empty
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out == Outcome("refused", "mount 'data' is not grantable")
    seams["start"].assert_not_called()


def test_queued_spawn_rechecked_against_repo_cap(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration.grant import RepoCap

    rig = Rig(tmp_path)
    _granted(rig, max_running_children=1, grantable={"loose": True})
    _queue_rights(rig, loose=True)
    monkeypatch.setattr(actions.profiles, "repo_cap", lambda cfg: RepoCap(loose=False))
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out == Outcome("refused", "loose network mode is not grantable")
    seams["start"].assert_not_called()


def test_queued_spawn_with_valid_rights_still_starts(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path)
    rig.cfg = rig.cfg.model_copy(update={"optional_mounts": {"data": MagicMock()}})
    _granted(rig, max_running_children=1, grantable={"mounts": ["data"]})
    _queue_rights(rig, mounts=["data"])
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out.state == "started"


def test_queued_restart_refused_when_restart_revoked(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path)
    _granted(rig, max_running_children=1, operations=["spawn", "message", "stop"])
    child = Node(
        full=_full(rig, "fix/1"),
        short="fix-1",
        running=False,
        config={labels.SPAWNED_BY: "proj-orch"},
    )
    rig.children = [child]
    _queue_rights(rig, kind="restart")
    boot = MagicMock()
    monkeypatch.setattr(actions.lifecycle, "boot_container", boot)
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out == Outcome("refused", "operation `restart` is not in this container's grant")
    boot.assert_not_called()


def test_spawn_refused_while_same_child_is_being_created(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    """A second spawn for the branch before the worker's `incus init` must not start twice."""
    rig = Rig(tmp_path, limit=3)
    seams["jobs"].return_value = {_full(rig, "fix/1")}
    out = actions.handle_spawn(rig.ctx(), _m("fix/1", mid="a2"))
    assert out == Outcome("refused", "container `fix-1` is already being created")
    seams["start"].assert_not_called()


def test_queued_spawn_refused_while_same_child_is_being_created(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    rig = Rig(tmp_path, limit=3)
    _queue(rig, ["fix/1"], kind="new")
    seams["jobs"].return_value = {_full(rig, "fix/1")}
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out == Outcome("refused", "container `fix-1` is already being created")
    seams["start"].assert_not_called()


def test_restart_refused_while_child_is_being_created(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration.protocol import RestartManifest

    rig = Rig(tmp_path, limit=3)
    full = _full(rig, "fix/1")
    rig.children = [
        Node(full=full, short="fix-1", running=False, config={labels.SPAWNED_BY: "proj-orch"})
    ]
    seams["jobs"].return_value = {full}
    boot = MagicMock()
    monkeypatch.setattr(actions.lifecycle, "boot_container", boot)
    m = RestartManifest(version=1, id="r1", type="restart", child="fix-1", prompt="again")
    out = actions.handle_restart(rig.ctx(), m)
    assert out == Outcome("refused", "child `fix-1` is still being created")
    _queue_rights(rig, kind="restart")
    ((_, queued_out),) = actions.drain_queue(rig.ctx())
    assert queued_out == Outcome("refused", "child `fix-1` is still being created")
    boot.assert_not_called()


def test_drain_persists_each_pop_before_starting(
    tmp_path: Path, seams: dict[str, MagicMock]
) -> None:
    """At most once: a crash inside the starter must not start the request again."""
    rig = Rig(tmp_path, limit=3)
    _queue(rig, ["fix/1", "fix/2"])
    seen: list[list[str]] = []

    def start(ctx: object, plan: object, *, fork: object) -> Outcome:
        seen.append([r.manifest_id for r in load_queue(tmp_path).get("proj-orch", [])])
        raise RuntimeError("broker crashed mid-start")

    seams["start"].side_effect = start
    with pytest.raises(RuntimeError):
        actions.drain_queue(rig.ctx())
    assert seen == [["m1"]]
    assert [r.manifest_id for r in load_queue(tmp_path)["proj-orch"]] == ["m1"]


def test_handle_spawn_queues_the_runner(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration.runner import Runner

    monkeypatch.setattr(actions, "runner_problems", lambda *a, **k: [])
    rig = Rig(tmp_path, running=1)
    runner = Runner(agent="claude-jb", profile="grok")
    m = _m().model_copy(update={"runner": runner})
    assert actions.handle_spawn(rig.ctx(), m).state == "queued"
    (req,) = load_queue(rig.root)["proj-orch"]
    assert req.runner == {"agent": "claude-jb", "profile": "grok", "model": None}
    assert req.runner_set is True


def _queue_runner(rig: Rig, runner: dict[str, object] | None) -> None:
    req = QueuedRequest(
        manifest_id="m0",
        child_full=_full(rig, "fix/1"),
        kind="new",
        commit=None,
        queued_at="t",
        branch="fix/1",
        base=None,
        prompt="p",
        rights=ChildRights().model_dump(mode="json"),
        runner=runner,
        runner_set=runner is not None,
    )
    save_queue(rig.root, {"proj-orch": [req]})


def test_queued_spawn_rechecks_runner(tmp_path: Path, seams: dict[str, MagicMock]) -> None:
    """The runner was fine at queue time; its LiteLLM profile is gone at dequeue."""
    rig = Rig(tmp_path)
    _queue_runner(rig, {"agent": "claude-jb", "profile": "gone", "model": None})
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out.state == "refused"
    assert "agent `claude` is not enabled in this repo" in out.detail
    seams["start"].assert_not_called()


def test_queued_spawn_with_valid_runner_carries_it(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration.runner import Runner

    monkeypatch.setattr(actions, "runner_problems", lambda *a, **k: [])
    rig = Rig(tmp_path)
    _queue_runner(rig, {"agent": "claude-jb", "profile": "grok", "model": None})
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out.state == "started"
    plan = seams["start"].call_args.args[1]
    assert plan.runner == Runner(agent="claude-jb", profile="grok")


def test_old_queue_entries_load_without_runner(tmp_path: Path) -> None:
    import json

    entry = {
        "manifest_id": "m0",
        "child_full": "proj-fix-1",
        "kind": "new",
        "commit": None,
        "queued_at": "t",
        "branch": "fix/1",
        "base": None,
        "prompt": "p",
        "rights": {},
    }
    (tmp_path / "queue.json").write_text(
        json.dumps({"version": 1, "queues": {"proj-orch": [entry]}})
    )
    (req,) = load_queue(tmp_path)["proj-orch"]
    assert req.runner is None and req.runner_set is False


def test_queued_restart_carries_runner_and_rechecks_it(
    tmp_path: Path, seams: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued with a runner whose LiteLLM profile is served; gone at dequeue."""
    from jailbee.config.models_litellm import LiteLLMConfig, LiteLLMRepoView
    from jailbee.orchestration.protocol import RestartManifest
    from tests.conftest import with_agent

    rig = Rig(tmp_path, running=1)
    cfg = make_config(tmp_path, agents={"claude": {"enabled": True}})
    cfg = with_agent(cfg, "claude", prompt_args="-p")
    cfg._litellm_view = LiteLLMRepoView(config=LiteLLMConfig(enabled=True))
    rig.cfg = cfg.model_copy(update={"container_prefix": "proj"})
    served = {"json": {"default_profile": "grok", "profiles": {"grok": {}}}}
    payload = MagicMock(return_value=served)
    monkeypatch.setattr("jailbee.orchestration.runner.litellm.container_sync_payload", payload)
    child = Node("proj-c1", "c1", False, {labels.SPAWNED_BY: "proj-orch"})
    rig.children.append(child)
    m = RestartManifest.model_validate(
        {
            "version": 1,
            "id": "r1",
            "type": "restart",
            "child": "c1",
            "prompt": "go",
            "runner": {"agent": "claude-jb", "profile": "grok"},
        }
    )
    assert actions.handle_restart(rig.ctx(), m).state == "queued"
    (req,) = load_queue(rig.root)["proj-orch"]
    assert req.runner == {"agent": "claude-jb", "profile": "grok", "model": None}
    assert req.runner_set is True
    # A null runner is queued as "set to nothing", distinct from absent.
    save_queue(rig.root, {})
    null = RestartManifest.model_validate({**m.model_dump(exclude={"runner"}), "runner": None})
    assert actions.handle_restart(rig.ctx(), null).state == "queued"
    (req2,) = load_queue(rig.root)["proj-orch"]
    assert req2.runner is None and req2.runner_set is True
    save_queue(rig.root, {"proj-orch": [req]})
    # At dequeue the profile is gone: refused, labels untouched, no boot.
    payload.return_value = {"json": {"default_profile": None, "profiles": {}}}
    rig.children[0] = Node("proj-c0", "c0", False, {labels.SPAWNED_BY: "proj-orch"})
    ((_, out),) = actions.drain_queue(rig.ctx())
    assert out.state == "refused"
    assert "LiteLLM profile `grok` is not served" in out.detail
    rig.incus.config_set.assert_not_called()
    rig.incus.config_unset_checked.assert_not_called()
