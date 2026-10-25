"""The orchestration broker: one long-lived loop that serves every outbox."""

from __future__ import annotations

import itertools
import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from jailbee.config import load_repo_config
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.orchestration import labels
from jailbee.orchestration.actions import (
    GRANT_REVOKED,
    ActionContext,
    Node,
    Outcome,
    dispatch,
    drain_queue,
    drop_queue,
    refused,
)
from jailbee.orchestration.journal import Journal, manifest_digest
from jailbee.orchestration.outbox import (
    delete_outbox_files,
    deliver,
    flush_pending,
    read_outbox,
    write_grant_json,
)
from jailbee.orchestration.protocol import (
    InboxStatus,
    ManifestError,
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
    from collections.abc import Iterator, Mapping
    from types import FrameType

    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.orchestration.outbox import RawManifest

log = logging.getLogger(__name__)

ACTIVE_INTERVAL = 5.0
IDLE_INTERVAL = 60.0
SLEEP_SLICE = 1.0


@dataclass
class BrokerMemory:
    """What the broker remembers between rounds (in memory only)."""

    # Orchestrator full name -> grant.json text last written into it.
    grant_written: dict[str, str] = field(default_factory=dict)
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
        _reply(ctx, ref, outcome, memory)
    delete_outbox_files(ctx.incus, ctx.sender.full, names, uid=uid)


def _serve(ctx: ActionContext, journal: Journal, memory: BrokerMemory) -> None:
    """One running orchestrator or child: parked messages, grant copy, outbox."""
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
        for req in drop_queue(ctx, GRANT_REVOKED):
            _reply(ctx, req.manifest_id, refused(GRANT_REVOKED), memory)
        return
    for req, outcome in drain_queue(ctx):
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
    # Parked messages of a vanished container must never reach a namesake.
    for full in purge_vanished_pending(root, {n for n in present if isinstance(n, str)}):
        log.info("broker: dropped parked messages of vanished %s", full)
    queues = load_queue(root)
    if not candidates and not queues:
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
            cfg=cfg, gcfg=gcfg, incus=incus, root=root, sender=node, nodes=nodes, now=now
        )

    orchestrators: list[ActionContext] = []
    for node in nodes.values():
        if not _labelled(node.config):
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
    return active


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
    stop = StopFlag()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    memory = BrokerMemory()
    try:
        root = state_root()
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
