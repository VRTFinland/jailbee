"""Pure local views; publication authorization remains with the domain preflight."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Literal, Union, assert_never

from jailbee import issue_manifest as issues
from jailbee import pr_outbox as prs
from jailbee.outbox import delete
from jailbee.outbox.models import (
    ActionView,
    CommentView,
    ContainerView,
    OutboxError,
    ProposalId,
    ProposalView,
    State,
    StoreSnapshot,
)
from jailbee.outbox_io import (
    ContainerIdentity,
    IssueJournal,
    JournalError,
    proposal_digest,
)

if TYPE_CHECKING:
    from jailbee.outbox.publication_db import Journal

# A store-held publication journal of either kind (``prepared`` read as uncertain).
StoreJournal = Union[IssueJournal, "Journal"]


def safe_text(value: str) -> str:
    """Keep literal markup; strip terminal controls, including Unicode format controls."""
    return "".join(
        c for c in value if c in "\n\t" or unicodedata.category(c) not in ("Cc", "Cf", "Cs")
    )


def _body_names(value: object) -> set[str]:
    """Collect inputs only; domain parsers remain the sole body validators."""
    result: set[str] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            body_file = item.get("body_file")
            if isinstance(body_file, str):
                result.add(body_file)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return result


def _issue_action(index: int, action: issues.IssueAction) -> ActionView:
    title: str | None = None
    board_title = getattr(action, "project_title", None)
    board = getattr(action, "project", "") + (f' "{board_title}"' if board_title else "")
    body: str | None
    markdown = True
    if isinstance(action, issues.CreateAction):
        target, title, body, kind = action.ref, action.title, action.body, "create"
    else:
        target = (
            str(action.target.number)
            if isinstance(action.target, issues.ExistingIssue)
            else action.target.ref
        )
        if isinstance(action, issues.EditAction):
            title, body, kind = action.title, action.body, "edit"
        elif isinstance(action, issues.CommentAction):
            body, kind = action.body, "comment"
        elif isinstance(action, issues.LabelsAction):
            body, kind, markdown = (
                f"add: {', '.join(action.add)}\nremove: {', '.join(action.remove)}",
                "labels",
                False,
            )
        elif isinstance(action, issues.AssigneesAction):
            body, kind, markdown = (
                f"add: {', '.join(action.add)}\nremove: {', '.join(action.remove)}",
                "assignees",
                False,
            )
        elif isinstance(action, (issues.ProjectClearAction, issues.ProjectRemoveAction)):
            change = (
                f"{action.field}: {action.expected_value if action.expected_value is not None else '(empty)'} → (empty)"
                if isinstance(action, issues.ProjectClearAction)
                else f"membership: {'on board' if action.expected_present else 'not on board'} → not on board"
            )
            body, kind, markdown = (
                f"project {board} · #{target} · {change}",
                action.kind,
                False,
            )
        elif isinstance(action, issues.ProjectSetAction):
            body, kind, markdown = (
                f"project {board} · #{target} · {action.field}: "
                f"{action.expected_value if action.expected_value is not None else '(empty)'} → {action.value}",
                action.kind,
                False,
            )
        elif isinstance(action, issues.ProjectAddAction):
            before = "on board" if action.expected_present else "not on board"
            body, kind, markdown = f"project {board} · #{target} · membership: {before} → on board", action.kind, False
        elif isinstance(action, issues.StateAction):
            body = action.state + (f" ({action.reason})" if action.reason else "")
            kind, markdown = "state", False
        else:
            assert_never(action)
    return ActionView(index, kind, action.repo, target, title, body, markdown, "pending", None, ())


def _pr_actions(manifest: prs.Manifest, recorded_pr: int | None) -> tuple[ActionView, ...]:
    result = []
    for index, action in enumerate(manifest.actions):
        comments: tuple[CommentView, ...] = ()
        if isinstance(action, prs.ReviewAction):
            kind = "review"
            comments = tuple(
                CommentView(i, f"{c.path}:{c.line}", c.body, c.path, c.line, c.start_line)
                for i, c in enumerate(action.comments)
            )
        elif isinstance(action, prs.DescriptionAction):
            kind = "description"
        elif isinstance(action, prs.ReplyAction):
            kind = "reply"
        else:
            kind = "comment"
        title = action.title if isinstance(action, prs.DescriptionAction) else None
        result.append(
            ActionView(
                index,
                kind,
                manifest.repo,
                str(manifest.pr or recorded_pr or ""),
                title,
                action.body,
                True,
                "pending",
                None,
                comments,
                action.comment_id if isinstance(action, prs.ReplyAction) else None,
            )
        )
    return tuple(result)


def _state(actions: tuple[ActionView, ...]) -> State:
    if any(a.state == "uncertain" for a in actions):
        return "uncertain"
    applied = sum(a.state == "applied" for a in actions)
    if applied and applied == len(actions):
        return "applied"
    return "partial" if applied else "pending"


def _build_view(
    identity: ContainerIdentity,
    store: StoreSnapshot,
    name: str,
    recorded_pr: int | None,
    issue_journals: Mapping[str, IssueJournal] | None = None,
    pr_journals: Mapping[str, StoreJournal] | None = None,
) -> ProposalView:
    files = store.as_dict()
    raw = files.get(name, "")
    error = None
    block = None
    actions: tuple[ActionView, ...] = ()
    state: State = "pending"
    body_names: set[str] = set()
    is_source = False
    try:
        try:
            body_names = _body_names(json.loads(raw))
        except ValueError:
            pass
        if name in store.rejected:
            raise OutboxError("manifest was rejected by the bounded text reader")
        if store.kind == "pr":
            manifest = prs.parse_manifest(name, raw, files)
            actions = _pr_actions(manifest, None if manifest.source else recorded_pr)
            is_source = manifest.source is not None
            if manifest.source is not None:
                state = "awaiting-push"
            elif manifest.pr is None and recorded_pr is None:
                state = "awaiting-pr"
        else:
            issue = issues.parse_manifest(name, raw, files)
            body_names = set(issue.body_files)
            actions = tuple(_issue_action(i, a) for i, a in enumerate(issue.actions))
    except RecursionError:
        error, state = "manifest JSON nesting exceeds the supported depth", "invalid"
    except (prs.ManifestError, issues.IssueManifestError, OutboxError) as exc:
        error, state = str(exc), "invalid"
    # Store-held proposals carry their journals; a PR source's one journaled
    # action (the push and PR create/update) is not one of its manifest actions.
    journals: Mapping[str, StoreJournal] | None = (
        issue_journals if store.kind == "issue" else None if is_source else pr_journals
    )
    # Store journals (a leftover prepared already reads as uncertain).
    journal_evidence: dict[str, object] = {"store": True}
    evidence = journal_evidence
    try:
        journal = journals.get(name) if journals is not None else None
        if journal is not None:
            journal_evidence["journal"] = asdict(journal)
            if journal.actions:
                block = "recorded publication progress prevents editing"
                bodies = {n: files[n] for n in body_names if n in files}
                digest = proposal_digest(name, raw, bodies)
                if journal.digest != digest or journal.action_count != len(actions):
                    raise JournalError("proposal differs from recorded journal history")
                issue_receipts = {r.index: r for r in journal.actions}
                updated = []
                for a in actions:
                    receipt = issue_receipts.get(a.index)
                    local_state: Literal["pending", "applied", "uncertain"] = "pending"
                    if receipt is not None:
                        local_state = "applied" if receipt.state == "applied" else "uncertain"
                    updated.append(
                        replace(a, state=local_state, receipt=receipt.url if receipt else None)
                    )
                actions = tuple(updated)
                if state != "invalid":
                    state = _state(actions)
    except JournalError as exc:
        block = "unusable publication journal prevents editing"
        error = "; ".join(v for v in (error, str(exc)) if v)
        if state != "invalid":
            state = "uncertain"
    relevant_rejected = sorted(n for n in store.rejected if n in body_names or n == name)
    inputs: dict[str, object] = {
        "identity": asdict(identity),
        "kind": store.kind,
        "name": name,
        "raw": raw,
        "bodies": [(n, files.get(n)) for n in sorted(body_names)],
        "progress": evidence,
        "rejected": relevant_rejected,
    }
    if store.versions:
        # Host-store proposals: the row version is part of what the view was built from.
        inputs["row_version"] = dict(store.versions).get(name)
    revision = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return ProposalView(ProposalId(store.kind, name), revision, raw, actions, state, error, block)


def build_views(
    identity: ContainerIdentity,
    stores: Sequence[StoreSnapshot],
    *,
    recorded_pr: int | None = None,
    issue_journals: Mapping[str, IssueJournal] | None = None,
    pr_journals: Mapping[str, StoreJournal] | None = None,
) -> tuple[ProposalView, ...]:
    """Inspect supplied snapshots without target resolution or directory creation.

    issue_journals and pr_journals, when given, are the store's publication
    journals by proposal name; a proposal without one has no progress.

    recorded_pr is display context only and never contributes to revision:
    publication must resolve and authorize its actual target through fresh gates.
    """
    views = []
    for store in stores:
        names = set(store.as_dict()) | set(store.rejected)
        for name in sorted(n for n in names if n.endswith(".json")):
            try:
                ProposalId(store.kind, name)
            except OutboxError:
                # Non-addressable rejected entries remain visible in store evidence.
                continue
            views.append(
                _build_view(identity, store, name, recorded_pr, issue_journals, pr_journals)
            )
    return tuple(views)


def _draftable(proposal: ProposalView) -> bool:
    """Same rule as `pr_outbox.draftable`, read off the action views.

    A PR proposal is draftable while a review or reply is still pending.
    """
    return proposal.id.kind == "pr" and any(
        a.state == "pending" and a.kind in ("review", "reply") for a in proposal.actions
    )


def _action_scope(
    container: ContainerView, proposal: ProposalView, action: ActionView
) -> tuple[list[int] | None, str | None]:
    """Indices a single-action drop removes, or why it is not allowed."""
    selection = delete.DeleteSelection(
        action=action.index,
        with_dependents=proposal.id.kind == "issue" and action.kind == "create",
    )
    try:
        plan = delete.plan_delete(container, proposal.id, selection)
    except OutboxError as exc:
        return None, str(exc)
    return list(plan.removed_actions), None


def _proposal_json(
    container: ContainerView, proposal: ProposalView, clear: delete.ContainerClearPlan
) -> dict[str, object]:
    item = next((i for i in (*clear.delete, *clear.keep) if i.proposal == proposal.id), None)
    deletable = item is not None and item in clear.delete
    archive = bool(item and item.selection and item.selection.archive_journal)
    actions: list[dict[str, object]] = []
    for a in proposal.actions:
        scope, block = _action_scope(container, proposal, a)
        actions.append(
            {
                "index": a.index,
                "kind": a.kind,
                "repo": a.repo,
                "target": a.target,
                "text": a.text,
                "title": a.title,
                "body": a.body,
                "markdown": a.markdown,
                "reply_to": a.reply_to,
                "state": a.state,
                "receipt": a.receipt,
                "delete_scope": scope,
                "delete_block": block,
                "comments": [
                    {
                        "index": c.index,
                        "label": c.label,
                        "text": c.text,
                        "path": c.path,
                        "line": c.line,
                        "start_line": c.start_line,
                    }
                    for c in a.comments
                ],
            }
        )
    return {
        "id": str(proposal.id),
        "kind": proposal.id.kind,
        "name": proposal.id.name,
        "revision": proposal.revision,
        "raw_text": proposal.raw_text,
        "state": proposal.state,
        "error": proposal.error,
        "edit_block": proposal.edit_block,
        "draftable": _draftable(proposal),
        "deletable": deletable,
        "delete_archive": archive,
        "delete_block": item.reason if item is not None and not deletable else None,
        "actions": actions,
    }


def _container_json(container: ContainerView) -> dict[str, object]:
    counts = {
        state: sum(p.state == state for p in container.proposals)
        for state in (
            "pending",
            "partial",
            "applied",
            "uncertain",
            "invalid",
            "awaiting-pr",
            "awaiting-push",
        )
    }
    return {
        "name": container.name,
        "identity": asdict(container.identity) if container.identity else None,
        "available": container.available,
        "destroyed": container.destroyed,
        "error": container.error,
        "counts": counts,
        "stores": [
            {"kind": s.kind, "rejected": list(s.rejected), "warnings": list(s.warnings)}
            for s in container.stores
        ],
    }


def _proposals_json(container: ContainerView) -> list[dict[str, object]]:
    clear = delete.plan_clear(container)
    return [_proposal_json(container, p, clear) for p in container.proposals]


def overview_json(containers: Sequence[ContainerView]) -> dict[str, object]:
    return {
        "schema": 1,
        "containers": [_container_json(c) | {"proposals": _proposals_json(c)} for c in containers],
    }


def detail_json(container: ContainerView, proposal: ProposalView) -> dict[str, object]:
    return {
        "schema": 1,
        "container": _container_json(container),
        "proposal": _proposal_json(container, proposal, delete.plan_clear(container)),
    }
