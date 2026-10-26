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
    assert json.loads((tmp_path / state.RESERVATIONS_FILE).read_text())["version"] == 1


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
    data = json.loads((tmp_path / "queue.json").read_text())
    assert data["version"] == 1


def test_missing_queue_is_empty(tmp_path: Path) -> None:
    assert load_queue(tmp_path) == {}


def test_corrupt_queue_is_empty_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "queue.json").write_text("{not json")
    with caplog.at_level(logging.WARNING):
        assert load_queue(tmp_path) == {}
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_empty_queue_omitted(tmp_path: Path) -> None:
    save_queue(tmp_path, {"proj-a": [], "proj-b": [_req()]})
    data = json.loads((tmp_path / "queue.json").read_text())
    assert list(data["queues"]) == ["proj-b"]
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
