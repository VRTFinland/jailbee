"""Broker replies and child watch settlement."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from jailbee.orchestration import broker_state, finishing, labels, watch
from jailbee.orchestration.actions import Node
from jailbee.orchestration.outbox import deliver, has_container_jailbee
from jailbee.orchestration.protocol import (
    InboxMessage,
    InboxStatus,
    RestartManifest,
    SpawnManifest,
    new_host_id,
)

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.orchestration.actions import ActionContext, Outcome
    from jailbee.orchestration.protocol import Manifest
    from jailbee.orchestration.state import QueuedRequest
    from jailbee.orchestration.watch import JobSnapshot

log = logging.getLogger(__name__)


def status(ref: str, outcome: Outcome) -> InboxStatus:
    return InboxStatus(
        version=1,
        id=new_host_id(),
        type="status",
        ref=ref,
        state=outcome.state,
        detail=outcome.detail,
    )


def reply(
    ctx: ActionContext, ref: str, outcome: Outcome, memory: broker_state.BrokerMemory
) -> None:
    deliver(
        ctx.incus,
        ctx.root,
        ctx.sender.full,
        status(ref, outcome),
        running=ctx.sender.running,
        uid=ctx.cfg.container_user.uid,
        now=ctx.now,
        seq=next(memory.seq),
        recipient_uuid=ctx.sender.config.get("volatile.uuid"),
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


def watch_manifest(
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


def watch_drained(ctx: ActionContext, req: QueuedRequest, outcome: Outcome) -> None:
    try:
        if req.kind == "restart":
            _track_restart(ctx, req.manifest_id, req.child_full, outcome)
        elif outcome.state == "started":
            _track(ctx, req.manifest_id, req.child_full, "spawn")
    except Exception:
        log.exception("broker: %s: could not watch %s", ctx.sender.full, req.manifest_id)


def _settle_watches(
    ctx: ActionContext, memory: broker_state.BrokerMemory, jobs: dict[str, JobSnapshot] | None
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
                fresh_nodes = broker_state.nodes(
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
                reply(ctx, entry.ref, outcome, memory)
            entries.remove(entry)
            watch.save_watch(ctx.root, watches)


def _settle_agent_watch(
    ctx: ActionContext,
    memory: broker_state.BrokerMemory,
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
            reply(ctx, entry.ref, boot, memory)
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
                recipient_uuid=ctx.sender.config.get("volatile.uuid"),
            )
        else:
            reply(ctx, entry.ref, outcome, memory)
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
                recipient_uuid=ctx.sender.config.get("volatile.uuid"),
            )
    index = entries.index(entry)
    if updated is None:
        entries.pop(index)
    else:
        entries[index] = updated
    watch.save_watch(ctx.root, watches)


def settle_all_watches(
    root: Path,
    orchestrators: list[ActionContext],
    present: set[object],
    memory: broker_state.BrokerMemory,
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


def forget_vanished_watches(root: Path, present: set[object]) -> None:
    """Drop the watch entries of orchestrators that no longer exist."""
    with watch.locked(root):
        watches = watch.load_watch(root)
        gone = [full for full in watches if full not in present]
        if gone:
            for full in gone:
                log.warning("broker: dropping the watches of vanished %s", full)
                del watches[full]
            watch.save_watch(root, watches)
