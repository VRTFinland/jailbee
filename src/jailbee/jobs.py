"""Presentation helpers for `jailbee job` — no DB access, no subprocess.

Row data comes from `background.py`; this module only decides how a job
renders. Table/JSON emission itself is the generic `table_format.emit`, so
`jailbee job ls` gets `--format` and `--fields` for free, exactly like `jailbee ls`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from jailbee import table_format
from jailbee.db.models import BackgroundJob

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

# A runtime (not TYPE_CHECKING) import of BackgroundJob: this alias subscripts
# the generic at runtime, so a forward reference would not resolve.
FieldSpecJob = table_format.FieldSpec[BackgroundJob]

# Longest error rendered in a table cell before it is elided. JSON output is
# never truncated — scripts want the whole message.
_ERROR_CELL_MAX = 60

_LOG_POLL_SEC = 0.5


def format_age(started_at: datetime, now: datetime) -> str:
    """Compact age of a job: ``45s``, ``12m``, ``3h``, ``2d``.

    Clamped at zero so clock skew between the writing worker and the reading
    process can never render a negative age.
    """
    secs = max(0, int((now - started_at).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def truncate_error(msg: str | None, limit: int = _ERROR_CELL_MAX) -> str:
    """One-line, length-capped error text for a table cell."""
    if not msg:
        return "—"
    one_line = " ".join(msg.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"


def _short_name(job: BackgroundJob) -> str:
    """Container name with its repo prefix stripped, when it has one."""
    prefix = f"{job.container_prefix}-"
    if job.container_name.startswith(prefix):
        return job.container_name[len(prefix) :]
    return job.container_name


def _phase_cell(job: BackgroundJob) -> str:
    """Phase with Rich markup: red when dead, yellow while in flight.

    The text itself comes from `background.job_label`, the shared source for
    every job-state label — so this column and the `jailbee ls` JOB column can
    never disagree about the same state. Deadness is decided separately via
    `background.clearable`: `job_label` can render a live `destroy`-kind job
    in `starting` as `"destroying"`, so `label != job.phase` is no longer a
    valid "this job is dead" test.
    """
    from jailbee import background

    label = background.job_label(job.phase, job.pid, kind=job.op_kind)
    if background.clearable(job.phase, job.pid):
        return f"[red]{label}[/red]"
    return f"[yellow]{label}[/yellow]"


def job_field_specs(*, now: datetime, all_repos: bool) -> list[FieldSpecJob]:
    """Column definitions for `jailbee job ls`.

    With ``all_repos`` the REPO column is shown and names stay fully
    qualified, mirroring how `jailbee ls --all` presents cross-repo rows.
    """
    return [
        table_format.FieldSpec(
            name="name",
            header="NAME",
            cell=(lambda j: j.container_name) if all_repos else _short_name,
            json=lambda j: j.container_name,
        ),
        table_format.FieldSpec(
            name="repo",
            header="REPO",
            cell=lambda j: j.container_prefix,
            json=lambda j: j.container_prefix,
            default_table=all_repos,
            default_json=all_repos,
        ),
        table_format.FieldSpec(
            name="kind",
            header="KIND",
            cell=lambda j: j.op_kind,
            json=lambda j: j.op_kind,
        ),
        table_format.FieldSpec(
            name="phase",
            header="PHASE",
            cell=_phase_cell,
            json=lambda j: j.phase,
        ),
        table_format.FieldSpec(
            name="pid",
            header="PID",
            cell=lambda j: str(j.pid),
            json=lambda j: j.pid,
            justify="right",
        ),
        table_format.FieldSpec(
            name="age",
            header="AGE",
            cell=lambda j: format_age(j.started_at, now),
            json=lambda j: j.started_at.isoformat(),
            justify="right",
        ),
        table_format.FieldSpec(
            name="error",
            header="ERROR",
            cell=lambda j: truncate_error(j.error_msg),
            json=lambda j: j.error_msg,
        ),
        table_format.FieldSpec(
            name="log",
            header="LOG",
            cell=lambda j: j.log_path,
            json=lambda j: j.log_path,
        ),
    ]


def follow_log(
    path: Path,
    *,
    write: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = _LOG_POLL_SEC,
) -> None:
    """Write the log, then keep writing what the worker appends.

    A poll loop rather than a `tail -f` subprocess, so it is unit-testable
    (inject ``sleep``) and needs no external binary. Returns cleanly on
    Ctrl-C, which surfaces as KeyboardInterrupt out of ``sleep``.
    """
    with path.open("r", errors="replace") as fh:
        write(fh.read())
        while True:
            chunk = fh.read()
            if chunk:
                write(chunk)
                continue
            try:
                sleep(poll_interval)
            except KeyboardInterrupt:
                return
