"""Import a finished child's work before reporting it to its orchestrator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.incus import IncusError
from jailbee.orchestration import labels
from jailbee.orchestration.protocol import (
    InboxMessage,
    PendingFinish,
    finish_file_name,
    new_host_id,
)
from jailbee.orchestration.state import atomic_write_text, pending_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator
    from datetime import datetime
    from pathlib import Path

    from jailbee.incus import Incus
    from jailbee.orchestration.actions import ActionContext, Node


def reply_path(ctx: ActionContext, child: Node) -> Path:
    """Durable gate from accepting done until its status reaches the child."""
    return ctx.root / "done-replies" / f"{child.full}.json"


def _destroy_allowed(ctx: ActionContext, orchestrator: Node) -> bool:
    from dataclasses import replace

    grant = replace(ctx, sender=orchestrator).grant()
    return grant is not None and "destroy" in grant.operations


def complete(
    ctx: ActionContext, child: Node | None, orchestrator: Node, item: PendingFinish
) -> tuple[InboxMessage, str]:
    """Import and mark for later cleanup; never write to an inbox here."""
    from jailbee import lifecycle, sync
    from jailbee.git import GitError
    from jailbee.orchestration import importing
    from jailbee.orchestration.actions import CHILD_EXEC_TIMEOUT_S, _child_head

    error: str | None = None
    kept_reason: str | None = None
    imported: importing.Imported | str
    policy = "keep" if child is None else labels.on_done_of(child.config)
    ref: str | None = None
    oid: str | None = None
    if child is None:
        error = (
            f"child {item.child_short} no longer exists; its branch is on the host at "
            f"refs/jailbee/{item.child_short}/{item.branch} ({item.done_sha[:12]})"
        )
    elif child.config.get(labels.LAST_IMPORT) == item.done_sha and importing.imported_ref_matches(
        ctx, orchestrator, f"refs/jailbee/from/{item.child_short}/{item.branch}", item.done_sha
    ):
        ref = f"refs/jailbee/from/{item.child_short}/{item.branch}"
        oid = item.done_sha
    elif not child.running:
        error = f"{item.child_short} is stopped; restart it and send import"
    else:
        bounded = ctx.incus.with_exec_timeout(CHILD_EXEC_TIMEOUT_S)
        try:
            repo_dir = lifecycle.container_repo_dir(ctx.cfg, bounded, child.full)
            branch = sync._container_current_branch(
                bounded, child.full, repo_dir, uid=ctx.cfg.container_user.uid
            )
            if branch != item.branch or _child_head(ctx, child) != item.done_sha:
                error = "child branch or HEAD changed since done; send a new done report"
        except (sync.SyncError, GitError, IncusError) as exc:
            error = str(exc)
        if error is None:
            imported = importing.import_child(ctx, child=child, orchestrator=orchestrator)
            if isinstance(imported, str):
                error = imported
            elif imported.oid != item.done_sha or imported.ref != (
                f"refs/jailbee/from/{item.child_short}/{item.branch}"
            ):
                error = "child branch or HEAD changed during import; send a new done report"
            else:
                ref, oid = imported.ref, imported.oid
    if error is not None:
        policy = "keep"
    elif policy == "destroy" and not _destroy_allowed(ctx, orchestrator):
        kept_reason = "orchestrator no longer has destroy permission"
        policy = "keep"
    elif policy == "destroy" and child is not None and oid is not None:
        try:
            ctx.incus.config_set(child.full, labels.PENDING_DESTROY, oid)
        except IncusError as exc:
            # The import succeeded, but cleanup must not hide the done report.
            kept_reason = f"could not schedule destruction: {exc}"
            policy = "keep"
    msg = InboxMessage.model_validate(
        {
            "version": 1,
            "id": new_host_id(),
            "type": "message",
            "from": item.child_short,
            "body": item.body,
            "kind": "done",
            "in_reply_to": item.in_reply_to,
            "imported_ref": ref,
            "imported_sha": oid,
            "import_error": error,
            "on_done": policy,
            "on_done_reason": kept_reason,
        }
    )
    if error is not None:
        summary = (
            f"done reported to {orchestrator.short} at {item.done_sha[:12]}, but the "
            f"import failed: {error}; this container is kept"
        )
    else:
        ending = "will be destroyed" if policy == "destroy" else "is kept"
        if kept_reason is not None:
            ending += f" ({kept_reason})"
        summary = f"imported into {orchestrator.short} at {str(oid)[:12]}; this container {ending}"
    return msg, summary


def flush_callback(ctx: ActionContext) -> Callable[[PendingFinish], InboxMessage]:
    """Complete a queued report with the recipient as the orchestrator."""

    def finish(item: PendingFinish) -> InboxMessage:
        msg, _ = complete(ctx, ctx.nodes.get(item.child_full), ctx.sender, item)
        return msg

    return finish


def park(ctx: ActionContext, orchestrator: Node, item: PendingFinish) -> Path:
    """Keep a finish item in the same causal queue as ordinary inbox messages."""
    path = pending_dir(ctx.root, orchestrator.full) / finish_file_name(
        item, ctx.now, seq=next(ctx.seq)
    )
    atomic_write_text(path, item.model_dump_json() + "\n")
    return path


def settle_destroys(ctx: ActionContext) -> None:
    """Destroy only after delivery, reply, journal and outbox cleanup finished."""
    from jailbee import lifecycle
    from jailbee.orchestration import outbox
    from jailbee.orchestration.actions import destroy_guard
    from jailbee.orchestration.state import purge_pending

    if not ctx.sender.running or outbox.has_pending(ctx.root, ctx.sender.full):
        return
    for child in ctx.children():
        marker = child.config.get(labels.PENDING_DESTROY)
        if not marker:
            continue
        if reply_path(ctx, child).exists() or outbox.has_pending(ctx.root, child.full):
            continue
        reason: str | None
        if not _destroy_allowed(ctx, ctx.sender):
            reason = "orchestrator no longer has destroy permission"
        elif child.config.get(labels.LAST_IMPORT) != marker:
            reason = "the imported commit changed"
        else:
            reason = destroy_guard(ctx, child)
        if reason is not None:
            body = f"child {child.short} was kept instead of destroyed: {reason}"
        else:
            try:
                lifecycle.destroy_container(ctx.cfg, ctx.incus, child.full, force=True)
            except Exception as exc:
                body = f"child {child.short} could not be destroyed: {exc}"
            else:
                purge_pending(ctx.root, child.full)
                if isinstance(ctx.nodes, dict):
                    ctx.nodes.pop(child.full, None)
                continue
        ctx.incus.config_unset_checked(child.full, labels.PENDING_DESTROY)
        msg = InboxMessage.model_validate(
            {
                "version": 1,
                "id": new_host_id(),
                "type": "message",
                "from": child.short,
                "kind": "note",
                "body": body,
            }
        )
        outbox.deliver(
            ctx.incus,
            ctx.root,
            ctx.sender.full,
            msg,
            running=True,
            uid=ctx.cfg.container_user.uid,
            now=ctx.now,
            seq=next(ctx.seq),
        )


def notify_orphaned_finishes(
    incus: Incus,
    root: Path,
    present: Collection[str],
    running: Collection[str],
    *,
    now: datetime,
    seq: Iterator[int],
) -> None:
    """Tell surviving workers about undeliverable reports before purging them."""
    from pydantic import ValidationError

    from jailbee.orchestration import outbox

    base = root / "pending"
    if not base.is_dir():
        return
    for directory in base.iterdir():
        if not directory.is_dir() or directory.name in present:
            continue
        for path in sorted(directory.glob("*.finish.json")):
            try:
                item = PendingFinish.model_validate_json(path.read_text())
            except (OSError, ValidationError):
                continue
            if item.child_full not in present:
                continue
            msg = InboxMessage.model_validate(
                {
                    "version": 1,
                    "id": new_host_id(),
                    "type": "message",
                    "kind": "note",
                    "from": directory.name,
                    "body": (
                        f"your orchestrator {directory.name} was destroyed; "
                        "your work was not imported and this container is kept"
                    ),
                }
            )
            incus.config_unset_checked(item.child_full, labels.PENDING_DESTROY)
            outbox.deliver(
                incus,
                root,
                item.child_full,
                msg,
                running=item.child_full in running,
                uid=item.uid,
                now=now,
                seq=next(seq),
            )
