"""Host-side tracking + job marshalling for detached `jailbee new` operations.

State lives in the `background_op` SQLite table (see db/models.py), whose
rows this module calls *jobs*. This is the only place outside db/ that
touches that schema. It also (de)serialises `NewContainerOptions` for the
worker's job file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from sqlmodel import Session, select

from jailbee.db.models import JOB_CREATE as JOB_CREATE
from jailbee.db.models import JOB_DESTROY as JOB_DESTROY
from jailbee.db.models import BackgroundJob

if TYPE_CHECKING:
    from jailbee.lifecycle import NewContainerOptions

PHASE_STARTING = "starting"
PHASE_CREATING = "creating"
PHASE_CLONING = "cloning"
PHASE_AUTOSTART = "autostart"
PHASE_STOPPING = "stopping"
PHASE_DELETING = "deleting"
PHASE_FAILED = "failed"

TERMINAL_PHASES = frozenset({PHASE_FAILED})

# Phases at which a create op's container is already `incus start`ed and can be
# attached to (shell/tmux) without waiting for the whole op to finish. The
# container is started before the clone, so by the autostart phase it's running
# and its repo is in place; autostart steps run as tmux windows the user may
# actually want to watch appear live.
ATTACHABLE_CREATE_PHASES = frozenset({PHASE_AUTOSTART})


@dataclass(frozen=True)
class ClearOutcome:
    """Result of a guarded `clear_job` call.

    ``reason`` explains what happened, so callers phrase their own message:
    ``failed``/``stale`` mean the row was deleted (terminal phase / worker
    gone), ``running`` means it was kept, ``missing`` means there was no row.
    """

    cleared: bool
    reason: Literal["failed", "stale", "running", "missing"]
    kind: str | None = None
    phase: str | None = None
    pid: int | None = None


def clearable(phase: str, pid: int) -> bool:
    """True when a job row is safe to delete: it reached a terminal phase, or
    its worker process is gone. Single source of truth for "this job is dead"
    — used by the attach guards, `jailbee job clear` and the dashboard action."""
    return phase in TERMINAL_PHASES or not worker_alive(pid)


def job_label(phase: str, pid: int, *, kind: str | None = None) -> str:
    """Plain-text label for a job's state.

    A terminal phase speaks for itself. A working phase whose worker has
    vanished gains a `(worker gone)` suffix — keeping the phase, so the label
    says where progress stopped. Single source of truth for every renderer:
    the `jailbee ls` JOB column, its `--json` value, and `jailbee job ls`'s PHASE.

    ``kind`` lets a live `destroy`-kind job in the `starting` phase render as
    the more legible ``"destroying"`` — but only while its worker is alive;
    a dead destroy job still reads ``"starting (worker gone)"`` so a vanished
    worker is never hidden behind the friendlier name. This label is text
    only — callers must use :func:`clearable` (not a comparison against this
    label) to decide whether a job is dead, since a live destroy job's label
    now legitimately differs from its bare phase.
    """
    if phase in TERMINAL_PHASES:
        return phase
    if not worker_alive(pid):
        return f"{phase} (worker gone)"
    if kind == JOB_DESTROY and phase == PHASE_STARTING:
        return "destroying"
    return phase


def job_label_or_empty(phase: str | None, pid: int | None, *, kind: str | None = None) -> str:
    """`job_label` for a container's optional job fields: "" when there is no
    job at all, and the bare phase for a row that somehow carries no pid
    (only reachable from a hand-built ContainerInfo — every populated row has
    a non-null pid)."""
    if phase is None:
        return ""
    if pid is None:
        return phase
    return job_label(phase, pid, kind=kind)


def start_job(
    session: Session,
    *,
    container_name: str,
    container_prefix: str,
    branch: str | None,
    pid: int,
    log_path: str,
    now: datetime,
    op_kind: str = JOB_CREATE,
) -> None:
    """Insert (or replace) the tracking row for a freshly-spawned worker."""
    row = session.get(BackgroundJob, container_name)
    if row is None:
        row = BackgroundJob(
            container_name=container_name,
            container_prefix=container_prefix,
            branch=branch,
            phase=PHASE_STARTING,
            pid=pid,
            log_path=log_path,
            error_msg=None,
            op_kind=op_kind,
            started_at=now,
            updated_at=now,
        )
    else:
        row.container_prefix = container_prefix
        row.branch = branch
        row.phase = PHASE_STARTING
        row.pid = pid
        row.log_path = log_path
        row.error_msg = None
        row.op_kind = op_kind
        row.started_at = now
        row.updated_at = now
    session.add(row)
    session.commit()


def set_phase(session: Session, container_name: str, phase: str, *, now: datetime) -> None:
    """Advance an existing op to a new phase. No-op if the row is gone."""
    row = session.get(BackgroundJob, container_name)
    if row is None:
        return
    row.phase = phase
    row.updated_at = now
    session.add(row)
    session.commit()


def fail_job(session: Session, container_name: str, error_msg: str, *, now: datetime) -> None:
    """Mark an op failed and record the error. No-op if the row is gone."""
    row = session.get(BackgroundJob, container_name)
    if row is None:
        return
    row.phase = PHASE_FAILED
    row.error_msg = error_msg
    row.updated_at = now
    session.add(row)
    session.commit()


def delete_job(session: Session, container_name: str) -> None:
    """Remove the tracking row. Used on success and by `jailbee destroy`."""
    row = session.get(BackgroundJob, container_name)
    if row is not None:
        session.delete(row)
        session.commit()


def clear_job(session: Session, container_name: str) -> ClearOutcome:
    """Delete a dead job row; refuse a live one.

    Guarded counterpart to :func:`delete_job`, which stays unguarded because
    the success and destroy paths legitimately delete rows of jobs they own.
    Clearing a live job would orphan a worker that keeps writing to the
    container and can no longer mark itself failed.
    """
    row = session.get(BackgroundJob, container_name)
    if row is None:
        return ClearOutcome(cleared=False, reason="missing")
    if not clearable(row.phase, row.pid):
        return ClearOutcome(
            cleared=False,
            reason="running",
            kind=row.op_kind,
            phase=row.phase,
            pid=row.pid,
        )
    reason: Literal["failed", "stale"] = "failed" if row.phase in TERMINAL_PHASES else "stale"
    outcome = ClearOutcome(
        cleared=True, reason=reason, kind=row.op_kind, phase=row.phase, pid=row.pid
    )
    session.delete(row)
    session.commit()
    return outcome


def list_jobs(session: Session, container_prefix: str) -> dict[str, BackgroundJob]:
    """Return {container_name: row} for one repo's in-flight/failed ops."""
    rows = session.exec(
        select(BackgroundJob).where(BackgroundJob.container_prefix == container_prefix)
    ).all()
    return {r.container_name: r for r in rows}


def list_all_jobs(session: Session) -> dict[str, BackgroundJob]:
    """Return {container_name: row} for every repo's jobs (`--all-repos`)."""
    rows = session.exec(select(BackgroundJob)).all()
    return {r.container_name: r for r in rows}


def worker_alive(pid: int) -> bool:
    """True if a process with ``pid`` exists (signal-0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — still "alive" for our purposes.
        return True
    return True


def op_to_job(
    opts: NewContainerOptions,
    *,
    container_name: str,
    log_path: str,
) -> dict[str, Any]:
    """Serialise a NewContainerOptions (+ run metadata) to a JSON-safe dict."""
    return {
        "container_name": container_name,
        "log_path": log_path,
        "opts": {
            "container_branch": opts.container_branch,
            "name": opts.name,
            "network": opts.network,
            "memory": opts.memory,
            "cpu": opts.cpu,
            "from_base": opts.from_base,
            "clone": opts.clone,
            "autostart": opts.autostart,
            "mirror_endpoint": (
                list(opts.mirror_endpoint) if opts.mirror_endpoint is not None else None
            ),
            "mirror_ca_path": (
                str(opts.mirror_ca_path) if opts.mirror_ca_path is not None else None
            ),
            "base": opts.base,
            "mount": opts.mount,
            "base_branch_label": opts.base_branch_label,
            "pr": opts.pr,
            "untrusted_head": opts.untrusted_head,
            "clone_commit": opts.clone_commit,
            "assume_yes": opts.assume_yes,
            "approved_autostart_ref": opts.approved_autostart_ref,
            "autofetch_done": opts.autofetch_done,
        },
    }


def job_to_opts(job: dict[str, Any]) -> tuple[NewContainerOptions, str, str]:
    """Inverse of op_to_job. Returns (opts, container_name, log_path)."""
    from pathlib import Path

    from jailbee.lifecycle import NewContainerOptions

    o = job["opts"]
    endpoint = o["mirror_endpoint"]
    opts = NewContainerOptions(
        container_branch=o["container_branch"],
        name=o["name"],
        network=o["network"],
        memory=o["memory"],
        cpu=o["cpu"],
        from_base=o["from_base"],
        clone=o["clone"],
        autostart=o["autostart"],
        mirror_endpoint=(tuple(endpoint) if endpoint is not None else None),
        mirror_ca_path=(Path(o["mirror_ca_path"]) if o["mirror_ca_path"] is not None else None),
        base=o["base"],
        mount=o["mount"],
        base_branch_label=o["base_branch_label"],
        pr=o["pr"],
        untrusted_head=o.get("untrusted_head", False),
        clone_commit=o["clone_commit"],
        # `.get` not `[...]`: a job file written by an older jailbee predates these
        # keys, and an in-flight background `jailbee new` must survive an upgrade.
        assume_yes=o.get("assume_yes", False),
        approved_autostart_ref=o.get("approved_autostart_ref"),
        # False for an older job file: its worker then does its own fetch, as
        # that jailbee version's foreground never did one.
        autofetch_done=o.get("autofetch_done", False),
    )
    return opts, job["container_name"], job["log_path"]
