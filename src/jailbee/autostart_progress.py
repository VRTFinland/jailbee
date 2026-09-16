"""Per-step progress for a detached autostart run.

Written by the supervisor beside its log, read by `jailbee autostart
status`. A file rather than a DB column: the job row carries one coarse
phase for `jailbee ls`, and widening that schema for a view nobody
queries would cost a migration.

**Entries are a log, not a state machine.** A step's ``"start"`` is not
guaranteed to be followed by an ``"ok"`` or a ``"fail"``: the executor's
abort handler interrupts in-flight steps without routing them through
`autostart._finish_step`, and a killed supervisor writes nothing at all.
A reader must treat a dangling ``"start"`` as "unknown, last seen
running" and never as an error or a success.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


def path_for_log(log_path: str | Path) -> Path:
    """The progress file belonging beside a detached run's log.

    One derivation, two producers: `_spawn_autostart_worker` names the pair
    when it hands the stages to a supervisor, and a background worker that
    finishes them in its own process derives the same pair from the log it
    is already writing to. `jailbee autostart status` then finds the file
    the same way whichever process ran the stages.
    """
    return Path(log_path).with_suffix(".progress.json")


@dataclass(frozen=True)
class ProgressEntry:
    stage: str
    step: str
    state: str  # "start" | "ok" | "fail"
    at: str  # ISO-8601


def append(path: Path, entry: ProgressEntry) -> None:
    """Append one entry. Never raises: progress is bookkeeping, and a full
    or read-only state dir must not abort the run it describes."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry)) + "\n")
    except OSError:
        return


def read(path: Path) -> list[ProgressEntry]:
    """Every entry written so far; empty when the file is absent.

    Unreadable lines are skipped rather than raising — a supervisor killed
    mid-write leaves a partial last line, and the entries before it are
    still the best account of where the run got to.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[ProgressEntry] = []
    for line in lines:
        try:
            out.append(ProgressEntry(**json.loads(line)))
        except (ValueError, TypeError):
            continue
    return out
