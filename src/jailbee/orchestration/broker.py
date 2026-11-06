"""The orchestration broker: one long-lived loop that serves every outbox."""

from __future__ import annotations

import contextlib
import fcntl
import itertools
import json
import logging
import queue
import signal
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from jailbee import __version__
from jailbee.config import load_repo_config
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.incus import ORCHESTRATION_DEVICE, IncusError
from jailbee.orchestration import (
    finishing,
    inbox_state,
    labels,
    publishing,
    socket_device,
    watch,
)
from jailbee.orchestration.actions import (
    GRANT_REVOKED,
    NOT_ALLOWED,
    ActionContext,
    Node,
    Outcome,
    dispatch,
    drain_queue,
    drop_queue,
    prune_reservations,
    refused,
)
from jailbee.orchestration.child_status import StatusCache
from jailbee.orchestration.journal import Journal, manifest_digest
from jailbee.orchestration.listener import Listener
from jailbee.orchestration.outbox import (
    INBOX_DIR,
    INBOX_READ_DIR,
    delete_inbox_files,
    delete_outbox_files,
    deliver,
    ensure_orchestration_env,
    flush_pending,
    grant_json_text,
    has_container_jailbee,
    has_pending,
    read_inbox_dir,
    read_outbox,
    set_aside_inbox_files,
    write_grant_json,
)
from jailbee.orchestration.protocol import (
    DestroyManifest,
    InboxMessage,
    InboxStatus,
    ManifestError,
    MessageManifest,
    PublishManifest,
    RestartManifest,
    SpawnManifest,
    StopManifest,
    body_file_of,
    inbox_file_name_for,
    new_host_id,
    parse_manifest,
)
from jailbee.orchestration.state import (
    atomic_write_text,
    load_queue,
    pending_dir,
    purge_vanished_pending,
    save_queue,
    state_root,
)
from jailbee.orchestration.store import BrokerStore, StoreError, store_path
from jailbee.outbox_io import OutboxReadError, _validate_outbox_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from types import FrameType

    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.orchestration.listener import Event
    from jailbee.orchestration.outbox import RawManifest
    from jailbee.orchestration.protocol import Manifest
    from jailbee.orchestration.socket_ops import Waiters
    from jailbee.orchestration.state import QueuedRequest
    from jailbee.orchestration.watch import JobSnapshot

log = logging.getLogger(__name__)

ACTIVE_INTERVAL = 5.0
IDLE_INTERVAL = 60.0
SLEEP_SLICE = 1.0
LOCK_FILE = "broker.lock"
#: Exit status after another jailbee version was installed under the running
#: broker; the unit's `RestartForceExitStatus` starts it again on the new code.
UPGRADED_EXIT = 75


@dataclass(frozen=True)
class ChildrenView:
    """An orchestrator's published children list and when it was observed."""

    observed_at: float
    children: list[dict[str, object]]


@dataclass
class BrokerMemory:
    """What the broker remembers between rounds (in memory only)."""

    # Orchestrator full name -> grant.json text last written into it.
    grant_written: dict[str, str] = field(default_factory=dict)
    publish_threads: list[threading.Thread] = field(default_factory=list)
    env_ensured: set[str] = field(default_factory=set)
    env_roles: dict[str, str | None] = field(default_factory=dict)
    # Keys of warnings already logged, so a standing problem is logged once.
    warned: set[str] = field(default_factory=set)
    # Inbox file sequence: orders the items emitted within one second.
    seq: Iterator[int] = field(default_factory=itertools.count)
    # Probe cadence and CPU readings behind the published child status (#64).
    status: StatusCache = field(default_factory=StatusCache)
    # Orchestrator full name -> its children as last published by sync_children.
    children: dict[str, ChildrenView] = field(default_factory=dict)
    # Container key (volatile.uuid) -> the context built for it in the last round.
    contexts: dict[str, ActionContext] = field(default_factory=dict)
    # Socket transport (None in tests that do not exercise it).
    listener: Listener | None = None
    store: BrokerStore | None = None
    requests: queue.Queue[Event] = field(default_factory=queue.Queue)
    # Container full name -> volatile.uuid whose socket device we attached.
    attached: dict[str, str] = field(default_factory=dict)
    # volatile.uuids whose sock dir this process created: the device must be
    # re-asserted (remove + add) until that has succeeded once.
    reassert: set[str] = field(default_factory=set)
    # Parked `next` long-polls, answered by the executor.
    waiters: Waiters = field(default_factory=lambda: _new_waiters())
    # `_monotonic()` of the last retention purge; None until the first round.
    last_purge: float | None = None
    # Store uuids the previous purge did not find in the listing.
    absent_uuids: set[str] = field(default_factory=set)
    # volatile.uuid values two or more listed instances share (no socket for them).
    shared_uuids: set[str] = field(default_factory=set)
    # Directory reuse requires a complete device inventory in the current round.
    socket_devices_verified: bool = False
    # Never serve a directory a foreign device named, even if cleanup failed.
    unsafe_socket_dirs: set[str] = field(default_factory=set)

    def warn_once(self, key: str, msg: str, *args: object) -> None:
        if key not in self.warned:
            self.warned.add(key)
            log.warning(msg, *args)


PURGE_INTERVAL = 3600.0
DEFAULT_RETENTION_DAYS = 30


def _monotonic() -> float:
    return time.monotonic()


def _retention_days(
    full: str, config: Mapping[str, str], configs: Mapping[str, Config | None]
) -> int:
    cfg = owner_config(full, config, configs)
    if cfg is None:
        return DEFAULT_RETENTION_DAYS
    return cfg.orchestration_view().repo.message_retention_days


def _purge_messages(
    memory: BrokerMemory,
    raw: list[dict[str, object]],
    now: datetime,
    everywhere: list[dict[str, object]] | None = None,
) -> None:
    """Once per hour: age out acked messages per repo and drop vanished containers' rows.

    ``raw`` is the current project's listing, ``everywhere`` every project's
    (None when it could not be listed: then no container counts as vanished).
    Never raises: a failed purge is logged and tried again next hour.
    """
    store = memory.store
    if store is None:
        return
    mono = _monotonic()
    if memory.last_purge is not None and mono - memory.last_purge < PURGE_INTERVAL:
        return
    memory.last_purge = mono
    configs: dict[str, Config | None] | None = None
    try:
        entry_config: dict[str, dict[str, str]] = {}
        for e in raw:
            conf = e.get("config")
            if isinstance(e.get("name"), str) and isinstance(conf, dict):
                entry_config[str(e["name"])] = conf
        live = {
            str(conf["volatile.uuid"])
            for e in everywhere or []
            if isinstance(conf := e.get("config"), dict) and conf.get("volatile.uuid")
        }
        # A uuid is gone only when it is in no project at all (an `incus
        # project switch` hides nothing from `--all-projects`), and only when
        # two consecutive purges (an hour apart) missed it. Its unread items
        # still stay for the retention of the repo it belonged to. An empty
        # or failed listing never counts as an absence.
        if live:
            absent = store.known_uuids() - live
            gone = absent & memory.absent_uuids
            memory.absent_uuids = absent
            if gone:
                configs = repo_configs(memory)
                names = store.recipient_names(gone)
                by_days: dict[int, set[str]] = {}
                for uuid in gone:
                    full = names.get(uuid)
                    days = _retention_days(full, {}, configs) if full else DEFAULT_RETENTION_DAYS
                    by_days.setdefault(days, set()).add(uuid)
                removed = 0
                for days, uuids in by_days.items():
                    removed += store.purge_uuids(
                        uuids, keep_unacked_after=now.timestamp() - days * 86400
                    )
                if removed:
                    log.info("broker: purged %d message rows of vanished containers", removed)
        recipients = store.recipients()
        if not recipients:
            return
        if configs is None:
            configs = repo_configs(memory)
    except Exception:
        log.exception("broker: message purge failed")
        return
    for full, uuid in recipients.items():
        try:
            days = DEFAULT_RETENTION_DAYS
            if full in entry_config:
                days = _retention_days(full, entry_config[full], configs)
            store.purge_acked_older_than(uuid, now.timestamp() - days * 86400)
        except Exception:
            log.exception("broker: message purge of %s failed", full)


def _new_waiters() -> Waiters:
    from jailbee.orchestration.socket_ops import Waiters

    return Waiters()


class StopFlag:
    """SIGTERM/SIGINT handler that only records the request; the loop polls it."""

    def __init__(self) -> None:
        self.requested = False

    def __call__(self, signum: int, frame: FrameType | None) -> None:
        self.requested = True


def repo_configs(memory: BrokerMemory | None = None) -> dict[str, Config | None]:
    """Registered container prefix -> loaded `Config`, or None when it does not load.

    A failed repo keeps its prefix (as None) so its containers are never
    attributed to a shorter registered prefix.
    """
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    memory = memory if memory is not None else BrokerMemory()
    with Session(get_engine()) as session:
        rows = [(r.container_prefix, r.repo_root) for r in session.exec(select(RegisteredRepo))]
    out: dict[str, Config | None] = {}
    for prefix, repo_root in rows:
        root = Path(repo_root)
        cfg: Config | None = None
        if not root.is_dir():
            memory.warn_once(f"repo-gone:{prefix}", "broker: repo %s: %s is gone", prefix, root)
        else:
            try:
                cfg = load_repo_config(root)
            except Exception as e:
                memory.warn_once(
                    f"repo-load:{prefix}", "broker: repo %s: config load failed: %s", prefix, e
                )
            else:
                if cfg.container_prefix != prefix:
                    memory.warn_once(
                        f"repo-prefix:{prefix}",
                        "broker: repo %s: prefix changed to %s; run `jailbee apply`",
                        prefix,
                        cfg.container_prefix,
                    )
                    cfg = None
        out[prefix] = cfg
    return out


def config_for(full: str, configs: Mapping[str, Config | None]) -> Config | None:
    """The config of the longest prefix `full` starts with (plus `-`); None if it failed."""
    best: str | None = None
    for prefix in configs:
        if full.startswith(prefix + "-") and (best is None or len(prefix) > len(best)):
            best = prefix
    return None if best is None else configs[best]


def owner_config(
    full: str, config: Mapping[str, str], configs: Mapping[str, Config | None]
) -> Config | None:
    """A child resolves through its orchestrator's repo; anything else by its own name."""
    parent = config.get(labels.SPAWNED_BY)
    return config_for(parent or full, configs)


def _labelled(config: Mapping[str, str]) -> bool:
    """Whether the labels make this container an orchestrator or a child candidate."""
    return bool(config.get(labels.SPAWNED_BY) or config.get(labels.GRANT))


def _status(ref: str, outcome: Outcome) -> InboxStatus:
    return InboxStatus(
        version=1,
        id=new_host_id(),
        type="status",
        ref=ref,
        state=outcome.state,
        detail=outcome.detail,
    )


def _reply(ctx: ActionContext, ref: str, outcome: Outcome, memory: BrokerMemory) -> None:
    deliver(
        ctx.incus,
        ctx.root,
        ctx.sender.full,
        _status(ref, outcome),
        running=ctx.sender.running,
        uid=ctx.cfg.container_user.uid,
        now=ctx.now,
        seq=next(memory.seq),
    )
    clear_reply_path(ctx, ref)


def clear_reply_path(ctx: ActionContext, ref: str) -> None:
    """Drop the done-reply gate once the status for `ref` reached the sender."""
    path = finishing.reply_path(ctx, ctx.sender)
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get("ref") == ref:
            path.unlink(missing_ok=True)


def _track(
    ctx: ActionContext, ref: str, child_full: str, kind: Literal["spawn", "restart"]
) -> None:
    """Remember a started child so a later round can report how it came up.

    `since` is `ctx.now`, the round's start: the job row a spawn creates exists
    only after that, so a genuine failure is never mistaken for a stale row.
    """
    with watch.locked(ctx.root):
        watches = watch.load_watch(ctx.root)
        if any(e.ref == ref for e in watches.get(ctx.sender.full, [])):
            return  # Actions registered the resolved name durably before launch.
        mine = [e for e in watches.get(ctx.sender.full, []) if e.child_full != child_full]
        mine.append(watch.WatchEntry(ref, child_full, kind, ctx.now.isoformat(), supervise=True))
        watches[ctx.sender.full] = mine
        watch.save_watch(ctx.root, watches)


def _track_restart(ctx: ActionContext, ref: str, child_full: str, outcome: Outcome) -> None:
    """Every successful restart owes actual agent-liveness confirmation."""
    if outcome.state in ("done", "started"):
        _track(ctx, ref, child_full, "restart")


def _watch_manifest(
    ctx: ActionContext, manifest: Manifest | None, ref: str, outcome: Outcome
) -> None:
    """Start watching what a handled spawn or restart left running; never raises."""
    try:
        from jailbee.lifecycle import derive_container_name

        if isinstance(manifest, SpawnManifest) and outcome.state == "started":
            _track(ctx, ref, derive_container_name(ctx.cfg, manifest.branch), "spawn")
        elif isinstance(manifest, RestartManifest):
            child = ctx.child(manifest.child)
            if isinstance(child, Node):
                _track_restart(ctx, ref, child.full, outcome)
    except Exception:
        log.exception("broker: %s: could not watch %s", ctx.sender.full, ref)


def _settle_watches(
    ctx: ActionContext, memory: BrokerMemory, jobs: dict[str, JobSnapshot] | None
) -> None:
    """Report the final status of this orchestrator's watched children.

    Deliver first, then save the shortened list: a crash in between repeats a
    status rather than losing one.
    """
    from jailbee.lifecycle import short_name

    with watch.locked(ctx.root):
        jobs = watch.read_jobs()
        watches = watch.load_watch(ctx.root)
        entries = watches.get(ctx.sender.full, [])
        if any(e.supervise for e in entries):
            try:
                fresh_nodes = _nodes(
                    ctx.incus.list_containers(), {ctx.cfg.container_prefix: ctx.cfg}
                )
            except Exception:
                log.exception("broker: could not refresh nodes for watch settlement")
                return
            ctx = replace(ctx, nodes=fresh_nodes)
        for entry in list(entries):
            if jobs is None and (entry.supervise or (not entry.confirmed and not entry.agent_only)):
                continue
            if entry.supervise:
                _settle_agent_watch(ctx, memory, jobs or {}, watches, entries, entry)
                continue
            outcome = watch.checked_verdict(
                entry,
                (jobs or {}).get(entry.child_full),
                ctx.nodes.get(entry.child_full),
                orchestrator_full=ctx.sender.full,
                short=short_name(ctx.cfg, entry.child_full),
                now=ctx.now,
            )
            if outcome is None:
                continue
            if outcome is not watch.DROP:
                _reply(ctx, entry.ref, outcome, memory)
            entries.remove(entry)
            watch.save_watch(ctx.root, watches)


def _settle_agent_watch(
    ctx: ActionContext,
    memory: BrokerMemory,
    jobs: dict[str, JobSnapshot],
    watches: dict[str, list[watch.WatchEntry]],
    entries: list[watch.WatchEntry],
    entry: watch.WatchEntry,
) -> None:
    from jailbee import background
    from jailbee.db.models import JOB_REFRESH
    from jailbee.lifecycle import short_name
    from jailbee.orchestration import agent_watch

    job = jobs.get(entry.child_full)
    if (
        job is not None
        and job.op_kind == JOB_REFRESH
        and job.pid > 0
        and not background.clearable(job.phase, job.pid)
    ):
        return
    child = ctx.nodes.get(entry.child_full)
    status = None
    if child is not None and child.config.get(labels.SPAWNED_BY) != ctx.sender.full:
        entries.remove(entry)  # Never monitor another orchestrator's namesake.
        watch.save_watch(ctx.root, watches)
        return
    if child is not None and labels.is_finished(child.config):
        entries.remove(entry)
        watch.save_watch(ctx.root, watches)
        return
    if not entry.confirmed and not entry.agent_only:
        boot = watch.checked_verdict(
            entry,
            jobs.get(entry.child_full),
            child,
            orchestrator_full=ctx.sender.full,
            short=short_name(ctx.cfg, entry.child_full),
            now=ctx.now,
        )
        if boot is None:
            return
        if boot.state == "failed":
            _reply(ctx, entry.ref, boot, memory)
            entries.remove(entry)
            watch.save_watch(ctx.root, watches)
            return
    if child is not None and child.running:
        status = agent_watch.probe(ctx.incus, child.full, uid=ctx.cfg.container_user.uid)
        if status is not None and status.alive:
            try:
                sid = agent_watch.current_session(ctx.cfg, child.full, status)
                if sid is not None:
                    history = labels.with_session(labels.sessions_of(child.config), sid)
                    if history is not None:
                        ctx.incus.config_set(child.full, labels.AGENT_SESSIONS, json.dumps(history))
            except Exception:
                log.debug("Could not record agent session for %s", child.full, exc_info=True)
    updated, outcome = agent_watch.advance(
        entry,
        child,
        status,
        now=ctx.now,
        has_jailbee=has_container_jailbee(ctx.incus, child.full)
        if child and child.running
        else False,
    )
    if outcome is not None:
        if outcome.state == "failed" and child is not None and child.running:
            tail = agent_watch.output_tail(
                ctx.incus,
                child.full,
                status.agent if status else "agent",
                uid=ctx.cfg.container_user.uid,
            )
            if tail:
                outcome = replace(outcome, detail=f"{outcome.detail}\nLast agent output:\n{tail}")
        if entry.confirmed:
            message = InboxMessage.model_validate(
                {
                    "version": 1,
                    "id": new_host_id(),
                    "type": "message",
                    "from": child.short if child else entry.child_full,
                    "kind": "blocked",
                    "body": outcome.detail,
                    "in_reply_to": entry.ref,
                }
            )
            deliver(
                ctx.incus,
                ctx.root,
                ctx.sender.full,
                message,
                running=ctx.sender.running,
                uid=ctx.cfg.container_user.uid,
                now=ctx.now,
                seq=next(memory.seq),
            )
        else:
            _reply(ctx, entry.ref, outcome, memory)
    if updated is not None and updated.confirmed and child is not None:
        token = (
            agent_watch.idle_signal(ctx.cfg, child.full, status, now=ctx.now)
            if status is not None
            else None
        )
        updated, idle_body = agent_watch.advance_idle(
            updated, token, now=ctx.now, after=ctx.cfg.orchestration_view().repo.idle_after_seconds
        )
        if idle_body is not None:
            tail = agent_watch.output_tail(
                ctx.incus,
                child.full,
                status.agent if status else "agent",
                uid=ctx.cfg.container_user.uid,
            )
            if tail:
                idle_body += f"\nLast agent output:\n{tail}"
            message = InboxMessage.model_validate(
                {
                    "version": 1,
                    "id": new_host_id(),
                    "type": "message",
                    "from": child.short,
                    "kind": "idle",
                    "body": idle_body,
                    "in_reply_to": entry.ref,
                }
            )
            deliver(
                ctx.incus,
                ctx.root,
                ctx.sender.full,
                message,
                running=ctx.sender.running,
                uid=ctx.cfg.container_user.uid,
                now=ctx.now,
                seq=next(memory.seq),
            )
    index = entries.index(entry)
    if updated is None:
        entries.pop(index)
    else:
        entries[index] = updated
    watch.save_watch(ctx.root, watches)


def _sync_grant_json(ctx: ActionContext, memory: BrokerMemory) -> None:
    """Write the effective grant into an orchestrator when it changed since the last write."""
    # TODO(socket-phase3): the socket `grant` op replaces grant.json; remove this writer.
    grant = ctx.grant()
    if grant is None:
        memory.grant_written.pop(ctx.sender.full, None)
        return
    text = grant_json_text(grant, *ctx.default_runner())
    if memory.grant_written.get(ctx.sender.full) == text:
        return
    write_grant_json(ctx.incus, ctx.sender.full, text, uid=ctx.cfg.container_user.uid)
    memory.grant_written[ctx.sender.full] = text


def _handle_raw(
    ctx: ActionContext, raw: RawManifest, journal: Journal, memory: BrokerMemory
) -> None:
    uid = ctx.cfg.container_user.uid
    try:
        _validate_outbox_name(raw.file_name)
    except ValueError:
        memory.warn_once(
            f"bad-name:{ctx.sender.full}:{raw.file_name}",
            "broker: %s: ignoring outbox file with an invalid name %r",
            ctx.sender.full,
            raw.file_name,
        )
        return
    body = body_file_of(raw.file_name, raw.text)
    bodies = {body: raw.files[body]} if body is not None and body in raw.files else {}
    names = [raw.file_name, *bodies]
    digest = manifest_digest(raw.file_name, raw.text, bodies)
    try:
        envelope = json.loads(raw.text)
        request_id = envelope.get("id") if isinstance(envelope, dict) else None
    except (ValueError, RecursionError):
        request_id = None
    if journal.seen(ctx.sender.key, digest) or (
        isinstance(request_id, str) and publishing.was_accepted(ctx, request_id)
    ):
        delete_outbox_files(ctx.incus, ctx.sender.full, names, uid=uid)
        return
    ref = PurePosixPath(raw.file_name).stem
    outcome: Outcome | None = None
    manifest: Manifest | None = None
    try:
        manifest = parse_manifest(raw.file_name, raw.text, raw.files)
    except ManifestError as e:
        outcome = refused(str(e))
    else:
        ref = manifest.id

    def record(ref: str, outcome: Outcome | None) -> None:
        journal.record(
            ctx.sender.key,
            digest,
            ref,
            outcome.state if outcome else "ignored",
            ctx.now,
            container_full=ctx.sender.full,
        )

    _handle_manifest(
        ctx,
        memory,
        ref=ref,
        manifest=manifest,
        outcome=outcome,
        record=record,
        reply=lambda ref, outcome: _reply(ctx, ref, outcome, memory),
        before_publish=lambda: delete_outbox_files(ctx.incus, ctx.sender.full, names, uid=uid),
        label=raw.file_name,
    )


def _handle_manifest(
    ctx: ActionContext,
    memory: BrokerMemory,
    *,
    ref: str,
    manifest: Manifest | None,
    outcome: Outcome | None,
    record: Callable[[str, Outcome | None], None],
    reply: Callable[[str, Outcome], None],
    before_publish: Callable[[], None],
    label: str | None = None,
) -> Outcome | None:
    """Steps shared by both transports once a request has been parsed.

    `outcome` is a preset refusal (parse failure); with a `manifest` and no
    preset the manifest is dispatched. `record` journals, `reply` delivers the
    status, `before_publish` is the file transport's outbox deletion.
    """
    if manifest is not None and outcome is None:
        try:
            outcome = dispatch(ctx, manifest)
        except Exception:
            # The detail goes to the container; the exception text stays in the log.
            log.exception("broker: %s: handler for %s failed", ctx.sender.full, label or ref)
            outcome = refused("broker error")
    if (
        isinstance(manifest, MessageManifest)
        and manifest.kind != "done"
        and outcome is not None
        and outcome.state in ("done", "queued")
        and ctx.role() == "child"
    ):
        watch.record_message(
            ctx.root,
            ctx.sender.config[labels.SPAWNED_BY],
            ctx.sender.full,
            manifest.kind or "note",
            manifest.body,
        )
    if isinstance(manifest, MessageManifest) and manifest.kind == "done" and outcome is not None:
        if outcome.state in ("done", "queued") and ctx.role() == "child":
            watch.cancel(ctx.root, ctx.sender.config[labels.SPAWNED_BY], ctx.sender.full)
        from jailbee.orchestration.state import atomic_write_text

        atomic_write_text(
            finishing.reply_path(ctx, ctx.sender),
            json.dumps({"ref": ref, "state": outcome.state, "detail": outcome.detail}),
        )
    # Journal before anything else: a crash after acting must never act again.
    record(ref, outcome)
    if outcome is not None:
        _watch_manifest(ctx, manifest, ref, outcome)
        _refresh_children_snapshot(ctx, manifest, outcome, memory)
        reply(ref, outcome)
    is_publish = (
        isinstance(manifest, PublishManifest) and outcome is not None and outcome.state == "started"
    )
    try:
        before_publish()
    except Exception:
        if is_publish:
            publishing.abort_before_start(
                ctx,
                ref,
                "publish was not started: could not remove the request; retry with a new id",
            )
        raise
    if is_publish:
        assert isinstance(manifest, PublishManifest)
        try:
            worker = publishing.start(ctx, manifest)
            if worker is not None:
                memory.publish_threads.append(worker)
        except Exception:
            publishing.abort_before_start(
                ctx, ref, "publish worker could not start; retry with a new id"
            )
            raise
    return outcome


def _refresh_children_snapshot(
    ctx: ActionContext, manifest: Manifest | None, outcome: Outcome, memory: BrokerMemory
) -> None:
    """Republish children.json right after the broker changed a child's state.

    Otherwise the orchestrator can read `done` for a `stop` and still see
    `running: true` until the end of the round. Failures only cost the lag.
    """
    if (
        not isinstance(manifest, (StopManifest, DestroyManifest, RestartManifest, SpawnManifest))
        or outcome.state not in ("done", "started")
        or ctx.role() != "orchestrator"
        or not ctx.sender.running
    ):
        return
    try:
        ctx.nodes = _nodes(ctx.incus.list_containers(), repo_configs(memory))
        _remember_children(ctx, memory, inbox_state.sync_children(ctx, memory.status))
    except Exception:
        log.exception("broker: child inbox state of %s failed", ctx.sender.full)


def _remember_children(ctx: ActionContext, memory: BrokerMemory, children: object) -> None:
    if isinstance(children, list):
        memory.children[ctx.sender.full] = ChildrenView(time.time(), children)


def _watch_drained(ctx: ActionContext, req: QueuedRequest, outcome: Outcome) -> None:
    try:
        if req.kind == "restart":
            _track_restart(ctx, req.manifest_id, req.child_full, outcome)
        elif outcome.state == "started":
            _track(ctx, req.manifest_id, req.child_full, "spawn")
    except Exception:
        log.exception("broker: %s: could not watch %s", ctx.sender.full, req.manifest_id)


def _ensure_env(ctx: ActionContext, memory: BrokerMemory) -> None:
    """Ensure each served container once per broker process; retry failures.

    Fork children copy an ensured orchestrator; new children use the current
    golden image.
    A vanished name is forgotten so a recreated older image is ensured again.
    """
    full = ctx.sender.full
    from jailbee.container_locks import locked as container_locked

    try:
        with container_locked(full):
            # The tick's nodes may predate a release. Recheck even on a cache hit.
            parent = ctx.incus.config_get_checked(full, labels.SPAWNED_BY)
            grant = ctx.incus.config_get_checked(full, labels.GRANT)
            role = "child" if parent else "orchestrator" if grant else "none"
            if role == "none":
                memory.env_ensured.discard(full)
                memory.env_roles.pop(full, None)
                _teardown_socket(
                    ctx.incus, memory, full, ctx.sender.config.get("volatile.uuid"), ctx.root
                )
                return
            if full in memory.env_ensured and memory.env_roles.get(full) == role:
                _ensure_socket(ctx, memory)
                return
            ensure_orchestration_env(ctx.incus, full, uid=ctx.cfg.container_user.uid)
            if role == "child":
                labels.write_child_role(ctx.incus, full, uid=ctx.cfg.container_user.uid)
            memory.env_ensured.add(full)
            memory.env_roles[full] = role
            _ensure_socket(ctx, memory)
    except (IncusError, OSError) as exc:
        memory.env_ensured.discard(full)
        memory.env_roles.pop(full, None)
        memory.warn_once(
            f"env:{full}",
            "broker: %s: could not prepare the orchestration inbox: %s",
            full,
            exc,
        )


#: Files read per `incus exec` while migrating one inbox directory. The read
#: helper returns the first N files by name (each at most `MAX_FILE_BYTES`), so
#: the directory is paged by deleting each page once it is in the store and
#: renaming its unparsable files aside, so every page brings new files.
MIGRATE_PAGE = 64


def _stamp_time(name: str, default: float) -> float:
    """The emission time encoded in an inbox file name, else `default`."""
    try:
        return datetime.strptime(name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return default


def _migrate_dir(
    ctx: ActionContext, store: BrokerStore, uuid: str, directory: str, *, acked: bool
) -> None:
    full = ctx.sender.full
    uid = ctx.cfg.container_user.uid
    seen: set[str] = set()
    while True:
        files = read_inbox_dir(ctx.incus, full, directory, uid=uid, limit=MIGRATE_PAGE)
        page = sorted(n for n in files if n.endswith(".json") and n not in seen)
        if not page:
            return
        seen.update(page)
        now = time.time()
        done: list[str] = []
        unparsable: list[str] = []
        for name in page:
            try:
                item = json.loads(files[name])
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise ValueError("not an inbox item")
            except ValueError:
                log.warning("broker: %s: set aside unparsable %s/%s", full, directory, name)
                unparsable.append(name)
                continue
            store.insert_item(
                uuid,
                full,
                item,
                now=_stamp_time(name, now),
                acked_at=now if acked else None,
            )
            if acked:
                # Inserted unread by an earlier pass, then moved to read/.
                store.ack(uuid, item["id"], now=now)
            done.append(name)
        # Only after every insert of the page succeeded (an error raised above).
        delete_inbox_files(ctx.incus, full, directory, done, uid=uid)
        # Renamed out of the `*.json` paging, so a page of them hides nothing.
        set_aside_inbox_files(ctx.incus, full, directory, unparsable, uid=uid)
        if len(files) < MIGRATE_PAGE:
            return


def _migrate_inbox(ctx: ActionContext, memory: BrokerMemory, uuid: str) -> bool:
    """Move a container's file inbox into the store before it becomes a recipient.

    Read history (`inbox/read/`) goes in acked, then unread `inbox/` items
    unacked, in file-name order, ids kept (duplicates are ignored), then
    `inbox/read/` again for files moved there meanwhile (acked). Files are
    removed only after their inserts; unparsable ones are renamed aside. True
    when the recipient row may be written: already migrated, or done now.
    False means retry next round.
    """
    store = memory.store
    assert store is not None
    full = ctx.sender.full
    if store.recipient_uuid(full) is not None:
        return True
    if not ctx.sender.running:
        return False
    try:
        _migrate_dir(ctx, store, uuid, INBOX_READ_DIR, acked=True)
        _migrate_dir(ctx, store, uuid, INBOX_DIR, acked=False)
        # A file moved to read/ while the inbox/ pass ran was inserted unread:
        # this pass acks it.
        _migrate_dir(ctx, store, uuid, INBOX_READ_DIR, acked=True)
    except (IncusError, OSError, StoreError, OutboxReadError) as exc:
        memory.warn_once(
            f"migrate:{full}", "broker: %s: could not migrate the file inbox: %s", full, exc
        )
        return False
    return True


def _ensure_socket(ctx: ActionContext, memory: BrokerMemory) -> None:
    """Open the listener, attach the device and record the recipient (idempotent)."""
    listener, store = memory.listener, memory.store
    uuid = ctx.sender.config.get("volatile.uuid")
    if listener is None or store is None or not uuid:
        return
    full = ctx.sender.full
    if uuid in memory.shared_uuids:
        return  # _check_socket_devices warned and tore down
    if listener.has_container(uuid) and memory.attached.get(full) == uuid:
        return
    try:
        if not listener.has_container(uuid):
            recorded = ctx.incus.config_device_get(full, ORCHESTRATION_DEVICE, "source")
            reused = (
                socket_device.reusable_dir(ctx.root, uuid, recorded)
                if (
                    memory.socket_devices_verified
                    and recorded not in memory.unsafe_socket_dirs
                    and not store.kv_get(f"socket-retired:{recorded}")
                )
                else None
            )
            if reused is None and recorded and socket_device.dir_uuid(recorded) == uuid:
                # Persist retirement before cleanup: an invisible or lazily
                # detached copy may hold it across another broker restart.
                store.kv_put(f"socket-retired:{recorded}", True)
                with contextlib.suppress(OSError):
                    socket_device.remove_dir(ctx.root, recorded)
            sdir = reused or socket_device.fresh_sock_dir(ctx.root, uuid)
            existed = sdir.exists()
            try:
                created = listener.open_container(uuid, sdir)
            except OSError:
                # The directory may be ours although the bind failed.
                if not existed and sdir.exists():
                    memory.reassert.add(uuid)
                raise
            if created:
                memory.reassert.add(uuid)
        live = listener.socket_dir(uuid)
        if live is None:
            raise OSError(f"no socket directory for {uuid}")
        socket_device.attach(ctx.incus, full, live, reassert=uuid in memory.reassert)
        memory.reassert.discard(uuid)
        if not _migrate_inbox(ctx, memory, uuid):
            return
        store.set_recipient(full, uuid, now=time.time())
        memory.attached[full] = uuid
    except (IncusError, OSError, StoreError) as exc:
        memory.attached.pop(full, None)
        memory.warn_once(
            f"socket:{full}", "broker: %s: could not set up the broker socket: %s", full, exc
        )


def _export_unacked(
    store: BrokerStore, memory: BrokerMemory, root: Path, full: str, uuid: str
) -> None:
    """Park each unacked store item as a pending file, then ack it.

    The file path's `flush_pending` delivers them; an item is acked only once
    its file is written, so a failure loses nothing.
    """
    for row in store.unacked(uuid):
        name = inbox_file_name_for(
            row.id, datetime.fromtimestamp(row.created_at, UTC), seq=next(memory.seq)
        )
        atomic_write_text(pending_dir(root, full) / name, json.dumps(row.item))
        store.ack(uuid, row.id, now=time.time())


def _teardown_socket(
    incus: Incus, memory: BrokerMemory, full: str, uuid: str | None, root: Path
) -> None:
    """Remove the device, close the listener and drop the recipient row.

    Unacked store items move to the pending dir first, so none is lost.
    """
    listener, store = memory.listener, memory.store
    if listener is None or store is None:
        return
    try:
        uuid = uuid or memory.attached.get(full) or store.recipient_uuid(full)
        if uuid:
            _export_unacked(store, memory, root, full, uuid)
            # Closed before the detach, so a failed detach still fails closed.
            listener.close_container(uuid)
        socket_device.detach(incus, full)
        store.remove_recipient(full)
        if uuid:  # an insert that raced the row's removal
            _export_unacked(store, memory, root, full, uuid)
        memory.attached.pop(full, None)
        memory.reassert.discard(uuid or "")
    except (IncusError, OSError, StoreError) as exc:
        memory.warn_once(
            f"socket-off:{full}", "broker: %s: could not remove the broker socket: %s", full, exc
        )


def _forget_vanished(raw: list[dict[str, Any]], memory: BrokerMemory) -> None:
    """Close listeners and drop state of containers that are gone or recreated.

    No device call: a vanished container has no device to remove, and a
    recreated one is set up (under its new uuid) when it is next served.
    """
    listener, store = memory.listener, memory.store
    if listener is None or store is None:
        return
    live: dict[str, str | None] = {}
    for entry in raw:
        name, config = entry.get("name"), entry.get("config")
        if isinstance(name, str) and isinstance(config, dict):
            uuid = config.get("volatile.uuid")
            live[name] = uuid if isinstance(uuid, str) and uuid else None
    try:
        recipients = store.recipients()
    except StoreError as exc:
        memory.warn_once("socket-recipients", "broker: cannot read the recipients: %s", exc)
        recipients = {}
    for full in set(memory.attached) | set(recipients):
        current = live.get(full, "")  # "" -> the name is gone
        if current is None:
            continue  # present but without a uuid: nothing to compare
        known = {u for u in (memory.attached.get(full), recipients.get(full)) if u}
        stale = known if current == "" else {u for u in known if u != current}
        if not stale:
            continue
        try:
            for uuid in stale:
                listener.close_container(uuid)
                memory.reassert.discard(uuid)
            memory.attached.pop(full, None)
            if recipients.get(full) in stale:
                store.remove_recipient(full)
        except (OSError, StoreError) as exc:
            memory.warn_once(f"socket-gone:{full}", "broker: %s: %s", full, exc)


def _shared_uuids(raw: list[dict[str, Any]]) -> set[str]:
    seen: set[str] = set()
    shared: set[str] = set()
    for entry in raw:
        config = entry.get("config")
        uuid = config.get("volatile.uuid") if isinstance(config, dict) else None
        if isinstance(uuid, str) and uuid:
            (shared if uuid in seen else seen).add(uuid)
    return shared


def _rotate_socket(incus: Incus, memory: BrokerMemory, root: Path, uuid: str, copy: str) -> None:
    """Move ``uuid``'s socket away from the directory ``copy`` mounts.

    Every connection drops and the old directory is removed, so the copy's
    mount (stale or lazily detached) leads nowhere; the owner's device is
    re-attached to the fresh directory. When the move fails the socket is
    closed instead (fail closed): the owner is set up afresh next round.
    """
    listener = memory.listener
    assert listener is not None
    owner = next((full for full, u in memory.attached.items() if u == uuid), None)
    fresh = socket_device.fresh_sock_dir(root, uuid)
    try:
        listener.rotate_container(uuid, fresh)
    except OSError as exc:
        memory.warn_once(f"socket-rotate:{uuid}", "broker: %s: %s", copy, exc)
        with contextlib.suppress(OSError):
            listener.close_container(uuid)
        if owner is not None:
            memory.attached.pop(owner, None)
        return
    log.warning("broker: %s mounted the socket of %s; moved it to %s", copy, uuid, fresh)
    if owner is None:
        return
    try:
        socket_device.attach(incus, owner, fresh, reassert=True)
    except (IncusError, OSError) as exc:
        memory.attached.pop(owner, None)  # _ensure_socket retries
        memory.warn_once(f"socket:{owner}", "broker: %s: %s", owner, exc)


def _strip_copy(
    incus: Incus,
    memory: BrokerMemory,
    root: Path,
    entry: dict[str, Any],
    name: str,
    project: str | None,
) -> None:
    """Remove a foreign socket device; move the socket if it named the live directory."""
    where = name if project is None else f"{project}/{name}"
    source = socket_device.source_uuid(entry)
    path = socket_device.source(entry)
    listener, store = memory.listener, memory.store
    assert listener is not None and store is not None
    if source and path:
        memory.unsafe_socket_dirs.add(path)
        try:
            # Keep the foreign device visible until its directory is durably
            # retired, even when removal or a later rotation fails.
            store.kv_put(f"socket-retired:{path}", True)
        except StoreError as exc:
            memory.warn_once(f"socket-retire:{path}", "broker: %s: %s", path, exc)
            with contextlib.suppress(OSError):
                listener.close_container(source)
            return
    log.warning("broker: %s carries another instance's socket device; removing", where)
    try:
        socket_device.detach(incus, name, project=project)
    except (IncusError, OSError) as exc:
        memory.warn_once(f"socket-off:{where}", "broker: %s: %s", where, exc)
    # Whether or not the detach worked (it may fail, or be lazy while the
    # copy holds the directory), a copy that mounts the source's live
    # directory moves the source to a fresh one.
    if not source or not path:
        return
    if str(listener.socket_dir(source)) == path:
        _rotate_socket(incus, memory, root, source, where)
        return
    # Not served right now (e.g. the first round after a broker restart): the
    # source must never be served from this directory again.
    try:
        if socket_device.remove_dir(root, path):
            log.warning("broker: removed the socket directory %s a copy mounted", path)
    except OSError as exc:
        memory.warn_once(f"socket-dir:{path}", "broker: %s: %s", path, exc)


def _other_projects(incus: Incus, memory: BrokerMemory) -> list[dict[str, Any]] | None:
    """The instances of every other Incus project, or None when they cannot be listed.

    A raw `incus copy --target-project` (or an import there) is invisible to
    the plain listing, yet may mount a served socket.
    """
    try:
        current = incus.current_project()
        everything = incus.list_containers(fast=True, all_projects=True)
    except (IncusError, OSError, ValueError) as exc:
        memory.warn_once("socket-projects", "broker: cannot list all Incus projects: %s", exc)
        return None
    if not isinstance(current, str) or not current:
        return None
    return [
        e for e in everything if isinstance(e.get("project"), str) and e.get("project") != current
    ]


def _check_socket_devices(
    incus: Incus,
    raw: list[dict[str, Any]],
    memory: BrokerMemory,
    root: Path,
    others: list[dict[str, Any]] | None = None,
) -> None:
    """Strip foreign devices, tear down lost roles and notice vanished devices.

    ``others`` (other projects' instances) are only checked for copies and
    twins of served containers; nothing else of theirs is touched.
    """
    if memory.listener is None or memory.store is None:
        return
    memory.socket_devices_verified = others is not None
    others = others or []
    _forget_vanished(raw, memory)
    memory.shared_uuids = _shared_uuids(raw + others)
    for entry in others:
        name, config = entry.get("name"), entry.get("config")
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        if not socket_device.has_device(entry):
            continue
        if socket_device.mismatched(entry) or config.get("volatile.uuid") in memory.shared_uuids:
            _strip_copy(incus, memory, root, entry, name, str(entry["project"]))
    for entry in raw:
        name = entry.get("name")
        config = entry.get("config")
        if not isinstance(name, str) or not isinstance(config, dict):
            continue
        uuid = config.get("volatile.uuid")
        if uuid in memory.shared_uuids:
            # The socket identifies a caller by uuid only: two instances with
            # one uuid (an `incus import` beside the original) would speak as
            # each other, so neither gets the socket (file transport instead).
            memory.warn_once(
                f"socket-shared:{name}",
                "broker: %s and another instance share the instance uuid %s; "
                "no broker socket for either",
                name,
                uuid,
            )
            if name in memory.attached or socket_device.has_device(entry):
                # No uuid: only the recorded recipient's unread items are
                # exported, to it, never to a twin listed first.
                _teardown_socket(incus, memory, name, None, root)
            continue
        if socket_device.mismatched(entry):
            _strip_copy(incus, memory, root, entry, name, None)
            memory.attached.pop(name, None)
            continue
        if _labelled(config):
            # A snapshot restore can keep the uuid and role but rewind the
            # device to an old generation. Invalidate _ensure_socket's cache
            # using this round's listing, without another per-container call.
            if name in memory.attached and socket_device.source(entry) != str(
                memory.listener.socket_dir(memory.attached[name])
            ):
                memory.attached.pop(name, None)
        elif name in memory.attached or socket_device.has_device(entry):
            _teardown_socket(incus, memory, name, config.get("volatile.uuid"), root)


def _serve(ctx: ActionContext, journal: Journal, memory: BrokerMemory) -> None:
    """One running orchestrator or child: parked messages, grant copy, outbox."""
    _ensure_env(ctx, memory)
    uid = ctx.cfg.container_user.uid
    # Parked messages first, so a new status never overtakes them.
    flush_pending(
        ctx.incus, ctx.root, ctx.sender.full, uid=uid, finish=finishing.flush_callback(ctx)
    )
    if ctx.role() == "orchestrator":
        _sync_grant_json(ctx, memory)
    path = finishing.reply_path(ctx, ctx.sender)
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get("ref"):
            _reply(ctx, saved["ref"], Outcome(saved["state"], saved["detail"]), memory)
    publishing.settle(ctx, lambda ref, out: _reply(ctx, ref, out, memory))
    for raw in read_outbox(ctx.incus, ctx.sender.full, uid=uid):
        _handle_raw(ctx, raw, journal, memory)


def _settle_queue(ctx: ActionContext, memory: BrokerMemory) -> None:
    """Start what the queue can start, or drop it when the grant is gone; report each."""
    if ctx.grant() is None:
        # A grant label still there means the repo cap forbids orchestration.
        reason = GRANT_REVOKED if ctx.raw_grant() is None else NOT_ALLOWED
        for req in drop_queue(ctx, reason):
            _reply(ctx, req.manifest_id, refused(reason), memory)
        return
    for req, outcome in drain_queue(ctx):
        _watch_drained(ctx, req, outcome)
        _reply(ctx, req.manifest_id, outcome, memory)


def _nodes(raw: list[dict[str, object]], configs: Mapping[str, Config | None]) -> dict[str, Node]:
    from jailbee.lifecycle import short_name

    nodes: dict[str, Node] = {}
    for entry in raw:
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        conf = entry.get("config")
        config = {str(k): str(v) for k, v in conf.items()} if isinstance(conf, dict) else {}
        cfg = owner_config(name, config, configs)
        short = short_name(cfg, name) if cfg is not None else name
        started_at = None
        last_used = entry.get("last_used_at")
        if isinstance(last_used, str):
            try:
                boot = datetime.fromisoformat(last_used)
                if boot.year > 1970 and boot.tzinfo is not None:
                    started_at = boot.timestamp()
            except ValueError:
                pass
        state = entry.get("state")
        state_data: dict[str, object] = state if isinstance(state, dict) else {}
        cpu = state_data.get("cpu")
        memory = state_data.get("memory")
        nodes[name] = Node(
            full=name,
            short=short,
            running=entry.get("status") == "Running",
            config=config,
            started_at=started_at,
            init_pid=_positive_int(state_data.get("pid")),
            cpu_usage_ns=_positive_int(cpu.get("usage") if isinstance(cpu, dict) else None),
            memory_usage=_positive_int(memory.get("usage") if isinstance(memory, dict) else None),
        )
    return nodes


def _positive_int(value: object) -> int | None:
    """An int > 0, else None; bools are ints in Python and are rejected."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def tick(
    incus: Incus,
    *,
    root: Path,
    journal: Journal,
    now: datetime,
    memory: BrokerMemory | None = None,
) -> bool:
    """Run one broker round; True while any running orchestrator or child exists."""
    memory = memory if memory is not None else BrokerMemory()
    # Release finished workers before taking the snapshot that replaces their slots.
    prune_reservations(root)
    memory.publish_threads[:] = [worker for worker in memory.publish_threads if worker.is_alive()]
    raw = incus.list_containers()
    others = _other_projects(incus, memory) if memory.store is not None else None
    _check_socket_devices(incus, raw, memory, root, others)
    _purge_messages(memory, raw, now, None if others is None else raw + others)
    candidates = [
        e
        for e in raw
        if isinstance(e.get("name"), str)
        and isinstance(e.get("config"), dict)
        and _labelled(e["config"])
    ]
    active = any(e.get("status") == "Running" for e in candidates)
    # A container that is gone (or recreated later) gets grant.json written afresh.
    present = {e.get("name") for e in raw}
    for full in [f for f in memory.grant_written if f not in present]:
        del memory.grant_written[full]
    memory.env_ensured.intersection_update(present)
    for full in [f for f in memory.children if f not in present]:
        del memory.children[full]
    # Warn workers before deleting a vanished parent's queued finish reports.
    unnotified = finishing.notify_orphaned_finishes(
        incus,
        root,
        {n for n in present if isinstance(n, str)},
        {str(e["name"]) for e in raw if e.get("status") == "Running"},
        now=now,
        seq=memory.seq,
    )
    # Parked messages of a vanished container must never reach a namesake.
    # A directory still holding a report whose notice failed is kept for a retry.
    keep = {n for n in present if isinstance(n, str)} | unnotified
    for full in purge_vanished_pending(root, keep):
        log.info("broker: dropped parked messages of vanished %s", full)
    queues = load_queue(root)
    identities = {
        str(e["name"]): str((e.get("config") or {}).get("volatile.uuid") or e["name"])
        for e in raw
        if isinstance(e.get("name"), str) and isinstance(e.get("config"), dict)
    }
    try:
        publishing.prune(root, identities, now)
        publishes = publishing.inflight(root)
        former_publishers = publishing.known_senders(root)
    except Exception:
        log.exception("broker: publish state maintenance failed")
        publishes = {}
        former_publishers = set()
    parked = [
        e["name"]
        for e in raw
        if isinstance(e.get("name"), str)
        and e.get("status") == "Running"
        and not (isinstance(e.get("config"), dict) and _labelled(e["config"]))
        and has_pending(root, e["name"])
    ]
    # Before the early return: a later namesake must not inherit stale entries.
    _forget_vanished_watches(root, present)
    if (
        not candidates
        and not queues
        and not parked
        and not publishes
        and not (former_publishers & present)
    ):
        memory.contexts.clear()
        return False

    try:
        gcfg = load_global_config(default_global_config_path())[0]
    except Exception:
        log.exception("broker: global config failed to load; skipping this round")
        return True
    configs = repo_configs(memory)
    nodes = _nodes(raw, configs)

    def context(node: Node) -> ActionContext | None:
        cfg = owner_config(node.full, node.config, configs)
        if cfg is None:
            memory.warn_once(
                f"no-config:{node.full}",
                "broker: %s has no loadable registered repo config; skipped",
                node.full,
            )
            return None
        return ActionContext(
            cfg=cfg,
            gcfg=gcfg,
            incus=incus,
            root=root,
            sender=node,
            nodes=nodes,
            now=now,
            seq=memory.seq,
        )

    # Before anything is served, so a `stop` handled later in this round keeps
    # the STOPPED_HEAD it records (the snapshot above predates it).
    for node in nodes.values():
        if node.running and (
            node.config.get(labels.STOPPED_HEAD)
            or node.config.get(labels.STOPPED_DIRTY)
            or node.config.get(labels.STOPPED_OUTBOX)
        ):
            _clear_stale_stop_labels(incus, node)

    orchestrators: list[ActionContext] = []
    for key in [k for k in memory.contexts if k not in {n.key for n in nodes.values()}]:
        del memory.contexts[key]
    for node in nodes.values():
        memory.contexts.pop(node.key, None)
        if not _labelled(node.config):
            try:
                if node.running and node.full in former_publishers:
                    former_ctx = context(node)
                    if former_ctx is not None:
                        _serve(former_ctx, journal, memory)
                    continue
                if node.full in publishes:
                    publish_ctx = context(node)
                    if publish_ctx is not None:
                        publishing.settle(publish_ctx, partial(_reply, publish_ctx, memory=memory))
                if node.running and has_pending(root, node.full):
                    _flush_unlabelled(node, context, root, incus)
            except Exception:
                log.exception("broker: serving former publisher %s failed", node.full)
            continue
        try:
            ctx = context(node)
            if ctx is None:
                continue
            if ctx.role() == "none":
                memory.warn_once(
                    f"bad-grant:{node.full}",
                    "broker: %s has an unparseable orchestration grant label; ignored",
                    node.full,
                )
                # Its inbox leaves the store (nothing is lost), then parked
                # messages, including those, still get delivered.
                _teardown_socket(incus, memory, node.full, node.config.get("volatile.uuid"), root)
                if node.running and has_pending(root, node.full):
                    flush_pending(
                        incus,
                        root,
                        node.full,
                        uid=ctx.cfg.container_user.uid,
                        finish=finishing.flush_callback(ctx),
                    )
                continue
            memory.contexts[node.key] = ctx
            if ctx.role() == "orchestrator":
                orchestrators.append(ctx)
            if node.running:
                _serve(ctx, journal, memory)
        except Exception:
            log.exception("broker: serving %s failed", node.full)

    # Handlers changed labels and may have stopped children after our initial
    # snapshot. Refresh once before cleanup; share deletions with queue drain.
    if orchestrators:
        nodes = _nodes(incus.list_containers(), configs)
        for ctx in orchestrators:
            ctx.nodes = nodes
            ctx.sender = nodes.get(ctx.sender.full, ctx.sender)
            try:
                finishing.settle_destroys(ctx)
            except Exception:
                log.exception("broker: finished children of %s failed", ctx.sender.full)

    # After every round: queues move whenever a slot may have freed up.
    seen = {ctx.sender.full for ctx in orchestrators}
    for ctx in orchestrators:
        try:
            _settle_queue(ctx, memory)
        except Exception:
            log.exception("broker: queue of %s failed", ctx.sender.full)
        if ctx.sender.running:
            try:
                _remember_children(ctx, memory, inbox_state.sync_children(ctx, memory.status))
            except Exception:
                log.exception("broker: child inbox state of %s failed", ctx.sender.full)
    memory.status.prune(set(nodes), seen)
    for full in queues:
        if full in seen:
            continue
        try:
            queued_node = nodes.get(full)
            ctx = context(queued_node) if queued_node is not None else None
            if ctx is not None:
                # No longer an orchestrator (grant label removed): grant() is None.
                _settle_queue(ctx, memory)
            elif queued_node is None:
                log.warning("broker: dropping the queue of vanished %s", full)
                current = load_queue(root)
                current.pop(full, None)
                save_queue(root, current)
        except Exception:
            log.exception("broker: queue of %s failed", full)
    # Queue launches can finish before watch settlement; the earlier snapshot
    # may still say Stopped even though the agent is already running.
    if orchestrators and any(watch.load_watch(root).values()):
        nodes = _nodes(incus.list_containers(), configs)
        for ctx in orchestrators:
            ctx.nodes = nodes
            ctx.sender = nodes.get(ctx.sender.full, ctx.sender)
    _settle_all_watches(root, orchestrators, present, memory)
    return active


def _settle_all_watches(
    root: Path, orchestrators: list[ActionContext], present: set[object], memory: BrokerMemory
) -> None:
    """Settle each orchestrator's watches."""
    jobs = watch.read_jobs()
    if jobs is None:
        memory.warn_once(
            "watch-db", "broker: job database unreadable; started children are not reported"
        )
    for ctx in orchestrators:
        try:
            _settle_watches(ctx, memory, jobs)
        except Exception:
            log.exception("broker: watches of %s failed", ctx.sender.full)


def _forget_vanished_watches(root: Path, present: set[object]) -> None:
    """Drop the watch entries of orchestrators that no longer exist."""
    with watch.locked(root):
        watches = watch.load_watch(root)
        gone = [full for full in watches if full not in present]
        if gone:
            for full in gone:
                log.warning("broker: dropping the watches of vanished %s", full)
                del watches[full]
            watch.save_watch(root, watches)


def _clear_stale_stop_labels(incus: Incus, node: Node) -> None:
    """A running container's recorded stop-time HEAD and dirty flag are stale."""
    try:
        for key in (labels.STOPPED_HEAD, labels.STOPPED_DIRTY, labels.STOPPED_OUTBOX):
            if node.config.get(key):
                incus.config_unset_checked(node.full, key)
    except Exception:
        log.exception("broker: clearing the stale stop labels of %s failed", node.full)


def _flush_unlabelled(
    node: Node, context: Callable[[Node], ActionContext | None], root: Path, incus: Incus
) -> None:
    """Flush parked messages of a running container without orchestration labels."""
    try:
        ctx = context(node)
        if ctx is not None:
            flush_pending(
                incus,
                root,
                node.full,
                uid=ctx.cfg.container_user.uid,
                finish=finishing.flush_callback(ctx),
            )
    except Exception:
        log.exception("broker: flushing %s failed", node.full)


def _pause(seconds: float, stop: StopFlag) -> None:
    """Sleep `seconds` in short slices, returning early once a stop is requested."""
    deadline = time.monotonic() + seconds
    while not stop.requested:
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(SLEEP_SLICE, left))


def _installed_version() -> str | None:
    """The jailbee version installed on disk now, or None when it cannot be read."""
    try:
        return _metadata_version("jailbee")
    except PackageNotFoundError:
        return None


def _stage_container_app(memory: BrokerMemory) -> None:
    from jailbee import container_app
    from jailbee.agent_skills import sync_agent_skills

    try:
        container_app.stage()
    except Exception:
        log.exception("broker: jailbee app staging failed; continuing")
        return
    try:
        configs = repo_configs(memory)
    except Exception:
        log.exception("broker: cannot load repos for skills sync")
        return
    for cfg in configs.values():
        if cfg is not None:
            try:
                sync_agent_skills(cfg)
            except Exception:
                log.exception("broker: skills sync failed")


def run_broker(incus: Incus) -> int:
    """Body of `jailbee _broker`: loop until SIGTERM (or SIGINT), then exit 0.

    The signal handler only sets a flag, checked between rounds, so a round is
    never interrupted between acting on a manifest and journalling it. Once a
    different jailbee version is installed, the broker exits `UPGRADED_EXIT`
    after the round, so systemd restarts it on the new code.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    root = state_root()
    # One broker per host: two would act on the same manifests and queues.
    # The lock dies with the process, so a crash never leaves it held.
    lock = (root / LOCK_FILE).open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        log.info("another orchestration broker is running; exiting")
        return 0
    stop = StopFlag()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    memory = BrokerMemory()
    memory.store = BrokerStore(store_path(root))
    memory.listener = Listener(memory.requests)
    memory.listener.start()
    try:
        journal = Journal(root)
        publishing.recover(root)
        log.info("orchestration broker starting")
        _stage_container_app(memory)
        from jailbee.orchestration import executor

        while not stop.requested:
            try:
                active = tick(
                    incus, root=root, journal=journal, now=datetime.now(UTC), memory=memory
                )
            except Exception:
                log.exception("broker: round failed")
                active = False
            installed = _installed_version()
            if installed is not None and installed != __version__:
                log.info("jailbee %s installed, running %s; restarting", installed, __version__)
                return UPGRADED_EXIT
            interval = ACTIVE_INTERVAL if active else IDLE_INTERVAL
            if memory.listener is not None:
                executor.evaluate_waiters(memory)
                executor.serve_requests(memory, until=time.monotonic() + interval, stop=stop)
            else:
                _pause(interval, stop)
        log.info("orchestration broker stopping")
        return 0
    finally:
        deadline = time.monotonic() + 25.0
        for worker in memory.publish_threads:
            if worker.is_alive():
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if memory.listener is not None:
            memory.listener.stop()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        lock.close()
