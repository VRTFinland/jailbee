"""Broker state files: the request queue and per-recipient pending directories."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from jailbee.db import state_dir

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

log = logging.getLogger(__name__)

RESERVATIONS_FILE = "reservations.json"
QUEUE_FILE = "queue.json"
WATCH_FILE = "watch.json"
_QUEUE_VERSION = 1


@dataclass(frozen=True)
class QueuedRequest:
    """A spawn request waiting for a free slot; `rights` is `ChildRights` as JSON."""

    manifest_id: str
    child_full: str
    kind: Literal["fork", "new", "pr", "restart"]
    commit: str | None
    queued_at: str
    branch: str
    base: str | None
    prompt: str
    rights: dict[str, object]
    # `Runner` as JSON; defaults keep queue files written before runners loadable.
    runner: dict[str, object] | None = None
    runner_set: bool = False
    on_done: str | None = None
    ref: str | None = None
    pr: int | None = None
    pr_base: str | None = None
    fork_base: str | None = None
    fork_base_pinned: bool = False


@dataclass(frozen=True)
class Reservation:
    """A broker-started create, independent of its best-effort job row."""

    child_full: str
    pid: int
    started_at: str


def load_reservations(root: Path) -> dict[str, list[Reservation]]:
    """Load create reservations; missing/corrupt files yield an empty store."""
    path = root / RESERVATIONS_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        groups = raw["reservations"]
        if not isinstance(groups, dict):
            return {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("ignoring unreadable orchestration reservations %s: %s", path, exc)
        return {}
    result: dict[str, list[Reservation]] = {}
    for recipient, items in groups.items():
        if not isinstance(items, list):
            continue
        entries = []
        for item in items:
            try:
                entry = Reservation(**item)
            except TypeError:
                continue
            if (
                isinstance(entry.child_full, str)
                and type(entry.pid) is int
                and isinstance(entry.started_at, str)
            ):
                entries.append(entry)
        if entries:
            result[recipient] = entries
    return result


def save_reservations(root: Path, reservations: dict[str, list[Reservation]]) -> None:
    """Atomically persist reservations, omitting empty lists."""
    payload = {
        "version": 1,
        "reservations": {k: [asdict(r) for r in v] for k, v in reservations.items() if v},
    }
    atomic_write_text(root / RESERVATIONS_FILE, json.dumps(payload, indent=2) + "\n")


def reservation_alive(pid: int) -> bool:
    """Probe a create worker, reaping exited workers parented by this broker."""
    from jailbee import background

    if pid <= 0:
        return False
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return background.worker_alive(pid)
    except OverflowError:
        return False  # Corrupt state cannot name a process outside pid_t's range.
    return waited == 0


def state_root() -> Path:
    """Return the orchestration state directory, creating it on demand."""
    root = state_dir() / "orchestration"
    root.mkdir(parents=True, exist_ok=True)
    return root


def pending_dir(root: Path, recipient_full: str) -> Path:
    """Return the directory holding messages not yet delivered to `recipient_full`."""
    return root / "pending" / recipient_full


def purge_pending(root: Path, recipient_full: str) -> None:
    """Drop everything parked for `recipient_full` (it is gone)."""
    shutil.rmtree(pending_dir(root, recipient_full), ignore_errors=True)


def purge_vanished_pending(root: Path, present: Collection[str]) -> list[str]:
    """Drop the pending directories of containers not in `present`; return their names."""
    base = root / "pending"
    if not base.is_dir():
        return []
    gone = sorted(p.name for p in base.iterdir() if p.is_dir() and p.name not in present)
    for name in gone:
        purge_pending(root, name)
    return gone


def atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file in the same directory and `os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_queue(root: Path) -> dict[str, list[QueuedRequest]]:
    """Load the queue; a missing or corrupt file yields an empty queue."""
    path = root / QUEUE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable orchestration queue %s: %s", path, exc)
        return {}
    try:
        return {
            recipient: [QueuedRequest(**item) for item in items]
            for recipient, items in raw["queues"].items()
            if items
        }
    except (KeyError, TypeError, AttributeError) as exc:
        log.warning("ignoring corrupt orchestration queue %s: %s", path, exc)
        return {}


def save_queue(root: Path, queues: dict[str, list[QueuedRequest]]) -> None:
    """Atomically persist the queue, omitting empty lists."""
    payload = {
        "version": _QUEUE_VERSION,
        "queues": {k: [asdict(r) for r in v] for k, v in queues.items() if v},
    }
    atomic_write_text(root / QUEUE_FILE, json.dumps(payload, indent=2) + "\n")
