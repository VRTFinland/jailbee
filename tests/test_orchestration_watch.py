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


def test_interleaved_register_cancel_preserves_unrelated_entries(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    save_watch(tmp_path, {ORCH: [
        WatchEntry("keep", "keep", "restart", NOW.isoformat()),
        WatchEntry("remove", "remove", "restart", NOW.isoformat()),
    ]})
    barrier = Barrier(2)

    def register():
        barrier.wait()
        watch.register(tmp_path, ORCH, WatchEntry("new", "new", "restart", NOW.isoformat()))

    def cancel():
        barrier.wait()
        watch.cancel(tmp_path, ORCH, "remove")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(register), pool.submit(cancel)]
        for future in futures:
            future.result()
    assert {item.child_full for item in load_watch(tmp_path)[ORCH]} == {"keep", "new"}


def test_watch_migrates_legacy_document_once_and_never_writes_file(tmp_path: Path) -> None:
    import json
    from dataclasses import asdict

    legacy = {"version": 1, "watches": {ORCH: [asdict(entry())]}}
    (tmp_path / "watch.json").write_text(json.dumps(legacy))

    assert load_watch(tmp_path) == {ORCH: [entry()]}
    assert (tmp_path / "watch.json.migrated").exists()
    (tmp_path / "watch.json").write_text(json.dumps({"version": 1, "watches": {}}))
    assert load_watch(tmp_path) == {ORCH: [entry()]}

    watch.record_message(tmp_path, ORCH, "proj-kid", "progress", "working\nsecond line")
    updated = load_watch(tmp_path)[ORCH][0]
    assert updated.last_message_kind == "progress"
    assert updated.last_message_line == "working"
    save_watch(tmp_path, {ORCH: [updated]})
    assert load_watch(tmp_path) == {ORCH: [updated]}
    assert not (tmp_path / "watch.json").exists()


def test_watch_migration_quarantines_syntax_and_top_level_corruption(tmp_path: Path) -> None:
    (tmp_path / "watch.json").write_text("{not json")
    assert load_watch(tmp_path) == {}
    assert (tmp_path / "watch.json.corrupt").exists()

    other = tmp_path / "wrong-shape"
    other.mkdir()
    (other / "watch.json").write_text('{"version": 1, "watches": []}')
    assert load_watch(other) == {}
    assert (other / "watch.json.corrupt").exists()


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
    out = run(entry(), job("deps", pid=999), None)
    assert out.state == "failed"
    assert out.detail == (
        "creation of child `kid` stopped unexpectedly during deps — the child was removed"
    )
    out = run(entry("restart"), job("autostart", pid=999, op_kind=JOB_AUTOSTART), None)
    assert out.detail == "restart of child `kid` stopped unexpectedly during autostart"


def test_dead_worker_says_a_running_child_was_kept_and_needs_a_stop(alive: None) -> None:
    """Issue #71: a half-created child is left running, and `restart` refuses it."""
    dead = JobSnapshot("autostart", 999, JOB_CREATE, None, NOW, "/state/logs/proj-kid-1.log")
    out = run(entry(), dead, child())
    assert out == Outcome(
        "failed",
        "creation of child `kid` stopped unexpectedly during autostart — the child was "
        "kept running; `stop` it before a `restart`",
    )


def test_dead_worker_with_stopped_or_missing_child(alive: None) -> None:
    dead = job("clone", pid=999)
    stopped = Node("proj-kid", "kid", False, {labels.SPAWNED_BY: ORCH})
    assert run(entry(), dead, stopped).detail == (
        "creation of child `kid` stopped unexpectedly during clone — the child was kept, stopped"
    )
    assert run(entry(), dead, None).detail == (
        "creation of child `kid` stopped unexpectedly during clone — the child was removed"
    )


@pytest.mark.parametrize("phase", ["failed", "autostart"])
def test_status_detail_never_carries_the_host_log_path(alive: None, phase: str) -> None:
    """Host paths stay out of container-facing statuses; the broker logs them."""
    row = JobSnapshot(phase, 999, JOB_CREATE, None, NOW, "/state/logs/proj-kid-1.log")
    assert "/state/logs" not in run(entry(), row, child()).detail


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
    assert read_jobs() == {"proj-kid": JobSnapshot("failed", 7, JOB_CREATE, "bad", NOW, "/x")}
    engine.dispose()


def test_watch_roundtrip_and_invalid_entries(tmp_path: Path) -> None:
    assert load_watch(tmp_path) == {}
    w = {ORCH: [entry(), entry("restart")], "empty": []}
    save_watch(tmp_path, w)
    assert load_watch(tmp_path) == {ORCH: w[ORCH]}


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
