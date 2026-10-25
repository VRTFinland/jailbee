from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

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


def test_corrupt_queue_is_empty_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
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
