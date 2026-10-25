from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import pytest

from jailbee import background
from jailbee.db.models import JOB_AUTOSTART, JOB_BOOT, JOB_CREATE, JOB_DESTROY, JOB_STOP
from jailbee.orchestration import labels, watch
from jailbee.orchestration.actions import Node, Outcome
from jailbee.orchestration.watch import (
    DROP,
    WATCH_GRACE_S,
    JobSnapshot,
    WatchEntry,
    load_watch,
    read_jobs,
    save_watch,
    verdict,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC)
SINCE = NOW - timedelta(seconds=300)
ORCH = "proj-orch"


def entry(kind: Literal["spawn", "restart"] = "spawn", since: datetime = SINCE) -> WatchEntry:
    return WatchEntry("m1", "proj-kid", kind, since.isoformat())


def job(
    phase: str = "deps",
    *,
    pid: int = 4242,
    op_kind: str = JOB_CREATE,
    error_msg: str | None = None,
    started_at: datetime = NOW - timedelta(seconds=10),
) -> JobSnapshot:
    return JobSnapshot(phase, pid, op_kind, error_msg, started_at)


def child(owner: str = ORCH) -> Node:
    return Node("proj-kid", "kid", True, {labels.SPAWNED_BY: owner})


def run(
    e: WatchEntry, j: JobSnapshot | None, c: Node | None, now: datetime = NOW
) -> Outcome | None:
    return verdict(e, j, c, orchestrator_full=ORCH, short="kid", now=now)


@pytest.fixture
def alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(background, "worker_alive", lambda pid: pid == 4242)


def test_live_worker_waits(alive: None) -> None:
    assert run(entry(), job("clone"), None) is None
    assert run(entry("restart"), job("autostart", op_kind=JOB_BOOT), child()) is None


def test_failed_row_spawn_child_removed(alive: None) -> None:
    out = run(entry(), job("failed", error_msg="boom"), None)
    assert out is not None
    assert out.state == "failed"
    assert out.detail == "creation of child `kid` failed: boom — the child was removed"


def test_failed_row_spawn_child_present(alive: None) -> None:
    out = run(entry(), job("failed", error_msg="boom"), child())
    assert out.detail == "creation of child `kid` failed: boom"


def test_failed_row_restart(alive: None) -> None:
    out = run(entry("restart"), job("failed", op_kind=JOB_BOOT, error_msg="nope"), child())
    assert out.state == "failed"
    assert out.detail == "restart of child `kid` failed: nope"


def test_failed_row_without_error_msg_says_see_the_worker_log(alive: None) -> None:
    out = run(entry(), job("failed"), child())
    assert out.detail == "creation of child `kid` failed: see the worker log"


def test_dead_worker_reports_phase(alive: None) -> None:
    out = run(entry(), job("deps", pid=999), child())
    assert out.state == "failed"
    assert out.detail == "creation of child `kid` stopped unexpectedly during deps"
    out = run(entry("restart"), job("autostart", pid=999, op_kind=JOB_AUTOSTART), child())
    assert out.detail == "restart of child `kid` stopped unexpectedly during autostart"


def test_no_row_child_present_spawn_is_ready() -> None:
    out = run(entry(), None, child())
    assert out.state == "done"
    assert out.detail == "child `kid` is ready"


def test_no_row_child_present_restart_drops() -> None:
    assert run(entry("restart"), None, child()) is DROP


def test_no_row_child_owned_by_other_orchestrator_is_not_ready() -> None:
    fresh = entry(since=NOW - timedelta(seconds=5))
    assert run(fresh, None, child("proj-other")) is None
    out = run(entry(), None, child("proj-other"))
    assert out is not None
    assert out.state == "failed"


def test_no_row_no_child_waits_within_grace() -> None:
    e = entry(since=NOW - timedelta(seconds=WATCH_GRACE_S - 1))
    assert run(e, None, None) is None


def test_no_row_no_child_fails_after_grace() -> None:
    e = entry(since=NOW - timedelta(seconds=WATCH_GRACE_S + 1))
    out = run(e, None, None)
    assert out == Outcome("failed", "creation of child `kid` ended without a container")


def test_no_row_no_child_restart_drops() -> None:
    assert run(entry("restart"), None, None) is DROP


@pytest.mark.parametrize("kind", [JOB_STOP, JOB_DESTROY])
def test_stop_or_destroy_row_counts_as_absent(alive: None, kind: str) -> None:
    assert run(entry(), job("failed", op_kind=kind), child()) == Outcome(
        "done", "child `kid` is ready"
    )
    assert run(entry("restart"), job("starting", op_kind=kind), child()) is DROP


def test_old_failed_row_before_since_is_ignored(alive: None) -> None:
    old = job("failed", error_msg="old", started_at=SINCE - timedelta(seconds=1))
    fresh = entry(since=NOW - timedelta(seconds=5))
    assert run(fresh, old, None) is None
    assert run(entry(), old, child()) == Outcome("done", "child `kid` is ready")
    old_dead = job("deps", pid=999, started_at=SINCE - timedelta(seconds=1))
    assert run(fresh, old_dead, None) is None


def test_read_jobs_returns_none_when_engine_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> object:
        raise RuntimeError("locked")

    monkeypatch.setattr("jailbee.db.get_engine", boom)
    assert read_jobs() is None


def test_read_jobs_snapshots_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlmodel import Session, SQLModel, create_engine

    from jailbee.db.models import BackgroundJob

    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BackgroundJob(
                container_name="proj-kid",
                container_prefix="proj",
                phase="failed",
                pid=7,
                log_path="/x",
                error_msg="bad",
                op_kind=JOB_CREATE,
                started_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
    monkeypatch.setattr("jailbee.db.get_engine", lambda: engine)
    assert read_jobs() == {"proj-kid": JobSnapshot("failed", 7, JOB_CREATE, "bad", NOW)}
    engine.dispose()


def test_watch_roundtrip_and_corrupt_file(tmp_path: Path) -> None:
    assert load_watch(tmp_path) == {}
    w = {ORCH: [entry(), entry("restart")], "empty": []}
    save_watch(tmp_path, w)
    assert load_watch(tmp_path) == {ORCH: w[ORCH]}
    (tmp_path / "watch.json").write_text("{not json")
    assert load_watch(tmp_path) == {}
    (tmp_path / "watch.json").write_text('{"watches": {"a": [{"bogus": 1}]}}')
    assert load_watch(tmp_path) == {}


def test_load_watch_drops_only_invalid_entries(tmp_path: Path) -> None:
    good = entry()
    bad = [
        {"ref": "r", "child_full": "c", "kind": "bogus", "since": NOW.isoformat()},
        {"ref": "r", "child_full": "c", "kind": "spawn", "since": "not a date"},
        {"ref": "r", "child_full": "c", "kind": "spawn", "since": "2026-10-25T12:00:00"},
        {"ref": 1, "child_full": "c", "kind": "spawn", "since": NOW.isoformat()},
        "junk",
    ]
    import json
    from dataclasses import asdict

    (tmp_path / "watch.json").write_text(
        json.dumps({"version": 1, "watches": {ORCH: [*bad, asdict(good)], "only-bad": bad}})
    )
    assert load_watch(tmp_path) == {ORCH: [good]}


def test_watch_file_name() -> None:
    from jailbee.orchestration import state

    assert state.WATCH_FILE == "watch.json"
    assert watch.WATCH_GRACE_S == 120
