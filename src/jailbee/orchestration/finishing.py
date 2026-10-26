"""Import a finished child's work before reporting it to its orchestrator."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.incus import IncusError
from jailbee.orchestration import labels
from jailbee.orchestration.protocol import InboxMessage, PendingFinish, finish_file_name, new_host_id
from jailbee.orchestration.state import atomic_write_text, pending_dir

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.orchestration.actions import ActionContext, Node


def complete(
    ctx: ActionContext, child: Node | None, orchestrator: Node, item: PendingFinish
) -> tuple[InboxMessage, str]:
    """Import and mark for later cleanup; never write to an inbox here."""
    from jailbee.orchestration import importing

    error: str | None = None
    imported: importing.Imported | str
    policy = "keep" if child is None else labels.on_done_of(child.config)
    ref: str | None = None
    oid: str | None = None
    if child is None:
        error = (f"child {item.child_short} no longer exists; its branch is on the host at "
                 f"refs/jailbee/{item.child_short}/{item.branch} ({item.done_sha[:12]})")
    elif child.config.get(labels.LAST_IMPORT) == item.done_sha:
        ref = f"refs/jailbee/from/{item.child_short}/{item.branch}"
        oid = item.done_sha
    elif not child.running:
        error = f"{item.child_short} is stopped; restart it and send import"
    else:
        imported = importing.import_child(ctx, child=child, orchestrator=orchestrator)
        if isinstance(imported, str):
            error = imported
        else:
            ref, oid = imported.ref, imported.oid
    if error is not None:
        policy = "keep"
    elif policy == "destroy" and child is not None and oid is not None:
        try:
            ctx.incus.config_set(child.full, labels.PENDING_DESTROY, oid)
        except IncusError as exc:
            # The import succeeded, but cleanup must not hide the done report.
            error = f"imported but could not schedule destruction: {exc}"
            policy = "keep"
    msg = InboxMessage.model_validate({
        "version": 1, "id": new_host_id(), "type": "message", "from": item.child_short,
        "body": item.body, "kind": "done", "in_reply_to": item.in_reply_to,
        "imported_ref": ref, "imported_sha": oid, "import_error": error, "on_done": policy,
    })
    if error is not None:
        summary = (f"done reported to {orchestrator.short} at {item.done_sha[:12]}, but the "
                   f"import failed: {error}; this container is kept")
    else:
        ending = "will be destroyed" if policy == "destroy" else "is kept"
        summary = f"imported into {orchestrator.short} at {str(oid)[:12]}; this container {ending}"
    return msg, summary


def flush_callback(ctx: ActionContext) -> Callable[[PendingFinish], InboxMessage]:
    """Complete a queued report with the recipient as the orchestrator."""
    def finish(item: PendingFinish) -> InboxMessage:
        msg, _ = complete(ctx, ctx.nodes.get(item.child_full), ctx.sender, item)
        return msg

    return finish


def park(ctx: ActionContext, orchestrator: Node, item: PendingFinish) -> None:
    """Keep a finish item in the same causal queue as ordinary inbox messages."""
    atomic_write_text(
        pending_dir(ctx.root, orchestrator.full) / finish_file_name(item, ctx.now, seq=next(ctx.seq)),
        item.model_dump_json() + "\n",
    )
