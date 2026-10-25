"""Broker handlers for messages between an orchestrator and its children."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee import lifecycle
from jailbee.orchestration import labels
from jailbee.orchestration.actions import ActionContext, Node, Outcome, done, refused
from jailbee.orchestration.outbox import deliver
from jailbee.orchestration.protocol import InboxMessage, MessageManifest, new_host_id

if TYPE_CHECKING:
    from jailbee.orchestration.protocol import Manifest


def _relay(ctx: ActionContext, target: Node, manifest: MessageManifest) -> Outcome:
    """Deliver `manifest` to `target`'s inbox, parking it when `target` is not running."""
    # Built from a dict: the wire name `from` is an alias, which mypy cannot see.
    msg = InboxMessage.model_validate(
        {
            "version": 1,
            "id": new_host_id(),
            "type": "message",
            "from": ctx.sender.short,
            "body": manifest.body,
            "kind": manifest.kind or "note",
            "in_reply_to": manifest.in_reply_to,
        }
    )
    result = deliver(
        ctx.incus,
        ctx.root,
        target.full,
        msg,
        running=target.running,
        uid=ctx.cfg.container_user.uid,
        now=ctx.now,
        seq=next(ctx.seq),
    )
    if result == "delivered":
        return done(f"delivered to {target.short}")
    return Outcome(
        "queued", f"{target.short} is not running; the message is delivered when it starts"
    )


def _orchestrator_of(ctx: ActionContext) -> Node:
    """The sender's orchestrator; a destroyed one is a stopped stand-in so the message parks."""
    full = ctx.sender.config[labels.SPAWNED_BY]
    listed = ctx.nodes.get(full)
    if listed is not None:
        return listed
    return Node(full, lifecycle.short_name(ctx.cfg, full), False, {})


def _from_orchestrator(ctx: ActionContext, manifest: MessageManifest) -> Outcome:
    if manifest.to is None:
        return refused("`to` is required for a message from an orchestrator")
    target = ctx.child(manifest.to)
    if isinstance(target, Outcome):
        return target
    return _relay(ctx, target, manifest)


def _from_child(ctx: ActionContext, manifest: MessageManifest) -> Outcome:
    if manifest.kind == "done":
        return refused("done is not supported yet")
    target = _orchestrator_of(ctx)
    if manifest.to is not None and manifest.to != target.short:
        return refused("a child can only message its orchestrator")
    return _relay(ctx, target, manifest)


def handle_message(ctx: ActionContext, manifest: Manifest) -> Outcome:
    if not isinstance(manifest, MessageManifest):
        return refused("not a message manifest")
    if ctx.role() == "child":
        return _from_child(ctx, manifest)
    return _from_orchestrator(ctx, manifest)
