"""Broker handlers for messages between an orchestrator and its children."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from jailbee import lifecycle, sync
from jailbee.git import GitError
from jailbee.incus import IncusError
from jailbee.orchestration import labels
from jailbee.orchestration.actions import ActionContext, Node, Outcome, done, refused
from jailbee.orchestration.outbox import deliver
from jailbee.orchestration.protocol import InboxMessage, MessageManifest, new_host_id
from jailbee.sync import SyncError

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


_FULL_OID = re.compile(r"[0-9a-f]{40}")


def handle_done(ctx: ActionContext, manifest: MessageManifest) -> Outcome:
    """Check a child's `done` report, record its commit, then relay it.

    Nothing is recorded or relayed unless the work tree is clean, HEAD is on a
    branch and that branch was fetched to the host.
    """
    uid = ctx.cfg.container_user.uid
    full = ctx.sender.full
    try:
        repo_dir = lifecycle.container_repo_dir(ctx.cfg, ctx.incus, full)
        if sync._container_status_dirty(ctx.incus, full, repo_dir, uid=uid):
            return refused("uncommitted changes: commit everything before reporting done")
        branch = sync._container_current_branch(ctx.incus, full, repo_dir, uid=uid)
    except (SyncError, GitError, IncusError) as exc:
        return refused(str(exc))
    if branch is None:
        return refused("detached HEAD: check out your branch before reporting done")
    try:
        fetched = sync.fetch_from_container(
            ctx.cfg, ctx.incus, ctx.sender.short, branch=branch, tags="none"
        )
    except (SyncError, GitError) as exc:
        return refused(f"could not fetch your branch to the host: {exc}")
    oid = fetched.new_oid
    if not _FULL_OID.fullmatch(oid or ""):
        return refused(f"could not fetch your branch to the host: unexpected commit id `{oid}`")
    try:
        # STATE last: a crash in between never shows `done` without a commit.
        ctx.incus.config_set(full, labels.DONE_SHA, oid)
        ctx.incus.config_set(full, labels.STATE, labels.STATE_DONE)
    except IncusError as exc:
        return refused(f"could not record the done report: {exc}")
    target = _orchestrator_of(ctx)
    outcome = _relay(ctx, target, manifest)
    if outcome.state == "done":
        return done(f"done reported to {target.short} at {oid[:12]}")
    return Outcome(
        outcome.state,
        f"{target.short} is not running; done at {oid[:12]} is delivered when it starts",
    )


def _from_child(ctx: ActionContext, manifest: MessageManifest) -> Outcome:
    target = _orchestrator_of(ctx)
    if manifest.to is not None and manifest.to != target.short:
        return refused("a child can only message its orchestrator")
    if manifest.kind == "done":
        return handle_done(ctx, manifest)
    return _relay(ctx, target, manifest)


def handle_message(ctx: ActionContext, manifest: Manifest) -> Outcome:
    if not isinstance(manifest, MessageManifest):
        return refused("not a message manifest")
    if ctx.role() == "child":
        return _from_child(ctx, manifest)
    return _from_orchestrator(ctx, manifest)
