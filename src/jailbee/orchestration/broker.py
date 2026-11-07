"""The orchestration broker: one long-lived loop that serves every outbox."""

from __future__ import annotations

import fcntl
import logging
import signal
import time
from datetime import UTC, datetime
from functools import partial
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _metadata_version
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee import __version__
from jailbee.global_config import default_global_config_path, load_global_config
from jailbee.orchestration import (
    broker_replies,
    broker_state,
    finishing,
    inbox_state,
    labels,
    manifests,
    publishing,
    query,
    retention,
    socket_lifecycle,
    watch,
)
from jailbee.orchestration.actions import (
    ActionContext,
    Node,
    prune_reservations,
)
from jailbee.orchestration.journal import Journal
from jailbee.orchestration.listener import Listener
from jailbee.orchestration.outbox import (
    flush_pending,
    has_pending,
)
from jailbee.orchestration.state import (
    load_queue,
    purge_vanished_pending,
    save_queue,
    state_root,
)
from jailbee.orchestration.store import BrokerStore, store_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.config import Config

    from jailbee.incus import Incus

log = logging.getLogger(__name__)

ACTIVE_INTERVAL = 5.0
IDLE_INTERVAL = 60.0
SLEEP_SLICE = 1.0
LOCK_FILE = "broker.lock"
#: Exit status after another jailbee version was installed under the running
#: broker; the unit's `RestartForceExitStatus` starts it again on the new code.
UPGRADED_EXIT = 75


def tick(
    incus: Incus,
    *,
    root: Path,
    journal: Journal,
    now: datetime,
    memory: broker_state.BrokerMemory | None = None,
) -> bool:
    """Run one broker round; True while any running orchestrator or child exists."""
    memory = memory if memory is not None else broker_state.BrokerMemory()
    # Release finished workers before taking the snapshot that replaces their slots.
    prune_reservations(root)
    memory.publish_threads[:] = [worker for worker in memory.publish_threads if worker.is_alive()]
    raw = incus.list_containers()
    others = socket_lifecycle.other_projects(incus, memory) if memory.store is not None else None
    early_configs: dict[str, Config | None] | None = None
    if memory.listener is not None and memory.store is not None:
        try:
            early_configs = broker_state.repo_configs(memory)
        except Exception:
            log.exception("broker: repo configs failed; plain container sockets left as they are")
    managed: Callable[[str], bool] | None = None
    if early_configs is not None:
        known = early_configs

        def managed(name: str) -> bool:
            return broker_state.is_managed(name, known)

    socket_lifecycle.check_socket_devices(incus, raw, memory, root, others, managed)
    if early_configs is not None:
        try:
            socket_lifecycle.serve_plain(incus, raw, memory, root, early_configs)
        except Exception:
            log.exception("broker: serving plain container sockets failed")
    retention.purge_messages(memory, raw, now, None if others is None else raw + others)
    candidates = [
        e
        for e in raw
        if isinstance(e.get("name"), str)
        and isinstance(e.get("config"), dict)
        and broker_state.labelled(e["config"])
    ]
    active = any(e.get("status") == "Running" for e in candidates)
    present = {e.get("name") for e in raw}
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
        identities={
            str(e["name"]): str(e["config"]["volatile.uuid"])
            for e in raw
            if isinstance(e.get("config"), dict) and e["config"].get("volatile.uuid")
        },
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
        and not (isinstance(e.get("config"), dict) and broker_state.labelled(e["config"]))
        and has_pending(root, e["name"])
    ]
    # Before the early return: a later namesake must not inherit stale entries.
    broker_replies.forget_vanished_watches(root, present)
    if (
        not candidates
        and not queues
        and not parked
        and not publishes
        and not (former_publishers & present)
    ):
        memory.contexts.clear()
        memory.children.clear()
        query.publish_queries(memory)
        return False

    try:
        gcfg = load_global_config(default_global_config_path())[0]
    except Exception:
        log.exception("broker: global config failed to load; skipping this round")
        return True
    configs = early_configs if early_configs is not None else broker_state.repo_configs(memory)
    nodes = broker_state.nodes(raw, configs)

    def context(node: Node) -> ActionContext | None:
        cfg = broker_state.owner_config(node.full, node.config, configs)
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
    stale_failed: set[str] = set()
    for node in nodes.values():
        if node.running and (
            node.config.get(labels.STOPPED_HEAD)
            or node.config.get(labels.STOPPED_DIRTY)
            or node.config.get(labels.STOPPED_OUTBOX)
        ):
            if not _clear_stale_stop_labels(incus, node, memory):
                stale_failed.add(f"stale-stop:{node.full}")
    memory.settle_failures("stale-stop:", stale_failed)

    orchestrators: list[ActionContext] = []
    # A name can be reused by a different instance; never carry its child view over.
    old_identities = {ctx.sender.full: key for key, ctx in memory.contexts.items()}
    for node in nodes.values():
        if node.full in old_identities and old_identities[node.full] != node.key:
            memory.children.pop(node.full, None)
    for key in [k for k in memory.contexts if k not in {n.key for n in nodes.values()}]:
        del memory.contexts[key]
    serve_failed: set[str] = set()
    flush_failed: set[str] = set()
    for node in nodes.values():
        memory.contexts.pop(node.key, None)
        if not broker_state.labelled(node.config):
            try:
                if node.full in former_publishers:
                    former_ctx = context(node)
                    if former_ctx is not None:
                        if node.running:
                            manifests.serve(former_ctx, journal, memory)
                        else:
                            publishing.settle(
                                former_ctx, partial(broker_replies.reply, former_ctx, memory=memory)
                            )
                            _flush_unlabelled(node, context, root, incus, memory, flush_failed)
                    continue
                if node.full in publishes:
                    publish_ctx = context(node)
                    if publish_ctx is not None:
                        publishing.settle(
                            publish_ctx, partial(broker_replies.reply, publish_ctx, memory=memory)
                        )
                if has_pending(root, node.full):
                    _flush_unlabelled(node, context, root, incus, memory, flush_failed)
            except Exception as exc:
                serve_failed.add(f"serve:{node.full}")
                memory.report_failure(
                    f"serve:{node.full}",
                    exc,
                    "broker: serving former publisher %s failed",
                    node.full,
                )
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
                # Socket access ends, but durable messages remain deliverable.
                socket_lifecycle.teardown_socket(
                    incus, memory, node.full, node.config.get("volatile.uuid"), root
                )
                if has_pending(root, node.full):
                    flush_pending(
                        incus,
                        root,
                        node.full,
                        uid=ctx.cfg.container_user.uid,
                        finish=finishing.flush_callback(ctx) if ctx.sender.running else None,
                        recipient_uuid=node.config.get("volatile.uuid"),
                    )
                continue
            memory.contexts[node.key] = ctx
            if ctx.role() == "orchestrator":
                orchestrators.append(ctx)
            if node.running:
                manifests.serve(ctx, journal, memory)
            else:
                flush_pending(
                    incus,
                    root,
                    node.full,
                    uid=ctx.cfg.container_user.uid,
                    finish=finishing.flush_callback(ctx) if ctx.sender.running else None,
                    recipient_uuid=node.config.get("volatile.uuid"),
                )
        except Exception as exc:
            serve_failed.add(f"serve:{node.full}")
            memory.report_failure(f"serve:{node.full}", exc, "broker: serving %s failed", node.full)
    memory.settle_failures("serve:", serve_failed)
    memory.settle_failures("flush:", flush_failed)

    # A child view belongs only to a currently valid orchestrator context.
    owners = {ctx.sender.full for ctx in memory.contexts.values() if ctx.role() == "orchestrator"}
    for full in set(memory.children) - owners:
        del memory.children[full]

    # Publish role and grant changes before any later inventory refresh can fail.
    query.publish_queries(memory)

    # Handlers changed labels and may have stopped children after our initial
    # snapshot. Refresh once before cleanup; share deletions with queue drain.
    if orchestrators:
        nodes = broker_state.nodes(incus.list_containers(), configs)
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
            manifests.settle_queue(ctx, memory)
        except Exception:
            log.exception("broker: queue of %s failed", ctx.sender.full)
        if ctx.sender.running:
            try:
                manifests.remember_children(
                    ctx, memory, inbox_state.sync_children(ctx, memory.status)
                )
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
                manifests.settle_queue(ctx, memory)
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
        nodes = broker_state.nodes(incus.list_containers(), configs)
        for ctx in orchestrators:
            ctx.nodes = nodes
            ctx.sender = nodes.get(ctx.sender.full, ctx.sender)
    broker_replies.settle_all_watches(root, orchestrators, present, memory)
    query.publish_queries(memory)
    return active


def _clear_stale_stop_labels(incus: Incus, node: Node, memory: broker_state.BrokerMemory) -> bool:
    """A running container's recorded stop-time HEAD and dirty flag are stale.

    Returns False when clearing failed (logged once per episode, retried next round).
    """
    try:
        for key in (labels.STOPPED_HEAD, labels.STOPPED_DIRTY, labels.STOPPED_OUTBOX):
            if node.config.get(key):
                incus.config_unset_checked(node.full, key)
    except Exception as exc:
        memory.report_failure(
            f"stale-stop:{node.full}",
            exc,
            "broker: clearing the stale stop labels of %s failed",
            node.full,
        )
        return False
    return True


def _flush_unlabelled(
    node: Node,
    context: Callable[[Node], ActionContext | None],
    root: Path,
    incus: Incus,
    memory: broker_state.BrokerMemory,
    failed: set[str],
) -> None:
    """Flush host-spooled messages even for stopped, unlabelled recipients."""
    try:
        ctx = context(node)
        if ctx is not None:
            flush_pending(
                incus,
                root,
                node.full,
                uid=ctx.cfg.container_user.uid,
                finish=finishing.flush_callback(ctx) if ctx.sender.running else None,
                recipient_uuid=node.config.get("volatile.uuid"),
            )
    except Exception as exc:
        failed.add(f"flush:{node.full}")
        memory.report_failure(f"flush:{node.full}", exc, "broker: flushing %s failed", node.full)


def _pause(seconds: float, stop: broker_state.StopFlag) -> None:
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


def _stage_container_app(memory: broker_state.BrokerMemory) -> None:
    from jailbee import container_app
    from jailbee.agent_skills import sync_agent_skills

    try:
        container_app.stage()
    except Exception:
        log.exception("broker: jailbee app staging failed; continuing")
        return
    try:
        configs = broker_state.repo_configs(memory)
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
    stop = broker_state.StopFlag()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    memory = broker_state.BrokerMemory()
    memory.store = BrokerStore(store_path(root))
    memory.queries = query.Queries(memory.store)
    memory.listener = Listener(memory.requests, queries=memory.queries)
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
