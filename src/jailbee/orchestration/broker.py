"""The orchestration broker: one long-lived loop that serves every outbox."""

from __future__ import annotations

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
from jailbee.orchestration.state import load_queue, save_queue, state_root

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import FrameType

    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.orchestration.outbox import RawManifest

log = logging.getLogger(__name__)

ACTIVE_INTERVAL = 5.0
IDLE_INTERVAL = 60.0


@dataclass
class BrokerMemory:
    """What the broker remembers between rounds (in memory only)."""

    # Orchestrator full name -> grant.json text last written into it.
    grant_written: dict[str, str] = field(default_factory=dict)
    # Containers already reported as belonging to no registered repo.
    unregistered_logged: set[str] = field(default_factory=set)


class StopBroker(BaseException):
    """Raised by the SIGTERM handler to leave the loop cleanly (not caught as an error)."""


def repo_configs() -> dict[str, Config]:
    """Container prefix -> loaded `Config`, for every registered repo that loads."""
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    with Session(get_engine()) as session:
        rows = [(r.container_prefix, r.repo_root) for r in session.exec(select(RegisteredRepo))]
    out: dict[str, Config] = {}
    for prefix, repo_root in rows:
        root = Path(repo_root)
        if not root.is_dir():
            continue
        try:
            cfg = load_repo_config(root)
        except Exception as e:
            log.warning("broker: skipping repo %s: config load failed: %s", prefix, e)
            continue
        out[cfg.container_prefix] = cfg
    return out


def config_for(full: str, configs: Mapping[str, Config]) -> Config | None:
    """The config whose prefix is the longest one `full` starts with (plus `-`)."""
    best: str | None = None
    for prefix in configs:
        if full.startswith(prefix + "-") and (best is None or len(prefix) > len(best)):
            best = prefix
    return None if best is None else configs[best]


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


def _reply(ctx: ActionContext, ref: str, outcome: Outcome) -> None:
    deliver(
        ctx.incus,
        ctx.root,
        ctx.sender.full,
        _status(ref, outcome),
        running=ctx.sender.running,
        uid=ctx.cfg.container_user.uid,
        now=ctx.now,
    )


def _sync_grant_json(ctx: ActionContext, memory: BrokerMemory) -> None:
    """Write the effective grant into an orchestrator when it changed since the last write."""
    grant = ctx.grant()
    if grant is None:
        return
    text = grant.to_label() + "\n"
    if memory.grant_written.get(ctx.sender.full) == text:
        return
    write_grant_json(ctx.incus, ctx.sender.full, text, uid=ctx.cfg.container_user.uid)
    memory.grant_written[ctx.sender.full] = text


def _handle_raw(ctx: ActionContext, raw: RawManifest, journal: Journal) -> None:
    uid = ctx.cfg.container_user.uid
    body = body_file_of(raw.file_name, raw.text)
    bodies = {body: raw.files[body]} if body is not None and body in raw.files else {}
    names = [raw.file_name, *bodies]
    digest = manifest_digest(raw.file_name, raw.text, bodies)
    if journal.seen(ctx.sender.full, digest):
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
        except Exception as e:
            log.exception("broker: %s: handler for %s failed", ctx.sender.full, raw.file_name)
            outcome = refused(f"broker error: {e}")
    # Journal before anything else: a crash after acting must never act again.
    journal.record(ctx.sender.full, digest, ref, outcome.state if outcome else "ignored", ctx.now)
    if outcome is not None:
        _reply(ctx, ref, outcome)
    delete_outbox_files(ctx.incus, ctx.sender.full, names, uid=uid)


def _serve(ctx: ActionContext, journal: Journal, memory: BrokerMemory) -> None:
    """One running orchestrator or child: parked messages, grant copy, outbox."""
    uid = ctx.cfg.container_user.uid
    # Parked messages first, so a new status never overtakes them.
    flush_pending(ctx.incus, ctx.root, ctx.sender.full, uid=uid)
    if ctx.role() == "orchestrator":
        _sync_grant_json(ctx, memory)
    for raw in read_outbox(ctx.incus, ctx.sender.full, uid=uid):
        _handle_raw(ctx, raw, journal)


def _settle_queue(ctx: ActionContext) -> None:
    """Start what the queue can start, or drop it when the grant is gone; report each."""
    if ctx.grant() is None:
        for req in drop_queue(ctx, GRANT_REVOKED):
            _reply(ctx, req.manifest_id, refused(GRANT_REVOKED))
        return
    for req, outcome in drain_queue(ctx):
        _reply(ctx, req.manifest_id, outcome)


def _nodes(raw: list[dict[str, object]], configs: Mapping[str, Config]) -> dict[str, Node]:
    from jailbee.lifecycle import short_name

    nodes: dict[str, Node] = {}
    for entry in raw:
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        conf = entry.get("config")
        config = {str(k): str(v) for k, v in conf.items()} if isinstance(conf, dict) else {}
        cfg = config_for(name, configs)
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
    queues = load_queue(root)
    if not candidates and not queues:
        return False

    try:
        gcfg = load_global_config(default_global_config_path())[0]
    except Exception:
        log.exception("broker: global config failed to load; skipping this round")
        return True
    configs = repo_configs()
    nodes = _nodes(raw, configs)

    def context(node: Node) -> ActionContext | None:
        cfg = config_for(node.full, configs)
        if cfg is None:
            if node.full not in memory.unregistered_logged:
                log.warning("broker: %s belongs to no registered repo; skipped", node.full)
                memory.unregistered_logged.add(node.full)
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
            if ctx is None or ctx.role() == "none":
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
            _settle_queue(ctx)
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
                _settle_queue(ctx)
            elif queued_node is None:
                log.warning("broker: dropping the queue of vanished %s", full)
                current = load_queue(root)
                current.pop(full, None)
                save_queue(root, current)
        except Exception:
            log.exception("broker: queue of %s failed", full)
    return active


def _on_sigterm(signum: int, frame: FrameType | None) -> None:
    raise StopBroker


def run_broker(incus: Incus) -> int:
    """Body of `jailbee _broker`: loop until SIGTERM."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    previous = signal.signal(signal.SIGTERM, _on_sigterm)
    memory = BrokerMemory()
    try:
        root = state_root()
        journal = Journal(root)
        log.info("orchestration broker starting")
        while True:
            try:
                active = tick(
                    incus, root=root, journal=journal, now=datetime.now(UTC), memory=memory
                )
            except Exception:
                log.exception("broker: round failed")
                active = False
            time.sleep(ACTIVE_INTERVAL if active else IDLE_INTERVAL)
    except (StopBroker, KeyboardInterrupt):
        log.info("orchestration broker stopping")
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
