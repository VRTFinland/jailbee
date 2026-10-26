"""Broker handlers for messages between an orchestrator and its children."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from jailbee import lifecycle, sync
from jailbee.git import GitError
from jailbee.incus import IncusError
from jailbee.orchestration import finishing, labels, outbox
from jailbee.orchestration.actions import (
    CHILD_EXEC_TIMEOUT_S,
    CHILD_TRANSPORT_TIMEOUT_S,
    ActionContext,
    Node,
    Outcome,
    done,
    refused,
)
from jailbee.orchestration.outbox import deliver
from jailbee.orchestration.protocol import InboxMessage, MessageManifest, PendingFinish, new_host_id
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
    branch and that branch was fetched to the host. Every exec into the child
    and the fetch from it are bounded (`CHILD_EXEC_TIMEOUT_S`,
    `CHILD_TRANSPORT_TIMEOUT_S`); an expiry is a refusal like any failure.
    """
    uid = ctx.cfg.container_user.uid
    full = ctx.sender.full
    bounded = ctx.incus.with_exec_timeout(CHILD_EXEC_TIMEOUT_S)
    try:
        repo_dir = lifecycle.container_repo_dir(ctx.cfg, bounded, full)
        if sync._container_status_dirty(bounded, full, repo_dir, uid=uid):
            return refused("uncommitted changes: commit everything before reporting done")
        # None also covers a failed exec, which cannot be told from a detached HEAD.
        branch = sync._container_current_branch(bounded, full, repo_dir, uid=uid)
    except (SyncError, GitError, IncusError) as exc:
        return refused(str(exc))
    if branch is None:
        return refused("detached HEAD: check out your branch before reporting done")
    try:
        fetched = sync.fetch_from_container(
            ctx.cfg,
            bounded,
            ctx.sender.short,
            branch=branch,
            tags="none",
            timeout=CHILD_TRANSPORT_TIMEOUT_S,
        )
    except (SyncError, GitError, IncusError) as exc:
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
    orchestrator_full = ctx.sender.config[labels.SPAWNED_BY]
    target = ctx.nodes.get(orchestrator_full)
    if target is None:
        short = lifecycle.short_name(ctx.cfg, orchestrator_full)
        return refused(f"your orchestrator {short} no longer exists; your work is on the host "
                       f"at {oid[:12]} and this container is kept")
    item = PendingFinish(
        version=1, type="finish", id=manifest.id, child_full=full,
        child_short=ctx.sender.short, branch=branch, done_sha=oid, body=manifest.body,
        in_reply_to=manifest.in_reply_to, uid=uid,
    )
    if target.running and not outbox.has_pending(ctx.root, target.full):
        # Persist intent before a destroy marker can exist: a broker crash
        # before the inbox write must leave a retryable delivery gate.
        pending = finishing.park(ctx, target, item)
        msg, summary = finishing.complete(ctx, ctx.sender, target, item)
        seq = next(ctx.seq)
        try:
            outbox.write_inbox(ctx.incus, target.full, msg, uid=uid, now=ctx.now, seq=seq)
        except IncusError:
            outbox.deliver(ctx.incus, ctx.root, target.full, msg, running=False,
                           uid=uid, now=ctx.now, seq=seq)
        pending.unlink(missing_ok=True)
        return done(summary)
    finishing.park(ctx, target, item)
    return Outcome("queued", f"{target.short} is not running or busy; your work is on the host "
                   f"at {oid[:12]} and is imported when it is delivered")


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
