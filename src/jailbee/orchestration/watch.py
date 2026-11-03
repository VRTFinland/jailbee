"""Watch list for started children, and the verdict on each entry.

When the broker starts a child it may leave work running detached (the create
worker, a boot's autostart). A watch entry remembers that, so a later broker
round can tell the orchestrator whether the child came up. The verdict is a
pure function of the child's job row and container; reading the job database is
best effort and never raises.
"""

from __future__ import annotations

import fcntl
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from jailbee import background
from jailbee.db.models import JOB_AUTOSTART, JOB_BOOT, JOB_CREATE
from jailbee.orchestration import labels
from jailbee.orchestration.actions import Node, Outcome, done, failed
from jailbee.orchestration.state import WATCH_FILE, atomic_write_text

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

WATCH_GRACE_S = 120
_WATCH_VERSION = 1
_TRACKED_KINDS = frozenset({JOB_CREATE, JOB_AUTOSTART, JOB_BOOT})

# Returned by `verdict` for "remove the entry, send nothing". Callers test `is DROP`.
DROP = Outcome("done", "")


@dataclass(frozen=True)
class WatchEntry:
    """One started child the broker still owes a final status for."""

    ref: str
    child_full: str
    kind: Literal["spawn", "restart"]
    since: str  # ISO 8601, UTC
    supervise: bool = False  # Legacy watches only judged the container/job.
    ready_since: str | None = None
    live_since: str | None = None
    confirmed: bool = False
    agent_only: bool = False  # No boot job belongs to this run.
    idle_since: str | None = None
    idle_reported_at: str | None = None
    idle_token: str | None = None
    last_message_kind: str | None = None
    last_message_line: str | None = None


@dataclass(frozen=True)
class JobSnapshot:
    """The fields of a job row the verdict needs, detached from the session."""

    phase: str
    pid: int
    op_kind: str
    error_msg: str | None
    started_at: datetime
    log_path: str | None = None


def load_watch(root: Path) -> dict[str, list[WatchEntry]]:
    """Load the watch list; a missing or corrupt file yields an empty one."""
    path = root / WATCH_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable orchestration watch list %s: %s", path, exc)
        return {}
    try:
        items_by_orch = raw["watches"].items()
        result: dict[str, list[WatchEntry]] = {}
        for orchestrator, items in items_by_orch:
            entries = [e for e in (_parse_entry(item) for item in items) if e is not None]
            if entries:
                result[orchestrator] = entries
        return result
    except (KeyError, TypeError, AttributeError) as exc:
        log.warning("ignoring corrupt orchestration watch list %s: %s", path, exc)
        return {}


def _parse_entry(item: object) -> WatchEntry | None:
    """One valid entry, or None (logged) so a bad one cannot stall the others."""
    try:
        if not isinstance(item, dict):
            raise TypeError("entry is not an object")
        entry = WatchEntry(**item)
        if entry.kind not in ("spawn", "restart"):
            raise ValueError(f"unknown kind {entry.kind!r}")
        if not all(isinstance(v, str) for v in (entry.ref, entry.child_full, entry.since)):
            raise TypeError("non-string field")
        if datetime.fromisoformat(entry.since).tzinfo is None:
            raise ValueError("`since` is not timezone-aware")
    except (TypeError, ValueError) as exc:
        log.warning("dropping invalid orchestration watch entry %r: %s", item, exc)
        return None
    return entry


def save_watch(root: Path, watches: dict[str, list[WatchEntry]]) -> None:
    """Atomically persist the watch list, omitting empty lists."""
    payload = {
        "version": _WATCH_VERSION,
        "watches": {k: [asdict(e) for e in v] for k, v in watches.items() if v},
    }
    atomic_write_text(root / WATCH_FILE, json.dumps(payload, indent=2) + "\n")


@contextmanager
def mutation_locked(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "mutation.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def locked(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "watch.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def register(root: Path, orchestrator: str, entry: WatchEntry) -> None:
    """Persist before launching: a run must never start without durable supervision."""
    with locked(root):
        watches = load_watch(root)
        entries = [e for e in watches.get(orchestrator, []) if e.child_full != entry.child_full]
        entries.append(entry)
        watches[orchestrator] = entries
        save_watch(root, watches)


def record_message(root: Path, orchestrator: str, child_full: str, kind: str, body: str) -> None:
    """Accepted child traffic resets the idle episode, not the supervision."""
    with locked(root):
        watches = load_watch(root)
        lines = body.splitlines()
        line = "".join(ch for ch in (lines[0] if lines else "") if ch.isprintable())[:240]
        watches[orchestrator] = [
            replace(
                e,
                idle_since=None,
                idle_reported_at=None,
                idle_token=None,
                last_message_kind=kind,
                last_message_line=line,
            )
            if e.child_full == child_full
            else e
            for e in watches.get(orchestrator, [])
        ]
        save_watch(root, watches)


def cancel(root: Path, orchestrator: str, child_full: str) -> None:
    """A successful broker stop/destroy intentionally ends this run."""
    with locked(root):
        watches = load_watch(root)
        watches[orchestrator] = [
            e for e in watches.get(orchestrator, []) if e.child_full != child_full
        ]
        save_watch(root, watches)


def read_jobs() -> dict[str, JobSnapshot] | None:
    """Every job row by container name, or None when the database cannot be read."""
    try:
        from sqlmodel import Session

        from jailbee.db import get_engine

        with Session(get_engine()) as session:
            return {
                name: JobSnapshot(
                    r.phase, r.pid, r.op_kind, r.error_msg, r.started_at, r.log_path or None
                )
                for name, r in background.list_all_jobs(session).items()
            }
    except Exception as exc:  # best effort: unreadable means "wait"
        log.debug("orchestration watch: job database unreadable: %s", exc)
        return None


def verdict(
    entry: WatchEntry,
    job: JobSnapshot | None,
    child: Node | None,
    *,
    orchestrator_full: str,
    short: str,
    now: datetime,
) -> Outcome | None:
    """Decide one watch entry: an `Outcome` to deliver, `DROP`, or None to wait."""
    spawn = entry.kind == "spawn"
    since = datetime.fromisoformat(entry.since)
    noun = "creation" if spawn else "restart"

    if job is not None and job.op_kind in _TRACKED_KINDS:
        stale_ok = job.started_at >= since  # a failed/dead row older than the watch is not ours
        if job.pid <= 0 and job.phase not in background.TERMINAL_PHASES:
            return None  # no real worker to judge (os.kill(0, 0) would probe our own group)
        if not background.clearable(job.phase, job.pid):
            return None  # live worker, any phase
        if stale_ok:
            if job.phase in background.TERMINAL_PHASES:
                reason = job.error_msg or " ".join(
                    w for w in ("see the worker log", job.log_path) if w
                )
                detail = f"{noun} of child `{short}` failed: {reason}"
                if spawn and child is None:
                    detail += " — the child was removed"
                return failed(detail)
            # No error was recorded: the worker was killed, or died before it
            # could write one. Say what it left behind, since a running child
            # refuses a `restart` until it is stopped.
            detail = f"{noun} of child `{short}` stopped unexpectedly during {job.phase}"
            if child is None:
                detail += " — the child was removed" if spawn else ""
            elif child.running:
                detail += " — the child was kept running; `stop` it before a `restart`"
            else:
                detail += " — the child was kept, stopped"
            if job.log_path:
                detail += f". Worker log on the host: {job.log_path}"
            return failed(detail)

    if child is not None and child.config.get(labels.SPAWNED_BY) == orchestrator_full:
        return done(f"child `{short}` is ready") if spawn else DROP
    if not spawn:
        return DROP
    if (now - since).total_seconds() > WATCH_GRACE_S:
        return failed(f"creation of child `{short}` ended without a container")
    return None


def checked_verdict(
    entry: WatchEntry,
    job: JobSnapshot | None,
    child: Node | None,
    *,
    orchestrator_full: str,
    short: str,
    now: datetime,
) -> Outcome | None:
    """`verdict`, with a vanished worker confirmed against a fresh job row.

    `job` is a snapshot taken before the caller refreshed its nodes, which is
    a full `incus list` and can take seconds. A worker that finishes in that
    window deletes its row and then exits, so the snapshot still shows a
    working phase while the pid is already gone — a completed create read as
    "stopped unexpectedly". The row is re-read once the pid is found gone:
    still there, the worker really died; gone or replaced, judge the fresh one.
    """
    outcome = verdict(
        entry, job, child, orchestrator_full=orchestrator_full, short=short, now=now
    )
    if (
        outcome is None
        or outcome is DROP
        or outcome.state != "failed"
        or job is None
        or job.phase in background.TERMINAL_PHASES
    ):
        return outcome
    fresh = read_jobs()
    if fresh is None:
        return None  # cannot confirm the death: wait for a readable database
    current = fresh.get(entry.child_full)
    if current == job:
        return outcome
    return verdict(
        entry, current, child, orchestrator_full=orchestrator_full, short=short, now=now
    )
