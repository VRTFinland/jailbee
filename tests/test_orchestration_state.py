from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from jailbee import background
from jailbee.orchestration import state
from jailbee.orchestration.state import (
    QueuedRequest,
    atomic_write_text,
    load_queue,
    pending_dir,
    save_queue,
    state_root,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_reservations_round_trip(tmp_path: Path) -> None:
    reservations = {"proj-orch": [state.Reservation("proj-a", 123, "2026-10-26T00:00:00+00:00")]}
    state.save_reservations(tmp_path, reservations)
    assert state.load_reservations(tmp_path) == reservations
    assert not (tmp_path / state.RESERVATIONS_FILE).exists()


def test_save_reservations_omits_empty(tmp_path: Path) -> None:
    state.save_reservations(tmp_path, {"proj-orch": []})
    assert state.load_reservations(tmp_path) == {}


def test_load_reservations_missing_is_empty(tmp_path: Path) -> None:
    assert state.load_reservations(tmp_path) == {}


def test_load_reservations_corrupt_is_empty(tmp_path: Path) -> None:
    (tmp_path / state.RESERVATIONS_FILE).write_text("{not json")
    assert state.load_reservations(tmp_path) == {}


def test_load_reservations_skips_malformed_entries(tmp_path: Path) -> None:
    (tmp_path / state.RESERVATIONS_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "reservations": {
                    "proj-orch": [
                        {"child_full": "proj-a", "pid": 123, "started_at": "t"},
                        {"child_full": "proj-b"},
                        {"child_full": "proj-c", "pid": "bad", "started_at": "t"},
                    ]
                },
            }
        )
    )
    assert state.load_reservations(tmp_path) == {
        "proj-orch": [state.Reservation("proj-a", 123, "t")]
    }


def test_reservation_reaps_exited_child() -> None:
    proc = subprocess.Popen(["true"])
    try:
        # Observe exit without reaping: reservation_alive must reap the zombie.
        os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
        assert state.reservation_alive(proc.pid) is False
    finally:
        proc.wait()


def test_reservation_live_child() -> None:
    proc = subprocess.Popen(["sleep", "5"])
    try:
        assert state.reservation_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait()


def test_reservation_foreign_pid_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "waitpid", MagicMock(side_effect=ChildProcessError))
    monkeypatch.setattr(background, "worker_alive", lambda pid: True)
    assert state.reservation_alive(4242) is True


def test_oversized_reservation_pid_is_pruned(tmp_path: Path) -> None:
    from jailbee.orchestration.actions import prune_reservations

    state.save_reservations(tmp_path, {"proj-orch": [state.Reservation("proj-a", 10**100, "t")]})
    prune_reservations(tmp_path)
    assert state.load_reservations(tmp_path) == {}


def test_reservation_pid_zero_dead() -> None:
    assert state.reservation_alive(0) is False


def _req(manifest_id: str = "m1") -> QueuedRequest:
    return QueuedRequest(
        manifest_id=manifest_id,
        child_full="proj-child",
        kind="fork",
        commit=None,
        queued_at="2026-10-25T10:00:00Z",
        branch="feat",
        base="main",
        prompt="do it",
        rights={"spawn": False},
    )


def test_queue_roundtrip(tmp_path: Path) -> None:
    queues = {"proj-a": [_req("m1"), _req("m2")], "proj-b": [_req("m3")]}
    save_queue(tmp_path, queues)
    assert load_queue(tmp_path) == queues
    assert not (tmp_path / "queue.json").exists()


def test_queue_warm_paths_and_legacy(tmp_path):
    from dataclasses import asdict, replace

    legacy = asdict(_req())
    del legacy["warm_paths"]
    (tmp_path / "queue.json").write_text(json.dumps({"queues": {"proj-a": [legacy]}}))
    assert load_queue(tmp_path)["proj-a"][0].warm_paths is None
    save_queue(tmp_path, {"proj-a": [replace(_req(), warm_paths=["*/build"])]})
    assert load_queue(tmp_path)["proj-a"][0].warm_paths == ["*/build"]
    assert (tmp_path / "queue.json.migrated").exists()


def test_save_before_load_never_reimports_legacy(tmp_path):
    from dataclasses import asdict

    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"queues": {"old": [asdict(_req())]}}))
    save_queue(tmp_path, {"new": [_req()]})
    path.write_text("not json")
    assert list(load_queue(tmp_path)) == ["new"]
    assert not path.exists()


def test_invalid_document_is_quarantined(tmp_path, caplog):
    path = tmp_path / "reservations.json"
    path.write_text('{"reservations": []}')
    assert state.load_reservations(tmp_path) == {}
    assert (tmp_path / "reservations.json.corrupt").exists()
    assert caplog.records


def test_missing_queue_is_empty(tmp_path: Path) -> None:
    assert load_queue(tmp_path) == {}


def test_corrupt_queue_is_empty_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "queue.json").write_text("{not json")
    with caplog.at_level(logging.WARNING):
        assert load_queue(tmp_path) == {}
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_empty_queue_omitted(tmp_path: Path) -> None:
    save_queue(tmp_path, {"proj-a": [], "proj-b": [_req()]})
    assert list(load_queue(tmp_path)) == ["proj-b"]


def test_atomic_write_leaves_no_temp_on_success(tmp_path: Path) -> None:
    target = tmp_path / "f.txt"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"
    assert list(tmp_path.iterdir()) == [target]


def test_pending_dir_under_root(tmp_path: Path) -> None:
    assert pending_dir(tmp_path, "proj-a").parent.parent == tmp_path


def test_state_root_created_under_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    root = state_root()
    assert root == tmp_path / "jailbee" / "orchestration"
    assert root.is_dir()


def test_unreadable_legacy_reservations_warns_and_starts_empty(tmp_path, monkeypatch, caplog):
    from pathlib import Path

    path = tmp_path / state.RESERVATIONS_FILE
    path.write_text(json.dumps({"reservations": {"orch": [
        {"child_full": "a", "pid": 1, "started_at": "t"}
    ]}}))
    original = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert state.load_reservations(tmp_path) == {}
    assert "denied" in caplog.text
    assert path.exists()
    monkeypatch.setattr(Path, "read_text", original)
    assert state.load_reservations(tmp_path) == {"orch": [state.Reservation("a", 1, "t")]}
    assert (tmp_path / "reservations.json.migrated").exists()


def test_unreadable_legacy_blocks_save_until_import_succeeds(tmp_path, monkeypatch):
    from pathlib import Path

    import pytest

    from jailbee.orchestration.store import StoreError

    path = tmp_path / state.RESERVATIONS_FILE
    path.write_text(json.dumps({"reservations": {"orch": [
        {"child_full": "a", "pid": 1, "started_at": "t"}
    ]}}))
    original = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert state.load_reservations(tmp_path) == {}
    with pytest.raises(StoreError, match="legacy"):
        state.save_reservations(tmp_path, {})
    assert path.exists()
    monkeypatch.setattr(Path, "read_text", original)
    assert state.load_reservations(tmp_path) == {"orch": [state.Reservation("a", 1, "t")]}
    assert (tmp_path / "reservations.json.migrated").exists()


def test_failed_read_requires_reload_before_saving_even_after_recovery(tmp_path, monkeypatch):
    from pathlib import Path

    import pytest

    from jailbee.orchestration.store import StoreError

    path = tmp_path / state.RESERVATIONS_FILE
    path.write_text(json.dumps({"reservations": {"orch": [
        {"child_full": "a", "pid": 1, "started_at": "t"}
    ]}}))
    original = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == path:
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert state.load_reservations(tmp_path) == {}
    monkeypatch.setattr(Path, "read_text", original)
    with pytest.raises(StoreError, match="legacy"):
        state.save_reservations(tmp_path, {})
    assert state.load_reservations(tmp_path) == {"orch": [state.Reservation("a", 1, "t")]}
    state.save_reservations(tmp_path, {})
    assert state.load_reservations(tmp_path) == {}


def test_reservations_import_once_and_ignore_recreated_source(tmp_path):
    path = tmp_path / state.RESERVATIONS_FILE
    path.write_text(json.dumps({"reservations": {"orch": [{"child_full": "a", "pid": 1, "started_at": "t"}]}}))
    expected = {"orch": [state.Reservation("a", 1, "t")]}
    assert state.load_reservations(tmp_path) == expected
    assert (tmp_path / "reservations.json.migrated").exists()
    path.write_text("bad")
    assert state.load_reservations(tmp_path) == expected


def test_missing_source_marks_import_complete(tmp_path):
    assert load_queue(tmp_path) == {}
    (tmp_path / "queue.json").write_text("bad")
    assert load_queue(tmp_path) == {}
    assert (tmp_path / "queue.json.migrated").exists()


def test_corrupt_syntax_quarantined(tmp_path):
    (tmp_path / "queue.json").write_text("{")
    assert load_queue(tmp_path) == {}
    assert (tmp_path / "queue.json.corrupt").read_text() == "{"


def test_failed_rename_after_commit_retried_without_reimport(tmp_path, monkeypatch):
    from pathlib import Path
    from dataclasses import asdict

    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"queues": {"orch": [asdict(_req())]}}))
    original = Path.replace
    calls = []

    def fail_once(self, target):
        if self == path and not calls:
            calls.append(1)
            raise PermissionError("denied")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", fail_once)
    assert load_queue(tmp_path) == {"orch": [_req()]}
    path.write_text("invalid")
    assert load_queue(tmp_path) == {"orch": [_req()]}
    assert (tmp_path / "queue.json.migrated").read_text() == "invalid"


def test_quarantine_failure_does_not_abort(tmp_path, monkeypatch, caplog):
    from pathlib import Path

    path = tmp_path / "queue.json"
    path.write_text("{")
    original = Path.replace

    def denied(self, target):
        if self == path:
            raise PermissionError("denied")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", denied)
    assert load_queue(tmp_path) == {}
    assert path.exists()
    assert "denied" in caplog.text
