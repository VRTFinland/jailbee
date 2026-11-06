"""Host-side teardown of a destroyed container's leftovers (issue #78)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from sqlmodel import Session

from jailbee import background, host_teardown
from jailbee.db import get_engine, state_dir
from jailbee.orchestration import state as ostate
from jailbee.outbox_io import ContainerIdentity, JournalKey, JournalStore

CREATED = "2026-11-01T10:00:00Z"
NEW_CREATED = "2026-11-05T10:00:00Z"


def _incus(name: str, created: str = CREATED) -> MagicMock:
    incus = MagicMock()
    incus.list_containers.return_value = [
        {"name": name, "status": "Stopped", "created_at": created},
        {"name": name + "-bar", "status": "Stopped", "created_at": created},
    ]
    return incus


def _job(name: str, log: str, kind: str = "create") -> None:
    with Session(get_engine()) as s:
        background.start_job(
            s,
            container_name=name,
            container_prefix="p",
            branch="b",
            pid=1,
            log_path=log,
            now=datetime.now(UTC),
        )
        if kind != "create":
            row = s.get(background.BackgroundJob, name)
            assert row is not None
            row.op_kind = kind
            s.add(row)
            s.commit()


def _logs() -> object:
    d = state_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _touch_logs(name: str, stamp: str = "20261101-100000") -> list:
    d = _logs()
    paths = [
        d / f"{name}-{stamp}.log",
        d / f"{name}-{stamp}.progress.json",
        d / f"{name}-{stamp}.job.json",
    ]
    for p in paths:
        p.write_text("x")
    return paths


def test_job_logs_removed_exactly_and_overlapping_name_survives() -> None:
    mine = _touch_logs("foo")
    other = _touch_logs("foo-bar")
    _job("foo", str(mine[0]))
    _job("foo-bar", str(other[0]))
    snap = host_teardown.snapshot(_incus("foo"), "foo")
    host_teardown.cleanup(snap)
    assert not any(p.exists() for p in mine)
    assert all(p.exists() for p in other)


def test_cleanup_is_idempotent() -> None:
    mine = _touch_logs("foo")
    _job("foo", str(mine[0]))
    snap = host_teardown.snapshot(_incus("foo"), "foo")
    host_teardown.cleanup(snap)
    host_teardown.cleanup(snap)
    assert not mine[0].exists()


def test_log_outside_state_logs_dir_or_foreign_name_is_never_removed(tmp_path) -> None:
    stray = tmp_path / "foo-20261101-100000.log"
    stray.write_text("x")
    foreign = _touch_logs("zzz")
    _job("foo", str(stray))
    host_teardown.cleanup(host_teardown.snapshot(_incus("foo"), "foo"))
    assert stray.exists()
    _job("foo", str(foreign[0]))
    host_teardown.cleanup(host_teardown.snapshot(_incus("foo"), "foo"))
    assert foreign[0].exists()


def test_destroy_job_own_log_is_kept() -> None:
    mine = _touch_logs("foo")
    _job("foo", str(mine[0]), kind="destroy")
    host_teardown.cleanup(host_teardown.snapshot(_incus("foo"), "foo"))
    assert mine[0].exists()


def _req(child: str, kind: str = "restart") -> ostate.QueuedRequest:
    return ostate.QueuedRequest(
        manifest_id="m",
        child_full=child,
        kind=kind,  # type: ignore[arg-type]
        commit=None,
        queued_at="t",
        branch="b",
        base=None,
        prompt="",
        rights={},
    )


def test_queue_entries_for_exact_child_only() -> None:
    root = ostate.state_root()
    ostate.save_queue(
        root,
        {
            "orch": [_req("foo"), _req("foo-bar"), _req("foo", kind="new")],
            "foo": [_req("x")],
            "other": [_req("foo-bar")],
        },
    )
    ostate.pending_dir(root, "foo").mkdir(parents=True)
    (ostate.pending_dir(root, "foo") / "m.json").write_text("{}")
    ostate.pending_dir(root, "foo-bar").mkdir(parents=True)
    host_teardown.cleanup(host_teardown.snapshot(_incus("foo"), "foo"))
    queues = ostate.load_queue(root)
    # restart of the gone child dropped; new-kind (a future incarnation) and
    # overlapping names kept; the destroyed orchestrator's own queue dropped.
    assert [(r.child_full, r.kind) for r in queues["orch"]] == [
        ("foo-bar", "restart"),
        ("foo", "new"),
    ]
    assert "foo" not in queues
    assert [r.child_full for r in queues["other"]] == ["foo-bar"]
    assert not ostate.pending_dir(root, "foo").exists()
    assert ostate.pending_dir(root, "foo-bar").exists()


def test_reservations_dead_dropped_live_kept(monkeypatch) -> None:
    root = ostate.state_root()
    ostate.save_reservations(
        root,
        {
            "orch": [
                ostate.Reservation("foo", 111, "t"),
                ostate.Reservation("foo", 222, "t"),
                ostate.Reservation("foo-bar", 111, "t"),
            ]
        },
    )
    monkeypatch.setattr(ostate, "reservation_alive", lambda pid: pid == 222)
    host_teardown.cleanup(host_teardown.snapshot(_incus("foo"), "foo"))
    kept = ostate.load_reservations(root)["orch"]
    assert [(r.child_full, r.pid) for r in kept] == [("foo", 222), ("foo-bar", 111)]


def _journal(name: str, created: str, manifest: str, *, uncertain: bool) -> JournalStore:
    store = JournalStore()
    key = JournalKey(ContainerIdentity(name, created), manifest)
    store.create(key, "a" * 64, 1)
    store.mark_prepared(key, 0, repo="o/r")
    # A journal left "prepared" is a publication with an unknown outcome.
    if not uncertain:
        store.mark_applied(key, 0, repo="o/r", url="https://github.com/o/r/issues/1", issue=1)
    return store


def _active(store: JournalStore, name: str, created: str) -> list:
    d = store.root / JournalStore._container_directory(ContainerIdentity(name, created))
    return sorted(p.name for p in d.glob("*.json")) if d.exists() else []


def test_certain_journal_archived_uncertain_receipt_kept() -> None:
    store = _journal("foo", CREATED, "ok.json", uncertain=False)
    _journal("foo", CREATED, "unsure.json", uncertain=True)
    host_teardown.cleanup(host_teardown.snapshot(_incus("foo"), "foo"))
    remaining = _active(store, "foo", CREATED)
    assert len(remaining) == 1  # only the uncertain one is still active
    d = store.root / JournalStore._container_directory(ContainerIdentity("foo", CREATED))
    assert len(list((d / "archive").glob("*.json"))) == 1


def test_other_incarnation_and_overlapping_name_journals_untouched() -> None:
    store = _journal("foo", NEW_CREATED, "m.json", uncertain=False)
    _journal("foo-bar", CREATED, "m.json", uncertain=False)
    _journal("foo", CREATED, "m.json", uncertain=False)
    host_teardown.cleanup(host_teardown.snapshot(_incus("foo"), "foo"))
    assert len(_active(store, "foo", CREATED)) == 0
    assert len(_active(store, "foo", NEW_CREATED)) == 1
    assert len(_active(store, "foo-bar", CREATED)) == 1


def test_pr_lock_file_removed_for_exact_identity() -> None:
    import hashlib

    locks = state_dir() / "pr-outbox" / "locks"
    locks.mkdir(parents=True)

    def lock(name: str, created: str):
        p = locks / (hashlib.sha256(f"{name}\0{created}".encode()).hexdigest() + ".lock")
        p.write_text("")
        return p

    mine, newer, bar = lock("foo", CREATED), lock("foo", NEW_CREATED), lock("foo-bar", CREATED)
    host_teardown.cleanup(host_teardown.snapshot(_incus("foo"), "foo"))
    assert not mine.exists()
    assert newer.exists()
    assert bar.exists()


def test_no_identity_skips_outbox_and_keeps_going(capsys) -> None:
    store = _journal("foo", CREATED, "m.json", uncertain=False)
    mine = _touch_logs("foo")
    _job("foo", str(mine[0]))
    incus = MagicMock()
    incus.list_containers.return_value = []
    host_teardown.cleanup(host_teardown.snapshot(incus, "foo"))
    assert not mine[0].exists()
    assert len(_active(store, "foo", CREATED)) == 1


def test_step_failure_warns_and_later_steps_still_run(monkeypatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(host_teardown, "warn", warnings.append)
    mine = _touch_logs("foo")
    _job("foo", str(mine[0]))
    root = ostate.state_root()
    ostate.save_queue(root, {"orch": [_req("foo")]})

    def boom(*a, **k):
        raise OSError("disk says no")

    monkeypatch.setattr(ostate, "save_queue", boom)
    snap = host_teardown.snapshot(_incus("foo"), "foo")
    host_teardown.cleanup(snap)
    assert any("orchestration" in w and "disk says no" in w for w in warnings)
    assert not mine[0].exists()


def test_snapshot_never_raises_when_incus_or_db_fail() -> None:
    incus = MagicMock()
    incus.list_containers.side_effect = RuntimeError("daemon down")
    snap = host_teardown.snapshot(incus, "foo")
    assert snap.identity is None
    host_teardown.cleanup(snap)


def test_corrupt_queue_file_left_alone() -> None:
    root = ostate.state_root()
    (root / "queue.json").write_text("{not json")
    host_teardown.cleanup(host_teardown.snapshot(_incus("foo"), "foo"))
    assert (root / "queue.json.corrupt").read_text() == "{not json"


# --- wiring through the two destroy paths ---------------------------------


def test_destroy_container_runs_teardown(make_cfg, tmp_path) -> None:
    from jailbee.lifecycle import destroy_container

    repo = tmp_path / "r"
    repo.mkdir()
    cfg = make_cfg(repo)
    name = f"{cfg.container_prefix}-feat"
    mine = _touch_logs(name)
    _job(name, str(mine[0]))
    incus = _incus(name)
    incus.exists.return_value = True
    destroy_container(cfg, incus, name, force=True)
    incus.delete.assert_called_once()
    assert not any(p.exists() for p in mine)


def test_destroy_container_teardown_failure_does_not_mask_success(
    make_cfg, tmp_path, monkeypatch
) -> None:
    from jailbee.lifecycle import destroy_container

    repo = tmp_path / "r"
    repo.mkdir()
    cfg = make_cfg(repo)
    name = f"{cfg.container_prefix}-feat"
    incus = _incus(name)
    incus.exists.return_value = True
    monkeypatch.setattr(
        host_teardown, "cleanup", MagicMock(side_effect=RuntimeError("teardown exploded"))
    )
    destroy_container(cfg, incus, name, force=True)
    incus.delete.assert_called_once()


def test_destroy_orphan_runs_teardown_without_repo() -> None:
    from jailbee import orphans

    mine = _touch_logs("gone-feat")
    _job("gone-feat", str(mine[0]))
    incus = _incus("gone-feat")
    orphans.destroy_orphan(incus, "gone-feat", force=True)
    incus.delete.assert_called_once()
    assert not any(p.exists() for p in mine)


def test_destroy_orphan_teardown_failure_does_not_mask_success(monkeypatch) -> None:
    from jailbee import orphans

    incus = _incus("gone-feat")
    monkeypatch.setattr(host_teardown, "cleanup", MagicMock(side_effect=RuntimeError("x")))
    orphans.destroy_orphan(incus, "gone-feat", force=True)
    incus.delete.assert_called_once()
