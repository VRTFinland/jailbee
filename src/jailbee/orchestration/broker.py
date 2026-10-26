"""The orchestration broker: one long-lived loop that serves every outbox."""

from __future__ import annotations

import fcntl
import itertools
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from jailbee.config import load_repo_config
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.incus import IncusError
from jailbee.orchestration import labels, watch
from jailbee.orchestration.actions import (
    GRANT_REVOKED,
    NOT_ALLOWED,
    ActionContext,
    Node,
    Outcome,
    dispatch,
    drain_queue,
    drop_queue,
    live_job_kind,
    refused,
)
from jailbee.orchestration.journal import Journal, manifest_digest
from jailbee.orchestration.outbox import (
    delete_outbox_files,
    deliver,
    ensure_orchestration_env,
    flush_pending,
    has_pending,
    read_outbox,
    write_grant_json,
)
from jailbee.orchestration.protocol import (
    InboxStatus,
    ManifestError,
    RestartManifest,
    SpawnManifest,
    body_file_of,
    new_host_id,
    parse_manifest,
)
from jailbee.orchestration.state import (
    load_queue,
    purge_vanished_pending,
    save_queue,
    state_root,
)
from jailbee.outbox_io import _validate_outbox_name

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from types import FrameType

    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.orchestration.outbox import RawManifest
    from jailbee.orchestration.protocol import Manifest
    from jailbee.orchestration.state import QueuedRequest
    from jailbee.orchestration.watch import JobSnapshot

log = logging.getLogger(__name__)

ACTIVE_INTERVAL = 5.0
IDLE_INTERVAL = 60.0
SLEEP_SLICE = 1.0
LOCK_FILE = "broker.lock"


@dataclass
class BrokerMemory:
    """What the broker remembers between rounds (in memory only)."""

    # Orchestrator full name -> grant.json text last written into it.
    grant_written: dict[str, str] = field(default_factory=dict)
    env_ensured: set[str] = field(default_factory=set)
    # Keys of warnings already logged, so a standing problem is logged once.
    warned: set[str] = field(default_factory=set)
    # Inbox file sequence: orders the items emitted within one second.
    seq: Iterator[int] = field(default_factory=itertools.count)

    def warn_once(self, key: str, msg: str, *args: object) -> None:
        if key not in self.warned:
            self.warned.add(key)
            log.warning(msg, *args)


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


def _track(
    ctx: ActionContext, ref: str, child_full: str, kind: Literal["spawn", "restart"]
) -> None:
    """Remember a started child so a later round can report how it came up.

    `since` is `ctx.now`, the round's start: the job row a spawn creates exists
    only after that, so a genuine failure is never mistaken for a stale row.
    """
    watches = watch.load_watch(ctx.root)
    mine = [e for e in watches.get(ctx.sender.full, []) if e.child_full != child_full]
    mine.append(watch.WatchEntry(ref, child_full, kind, ctx.now.isoformat()))
    watches[ctx.sender.full] = mine
    watch.save_watch(ctx.root, watches)


def _track_restart(ctx: ActionContext, ref: str, child_full: str, outcome: Outcome) -> None:
    """Watch a restart only when detached work (a boot or autostart job) is still running."""
    if outcome.state not in ("done", "started"):
        return
    if live_job_kind(child_full) is not None:
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
    if jobs is None:
        return
    from jailbee.lifecycle import short_name

    watches = watch.load_watch(ctx.root)
    entries = watches.get(ctx.sender.full, [])
    for entry in list(entries):
        outcome = watch.verdict(
            entry,
            jobs.get(entry.child_full),
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


def _sync_grant_json(ctx: ActionContext, memory: BrokerMemory) -> None:
    """Write the effective grant into an orchestrator when it changed since the last write."""
    grant = ctx.grant()
    if grant is None:
        memory.grant_written.pop(ctx.sender.full, None)
        return
    text = grant.to_label() + "\n"
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
    if journal.seen(ctx.sender.key, digest):
        delete_outbox_files(ctx.incus, ctx.sender.full, names, uid=uid)
        return
    ref = PurePosixPath(raw.file_name).stem
    outcome: Outcome | None
    manifest: Manifest | None = None
    try:
        manifest = parse_manifest(raw.file_name, raw.text, raw.files)
    except ManifestError as e:
        outcome = refused(str(e))
    else:
        ref = manifest.id
        try:
            outcome = dispatch(ctx, manifest)
        except Exception:
            # The detail goes to the container; the exception text stays in the log.
            log.exception("broker: %s: handler for %s failed", ctx.sender.full, raw.file_name)
            outcome = refused("broker error")
    # Journal before anything else: a crash after acting must never act again.
    journal.record(
        ctx.sender.key,
        digest,
        ref,
        outcome.state if outcome else "ignored",
        ctx.now,
        container_full=ctx.sender.full,
    )
    if outcome is not None:
        _watch_manifest(ctx, manifest, ref, outcome)
        _reply(ctx, ref, outcome, memory)
    delete_outbox_files(ctx.incus, ctx.sender.full, names, uid=uid)


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

    Upgrades restart the broker, pushing changed helpers then. Fork children
    copy an ensured orchestrator; new children use the current golden image.
    A vanished name is forgotten so a recreated older image is ensured again.
    """
    full = ctx.sender.full
    if full in memory.env_ensured:
        return
    try:
        ensure_orchestration_env(ctx.incus, full, uid=ctx.cfg.container_user.uid)
    except IncusError as exc:
        memory.warn_once(
            f"env:{full}",
            "broker: %s: could not install the orchestration inbox helper: %s",
            full,
            exc,
        )
    else:
        memory.env_ensured.add(full)


def _serve(ctx: ActionContext, journal: Journal, memory: BrokerMemory) -> None:
    """One running orchestrator or child: parked messages, grant copy, outbox."""
    _ensure_env(ctx, memory)
    uid = ctx.cfg.container_user.uid
    # Parked messages first, so a new status never overtakes them.
    flush_pending(ctx.incus, ctx.root, ctx.sender.full, uid=uid)
    if ctx.role() == "orchestrator":
        _sync_grant_json(ctx, memory)
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
        nodes[name] = Node(
            full=name, short=short, running=entry.get("status") == "Running", config=config
        )
    return nodes


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
    raw = incus.list_containers()
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
    # Parked messages of a vanished container must never reach a namesake.
    for full in purge_vanished_pending(root, {n for n in present if isinstance(n, str)}):
        log.info("broker: dropped parked messages of vanished %s", full)
    queues = load_queue(root)
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
    if not candidates and not queues and not parked:
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
        if node.running and node.config.get(labels.STOPPED_HEAD):
            _clear_stale_stopped_head(incus, node)

    orchestrators: list[ActionContext] = []
    for node in nodes.values():
        if not _labelled(node.config):
            # A revoked grant leaves no label; parked messages still get delivered.
            if node.running and has_pending(root, node.full):
                _flush_unlabelled(node, context, root, incus)
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
                # Messages parked while it was an orchestrator still get delivered.
                if node.running and has_pending(root, node.full):
                    flush_pending(incus, root, node.full, uid=ctx.cfg.container_user.uid)
                continue
            if ctx.role() == "orchestrator":
                orchestrators.append(ctx)
            if node.running:
                _serve(ctx, journal, memory)
        except Exception:
            log.exception("broker: serving %s failed", node.full)

    # After every round: queues move whenever a slot may have freed up.
    seen = {ctx.sender.full for ctx in orchestrators}
    for ctx in orchestrators:
        try:
            _settle_queue(ctx, memory)
        except Exception:
            log.exception("broker: queue of %s failed", ctx.sender.full)
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
        return
    for ctx in orchestrators:
        try:
            _settle_watches(ctx, memory, jobs)
        except Exception:
            log.exception("broker: watches of %s failed", ctx.sender.full)


def _forget_vanished_watches(root: Path, present: set[object]) -> None:
    """Drop the watch entries of orchestrators that no longer exist."""
    watches = watch.load_watch(root)
    gone = [full for full in watches if full not in present]
    if gone:
        for full in gone:
            log.warning("broker: dropping the watches of vanished %s", full)
            del watches[full]
        watch.save_watch(root, watches)


def _clear_stale_stopped_head(incus: Incus, node: Node) -> None:
    """Unset STOPPED_HEAD of a running container: it was started outside the
    broker (whose own restart clears it), so the recorded HEAD is stale."""
    try:
        incus.config_unset_checked(node.full, labels.STOPPED_HEAD)
    except Exception:
        log.exception("broker: clearing the stale stopped HEAD of %s failed", node.full)


def _flush_unlabelled(
    node: Node, context: Callable[[Node], ActionContext | None], root: Path, incus: Incus
) -> None:
    """Flush parked messages of a running container without orchestration labels."""
    try:
        ctx = context(node)
        if ctx is not None:
            flush_pending(incus, root, node.full, uid=ctx.cfg.container_user.uid)
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


def run_broker(incus: Incus) -> int:
    """Body of `jailbee _broker`: loop until SIGTERM (or SIGINT), then exit 0.

    The signal handler only sets a flag, checked between rounds, so a round is
    never interrupted between acting on a manifest and journalling it.
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
    try:
        journal = Journal(root)
        log.info("orchestration broker starting")
        while not stop.requested:
            try:
                active = tick(
                    incus, root=root, journal=journal, now=datetime.now(UTC), memory=memory
                )
            except Exception:
                log.exception("broker: round failed")
                active = False
            _pause(ACTIVE_INTERVAL if active else IDLE_INTERVAL, stop)
        log.info("orchestration broker stopping")
        return 0
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        lock.close()
