"""Host-side leftovers of a destroyed container (issue #78).

Both destroy paths (`lifecycle.destroy_container`, `orphans.destroy_orphan`)
take a :func:`snapshot` while the instance still exists, delete it, then call
:func:`cleanup`. Everything is bound to the container's exact full name, and
the outbox parts to its creation timestamp too (the identity the journals are
keyed by), so a namesake, a longer name sharing a prefix or a later
incarnation of the same name is never touched. Each step is idempotent and
best-effort: a failure is a warning, never a failed destroy.

What is deliberately kept: job rows' *destroy* logs (the worker writing them
is the caller), spawn requests that create a future incarnation, live create
reservations, journals with an unknown publication outcome, archived journal
history and published Sent history.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
from dataclasses import dataclass
from pathlib import Path

from jailbee.tui import warn


@dataclass(frozen=True)
class TeardownSnapshot:
    """What must be read before the instance (and its job row) is gone."""

    name: str
    identity: object | None  # outbox_io.ContainerIdentity; lazy import keeps this light
    log_paths: tuple[Path, ...]


def _job_log_paths(name: str) -> tuple[Path, ...]:
    """Files of the job row's latest run: log, progress and job file beside it.

    Only paths inside the state ``logs`` directory whose file name starts with
    ``<name>-`` count, and a destroy job's own log is left to its worker.
    """
    from sqlmodel import Session

    from jailbee.autostart_progress import path_for_log
    from jailbee.db import get_engine, state_dir
    from jailbee.db.models import JOB_DESTROY, BackgroundJob

    with Session(get_engine()) as session:
        row = session.get(BackgroundJob, name)
        if row is None or not row.log_path or row.op_kind == JOB_DESTROY:
            return ()
        log = Path(row.log_path)
    if log.parent != state_dir() / "logs" or not log.name.startswith(f"{name}-"):
        return ()
    return (log, path_for_log(log), log.with_suffix(".job.json"))


def snapshot(incus: object, name: str) -> TeardownSnapshot:
    """Capture identity and job log paths; never raises."""
    identity = None
    try:
        from jailbee.outbox_io import container_identity

        identity = container_identity(incus, name)  # type: ignore[arg-type]
    except Exception:
        identity = None
    try:
        logs = _job_log_paths(name)
    except Exception:
        logs = ()
    return TeardownSnapshot(name=name, identity=identity, log_paths=logs)


def cleanup(snap: TeardownSnapshot) -> None:
    """Run every teardown step; each failure warns and the rest still run."""
    steps = (
        ("background job logs", _remove_job_logs),
        ("orchestration state", _drop_orchestration),
        ("issue outbox journals", _archive_issue_journals),
        ("PR outbox lock", _remove_pr_lock),
    )
    for label, step in steps:
        try:
            step(snap)
        except Exception as exc:
            warn(f"Could not clean up {label} for '{snap.name}' (continuing): {exc}")


def _remove_job_logs(snap: TeardownSnapshot) -> None:
    for path in snap.log_paths:
        path.unlink(missing_ok=True)


def _drop_orchestration(snap: TeardownSnapshot) -> None:
    from jailbee.orchestration import state
    from jailbee.orchestration.store import PendingImportError

    name = snap.name
    root = state.state_root()
    state.purge_pending(root, name)

    # Queues are keyed by the requesting orchestrator. A destroyed
    # orchestrator's own queue is dead (the broker drops it for vanished
    # containers too); in any queue only restarts of this exact child are
    # dead — `new`/`fork`/`pr` requests create a future incarnation.
    queues = state.load_queue(root)
    kept = {
        key: [r for r in reqs if not (r.child_full == name and r.kind == "restart")]
        for key, reqs in queues.items()
        if key != name
    }
    if kept != queues:
        state.save_queue(root, kept)

    # A reservation whose create worker is alive is a live broker request.
    try:
        reservations = state.load_reservations(root)
    except PendingImportError:
        return  # unknown until the legacy import finishes; keep every reservation
    pruned = {
        key: [r for r in items if not (r.child_full == name and not state.reservation_alive(r.pid))]
        for key, items in reservations.items()
    }
    if pruned != reservations:
        state.save_reservations(root, pruned)


def _archive_issue_journals(snap: TeardownSnapshot) -> None:
    """Archive certain journals; keep any with an unknown publication outcome."""
    identity = snap.identity
    if identity is None:
        return
    from jailbee.outbox_io import ContainerIdentity, JournalError, JournalKey, JournalStore

    assert isinstance(identity, ContainerIdentity)
    store = JournalStore()
    directory = store.root / JournalStore._container_directory(identity)
    if not directory.is_dir():
        return
    retained: list[str] = []
    for path in sorted(directory.glob("*.json")):
        try:
            encoded = path.stem
            manifest = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
            key = JournalKey(identity=identity, manifest_name=manifest)
            store.archive(key)
        except (JournalError, ValueError, binascii.Error):
            retained.append(path.name)
            continue
        with contextlib.suppress(OSError):
            path.with_name(f"{path.name}.lock").unlink(missing_ok=True)
    if retained:
        warn(
            f"Kept {len(retained)} issue outbox journal(s) of '{snap.name}': "
            "publication outcome unknown or unreadable"
        )


def _remove_pr_lock(snap: TeardownSnapshot) -> None:
    identity = snap.identity
    if identity is None:
        return
    from jailbee.db import state_dir
    from jailbee.outbox_io import ContainerIdentity

    assert isinstance(identity, ContainerIdentity)
    raw = f"{identity.full_name}\0{identity.created_at}".encode()
    (state_dir() / "pr-outbox" / "locks" / f"{hashlib.sha256(raw).hexdigest()}.lock").unlink(
        missing_ok=True
    )
