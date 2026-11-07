"""Manifest handling, child snapshots, and queued request settlement."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING

from jailbee.orchestration import (
    broker_replies,
    broker_state,
    finishing,
    inbox_state,
    labels,
    publishing,
    query,
    socket_lifecycle,
    watch,
)
from jailbee.orchestration.actions import (
    GRANT_REVOKED,
    NOT_ALLOWED,
    ActionContext,
    Outcome,
    dispatch,
    drain_queue,
    drop_queue,
    refused,
)
from jailbee.orchestration.outbox import flush_pending
from jailbee.orchestration.protocol import (
    DestroyManifest,
    MessageManifest,
    PublishManifest,
    RestartManifest,
    SpawnManifest,
    StopManifest,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.orchestration.journal import Journal
    from jailbee.orchestration.protocol import Manifest

log = logging.getLogger(__name__)


def handle_manifest(
    ctx: ActionContext,
    memory: broker_state.BrokerMemory,
    *,
    ref: str,
    manifest: Manifest | None,
    outcome: Outcome | None,
    record: Callable[[str, Outcome | None], None],
    reply: Callable[[str, Outcome], None],
    before_publish: Callable[[], None],
    label: str | None = None,
) -> Outcome | None:
    """Dispatch a parsed socket request, recording before replying or publishing.

    `outcome` is a preset refusal (parse failure); with a `manifest` and no
    preset the manifest is dispatched. `record` journals, `reply` delivers the
    status; `before_publish` is the caller's pre-activation hook.
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
        broker_replies.watch_manifest(ctx, manifest, ref, outcome)
        _refresh_children_snapshot(ctx, manifest, outcome, memory)
        if (
            isinstance(manifest, MessageManifest)
            and outcome.state in ("done", "queued")
            and ctx.role() == "child"
        ):
            parent = ctx.nodes.get(ctx.sender.config[labels.SPAWNED_BY])
            if parent is not None:
                parent_ctx = memory.contexts.get(parent.key)
                if parent_ctx is not None:
                    query.publish_context(memory, parent_ctx)
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
    ctx: ActionContext,
    manifest: Manifest | None,
    outcome: Outcome,
    memory: broker_state.BrokerMemory,
) -> None:
    """Refresh the children view right after the broker changed a child's state.

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
        ctx.nodes = broker_state.nodes(
            ctx.incus.list_containers(), broker_state.repo_configs(memory)
        )
        remember_children(ctx, memory, inbox_state.sync_children(ctx, memory.status))
        query.publish_context(memory, ctx)
    except Exception:
        log.exception("broker: child inbox state of %s failed", ctx.sender.full)


def remember_children(
    ctx: ActionContext, memory: broker_state.BrokerMemory, children: object
) -> None:
    if isinstance(children, list):
        memory.children[ctx.sender.full] = broker_state.ChildrenView(time.time(), children)


def serve(ctx: ActionContext, journal: Journal, memory: broker_state.BrokerMemory) -> None:
    """One running orchestrator or child: socket setup and durable deliveries."""
    socket_lifecycle.ensure_env(ctx, memory)
    uid = ctx.cfg.container_user.uid
    # Parked messages first, so a new status never overtakes them.
    flush_pending(
        ctx.incus,
        ctx.root,
        ctx.sender.full,
        uid=uid,
        finish=finishing.flush_callback(ctx) if ctx.sender.running else None,
        recipient_uuid=ctx.sender.config.get("volatile.uuid"),
    )
    path = finishing.reply_path(ctx, ctx.sender)
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get("ref"):
            broker_replies.reply(ctx, saved["ref"], Outcome(saved["state"], saved["detail"]), memory)
    publishing.settle(ctx, lambda ref, out: broker_replies.reply(ctx, ref, out, memory))


def settle_queue(ctx: ActionContext, memory: broker_state.BrokerMemory) -> None:
    """Start what the queue can start, or drop it when the grant is gone; report each."""
    if ctx.grant() is None:
        # A grant label still there means the repo cap forbids orchestration.
        reason = GRANT_REVOKED if ctx.raw_grant() is None else NOT_ALLOWED
        for req in drop_queue(ctx, reason):
            broker_replies.reply(ctx, req.manifest_id, refused(reason), memory)
        return
    for req, outcome in drain_queue(ctx):
        broker_replies.watch_drained(ctx, req, outcome)
        broker_replies.reply(ctx, req.manifest_id, outcome, memory)
