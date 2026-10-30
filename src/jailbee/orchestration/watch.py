"""Watch list for started children, and the verdict on each entry.

When the broker starts a child it may leave work running detached (the create
worker, a boot's autostart). A watch entry remembers that, so a later broker
round can tell the orchestrator whether the child came up. The verdict is a
pure function of the child's job row and container; reading the job database is
best effort and never raises.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
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


@dataclass(frozen=True)
class JobSnapshot:
    """The fields of a job row the verdict needs, detached from the session."""

    phase: str
    pid: int
    op_kind: str
    error_msg: str | None
    started_at: datetime


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


def cancel(root: Path, orchestrator: str, child_full: str) -> None:
    """A successful broker stop/destroy intentionally ends this run."""
    watches = load_watch(root)
    watches[orchestrator] = [e for e in watches.get(orchestrator, []) if e.child_full != child_full]
    save_watch(root, watches)


def read_jobs() -> dict[str, JobSnapshot] | None:
    """Every job row by container name, or None when the database cannot be read."""
    try:
        from sqlmodel import Session

        from jailbee.db import get_engine

        with Session(get_engine()) as session:
            return {
                name: JobSnapshot(r.phase, r.pid, r.op_kind, r.error_msg, r.started_at)
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
                reason = job.error_msg or "see the worker log"
                detail = f"{noun} of child `{short}` failed: {reason}"
                if spawn and child is None:
                    detail += " — the child was removed"
                return failed(detail)
            return failed(f"{noun} of child `{short}` stopped unexpectedly during {job.phase}")

    if child is not None and child.config.get(labels.SPAWNED_BY) == orchestrator_full:
        return done(f"child `{short}` is ready") if spawn else DROP
    if not spawn:
        return DROP
    if (now - since).total_seconds() > WATCH_GRACE_S:
        return failed(f"creation of child `{short}` ended without a container")
    return None
