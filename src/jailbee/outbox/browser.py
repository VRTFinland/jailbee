"""Injectable, iterative terminal navigation over immutable outbox views."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from jailbee.incus import IncusError
from jailbee.outbox.delete import DeletePlan, DeleteSelection, plan_delete
from jailbee.outbox.inspect import safe_text
from jailbee.outbox.models import ContainerView, OutboxError, OutboxExecutionError, ProposalId
from jailbee.remote_ssh.repo_scope import RepoScopeError


@dataclass(frozen=True)
class BrowserActions:
    load: Callable[[str | None], tuple[ContainerView, ...]]
    delete: Callable[[str, DeletePlan], tuple[str, ...]] | None
    publish: Callable[[str, ProposalId, str], int] | None
    confirm: Callable[[str], bool]
    choose: Callable[[str, Sequence[tuple[str, str]]], str | None]
    show: Callable[[str], None]


def run_browser(actions: BrowserActions, initial_container: str | None = None) -> int:
    """Navigate without recursion; only unchanged revisions retain child indices."""
    name: str | None = None
    proposal_id: ProposalId | None = None
    revision: str | None = None
    action_index: int | None = None
    comment_index: int | None = None
    views: tuple[ContainerView, ...] = ()
    reload = True
    errors = (OutboxError, OutboxExecutionError, IncusError, RepoScopeError, OSError)

    def show(text: str) -> None:
        actions.show(safe_text(text))

    while True:
        if reload:
            try:
                views = actions.load(initial_container)
                if name is None and initial_container is not None and len(views) == 1:
                    name = views[0].name
            except errors as exc:
                show(str(exc))
                views = ()
            reload = False
        container = next((c for c in views if c.name == name), None)
        if name is not None and container is None:
            name = None
        proposal = (
            next((p for p in container.proposals if p.id == proposal_id), None)
            if container
            else None
        )
        if proposal_id is not None and proposal is None:
            proposal_id = None
            revision = None
            action_index = comment_index = None
        if proposal is not None and revision != proposal.revision:
            action_index = comment_index = None
            revision = proposal.revision
        action = (
            next((a for a in proposal.actions if a.index == action_index), None)
            if proposal
            else None
        )
        if action_index is not None and action is None:
            action_index = comment_index = None
        comment = (
            next((c for c in action.comments if c.index == comment_index), None) if action else None
        )
        if comment_index is not None and comment is None:
            comment_index = None

        options: list[tuple[str, str]] = [("exit", "Exit"), ("refresh", "Refresh")]
        if name is None:
            options.extend(
                (
                    f"container:{c.name}",
                    f"{c.name} ({'available' if c.available else 'unavailable'})",
                )
                for c in views
            )
            if not views:
                show("No containers available.")
        else:
            options.append(("back", "Back"))
            assert container is not None
            show(container.name)
            if not container.available:
                show(container.error or "Container unavailable")
            elif proposal is None:
                options.extend(
                    (f"proposal:{p.id}", f"{p.id} [{p.state}]") for p in container.proposals
                )
                if not container.proposals:
                    show("No proposals")
                for snapshot in container.stores:
                    for warning in (*snapshot.warnings, *snapshot.rejected):
                        show(warning)
            else:
                show(f"{proposal.id} [{proposal.state}]\nRevision: {proposal.revision}")
                if proposal.error:
                    show(proposal.error)
                if proposal.edit_block:
                    show(proposal.edit_block)
                show("Raw manifest:\n" + proposal.raw_text)
                for item in proposal.actions:
                    show(
                        f"Action {item.index}: {item.kind} {item.repo} "
                        f"{item.target} [{item.state}]\n{item.text}"
                    )
                    if item.receipt:
                        show("Receipt: " + item.receipt)
                    for child in item.comments:
                        show(f"Comment {child.index}: {child.label}\n{child.text}")
                if action is None:
                    options.extend(
                        (f"action:{a.index}", f"Action {a.index}: {a.kind} [{a.state}]")
                        for a in proposal.actions
                    )
                elif comment is None:
                    options.extend(
                        (f"comment:{c.index}", f"Comment {c.index}: {c.label}")
                        for c in action.comments
                    )
                if actions.delete is not None and not proposal.edit_block:
                    options.append(
                        (
                            "delete",
                            "Delete selected comment"
                            if comment
                            else "Delete selected action"
                            if action
                            else "Delete manifest",
                        )
                    )
                if (
                    actions.publish is not None
                    and not proposal.error
                    and proposal.state in ("pending", "partial", "awaiting-pr")
                ):
                    options.append(("publish", "Publish all pending actions in this manifest"))
                if (
                    actions.delete is not None
                    and proposal.id.kind == "issue"
                    and proposal.edit_block
                    and not proposal.error
                    and proposal.state in ("partial", "applied")
                    and not any(a.state == "uncertain" for a in proposal.actions)
                    and action is None
                ):
                    options.append(
                        ("archive-delete", "Archive settled journal and delete whole manifest")
                    )
        choice = actions.choose("Outbox", tuple((key, safe_text(label)) for key, label in options))
        if choice is None or choice == "exit":
            return 0
        if choice not in dict(options):
            show("Invalid selection; choose a displayed option.")
            continue
        if choice == "refresh":
            reload = True
        elif choice == "back":
            if comment_index is not None:
                comment_index = None
            elif action_index is not None:
                action_index = None
            elif proposal_id is not None:
                proposal_id = None
            else:
                name = None
        elif choice.startswith("container:"):
            name = choice.removeprefix("container:")
        elif choice.startswith("proposal:"):
            proposal_id = ProposalId.parse(choice.removeprefix("proposal:"))
        elif choice.startswith("action:"):
            action_index = int(choice.removeprefix("action:"))
        elif choice.startswith("comment:"):
            comment_index = int(choice.removeprefix("comment:"))
        elif container is not None and proposal is not None:
            try:
                if choice in ("delete", "archive-delete") and actions.delete is not None:
                    selection = DeleteSelection(
                        action_index, comment_index, archive_journal=choice == "archive-delete"
                    )
                    try:
                        plan = plan_delete(container, proposal.id, selection)
                    except OutboxError:
                        # Offer a cascade only when the pure planner validates it.
                        plan = plan_delete(
                            container,
                            proposal.id,
                            DeleteSelection(action_index, comment_index, with_dependents=True),
                        )
                        if not actions.confirm(
                            safe_text("Include dependent actions?\n" + "\n".join(plan.summary))
                        ):
                            continue
                    if not actions.confirm(
                        safe_text("Delete this exact scope?\n" + "\n".join(plan.summary))
                    ):
                        continue
                    for line in actions.delete(container.name, plan):
                        show(line)
                elif choice == "publish" and actions.publish is not None:
                    # publish shows the whole manifest's plan and asks once under it.
                    status = actions.publish(container.name, proposal.id, proposal.revision)
                    if status:
                        show(f"Publication returned status {status}; refresh required.")
                reload = True
            except errors as exc:
                show(str(exc))
                reload = True
