"""Pure local views; publication authorization remains with the domain preflight."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from typing import Literal

from jailbee import issue_manifest as issues
from jailbee import pr_outbox as prs
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
    JournalError,
    JournalStore,
    issue_proposal_digest,
    journal_key,
)


@dataclass(frozen=True)
class ProgressEvidence:
    """Strict PR evidence, reusable without discarding rejected snapshot entries."""

    applied: frozenset[int] = frozenset()
    receipts: tuple[tuple[int, str], ...] = ()
    edit_block: str | None = None
    error: str | None = None
    inputs: tuple[tuple[str, str | None], ...] = ()
    rejected: tuple[str, ...] = ()


def pr_progress_evidence(
    store: StoreSnapshot,
    name: str,
    action_count: int | None,
) -> ProgressEvidence:
    """Never coerce malformed/skipped sidecars or logs to empty progress."""
    files = store.as_dict()
    sidecar = f"{name}.progress.json"
    rejected = tuple(sorted(n for n in store.rejected if n in (sidecar, "applied.log")))
    inputs: list[tuple[str, str | None]] = [(sidecar, files.get(sidecar))]
    error = "publication evidence was rejected" if rejected else None
    block = (
        "recorded publication evidence prevents editing" if rejected or sidecar in files else None
    )
    applied: frozenset[int] = frozenset()
    receipts: tuple[tuple[int, str], ...] = ()
    if sidecar in files:
        try:
            value = json.loads(files[sidecar])
            if not isinstance(value, dict) or set(value) != {"applied", "urls"}:
                raise ValueError
            indices, urls = value["applied"], value["urls"]
            if not isinstance(indices, list) or not isinstance(urls, dict):
                raise ValueError
            if any(
                type(i) is not int or i < 0 or (action_count is not None and i >= action_count)
                for i in indices
            ) or len(set(indices)) != len(indices):
                raise ValueError
            if any(
                not isinstance(k, str)
                or not k.isascii()
                or not k.isdecimal()
                or str(int(k)) != k
                or int(k) not in indices
                or not isinstance(v, str)
                for k, v in urls.items()
            ):
                raise ValueError
            applied = frozenset(indices)
            receipts = tuple(sorted((int(k), v) for k, v in urls.items()))
        except (ValueError, TypeError, RecursionError):
            error = "invalid PR progress sidecar"
    # The writer separates timestamp/name/suffix with one literal space. Never
    # split the name: existing histories can include leading or embedded spaces.
    for line in files.get("applied.log", "").splitlines():
        _, separator, remainder = line.partition(" ")
        prefix = name + " "
        matching_name = remainder == name or remainder.startswith(prefix)
        suffix = remainder[len(prefix) :] if remainder.startswith(prefix) else ""
        exact = re.fullmatch(r"pr=([^ ]+) actions=([0-9]+) urls=(.+)", suffix) is not None
        # Legacy records do not escape filenames. Even an existing longer name
        # cannot prove this is not malformed history for the selected proposal.
        if separator and matching_name:
            inputs.append(("applied.log", line))
            block = "recorded publication evidence prevents editing"
            if not exact:
                error = "invalid PR receipt log record"
            elif not applied:
                error = "receipt log records publication without usable action progress"
    return ProgressEvidence(applied, receipts, block, error, tuple(inputs), rejected)


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
        else:
            body = action.state + (f" ({action.reason})" if action.reason else "")
            kind, markdown = "state", False
    return ActionView(index, kind, action.repo, target, title, body, markdown, "pending", None, ())


def _pr_actions(manifest: prs.Manifest, recorded_pr: int | None) -> tuple[ActionView, ...]:
    result = []
    for index, action in enumerate(manifest.actions):
        comments: tuple[CommentView, ...] = ()
        if isinstance(action, prs.ReviewAction):
            kind = "review"
            comments = tuple(
                CommentView(i, f"{c.path}:{c.line}", c.body) for i, c in enumerate(action.comments)
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
    journal_store: JournalStore,
    recorded_pr: int | None,
) -> ProposalView:
    files = store.as_dict()
    raw = files.get(name, "")
    error = None
    block = None
    actions: tuple[ActionView, ...] = ()
    state: State = "pending"
    body_names: set[str] = set()
    evidence: object = None
    try:
        try:
            body_names = _body_names(json.loads(raw))
        except ValueError:
            pass
        if name in store.rejected:
            raise OutboxError("manifest was rejected by the bounded text reader")
        if store.kind == "pr":
            manifest = prs.parse_manifest(name, raw, files)
            actions = _pr_actions(manifest, recorded_pr)
            if manifest.pr is None and recorded_pr is None:
                state = "awaiting-pr"
        else:
            issue = issues.parse_manifest(name, raw, files)
            body_names = set(issue.body_files)
            actions = tuple(_issue_action(i, a) for i, a in enumerate(issue.actions))
    except RecursionError:
        error, state = "manifest JSON nesting exceeds the supported depth", "invalid"
    except (prs.ManifestError, issues.IssueManifestError, OutboxError) as exc:
        error, state = str(exc), "invalid"
    if store.kind == "pr":
        progress = pr_progress_evidence(store, name, len(actions) if state != "invalid" else None)
        evidence = asdict(progress) | {"applied": sorted(progress.applied)}
        block = progress.edit_block
        if progress.error:
            error = "; ".join(v for v in (error, progress.error) if v)
            if state != "invalid":
                state = "uncertain"
        elif state != "invalid":
            receipts = dict(progress.receipts)
            actions = tuple(
                replace(
                    a,
                    state="applied" if a.index in progress.applied else "pending",
                    receipt=receipts.get(a.index),
                )
                for a in actions
            )
            if progress.applied:
                state = _state(actions)
    else:
        key = journal_key(identity, name)
        # Retain exact bytes as well as validated/recovered semantics, even on corruption.
        try:
            journal_bytes = journal_store._path(key).read_bytes()
        except FileNotFoundError:
            journal_bytes = None
        except OSError as exc:
            journal_bytes = str(exc).encode()
        journal_evidence: dict[str, object] = {
            "raw": None if journal_bytes is None else journal_bytes.hex()
        }
        evidence = journal_evidence
        try:
            journal = journal_store.load(key)
            if journal is not None:
                journal_evidence["journal"] = asdict(journal)
                if journal.actions:
                    block = "recorded publication progress prevents editing"
                    digest = issue_proposal_digest(
                        key,
                        raw,
                        {n: files[n] for n in body_names if n in files},
                        journal,
                    )
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
    relevant_rejected = sorted(
        n
        for n in store.rejected
        if n in body_names or n == name or n == f"{name}.progress.json" or n == "applied.log"
    )
    inputs = {
        "identity": asdict(identity),
        "kind": store.kind,
        "name": name,
        "raw": raw,
        "bodies": [(n, files.get(n)) for n in sorted(body_names)],
        "progress": evidence,
        "rejected": relevant_rejected,
    }
    revision = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return ProposalView(ProposalId(store.kind, name), revision, raw, actions, state, error, block)


def build_views(
    identity: ContainerIdentity,
    stores: Sequence[StoreSnapshot],
    *,
    journal_store: JournalStore,
    recorded_pr: int | None = None,
) -> tuple[ProposalView, ...]:
    """Inspect supplied snapshots without target resolution or directory creation.

    recorded_pr is display context only and never contributes to revision:
    publication must resolve and authorize its actual target through fresh gates.
    """
    views = []
    for store in stores:
        names = set(store.as_dict()) | set(store.rejected)
        for name in sorted(
            n for n in names if n.endswith(".json") and not n.endswith(".progress.json")
        ):
            try:
                ProposalId(store.kind, name)
            except OutboxError:
                # Non-addressable rejected entries remain visible in store evidence.
                continue
            views.append(_build_view(identity, store, name, journal_store, recorded_pr))
    return tuple(views)


def _proposal_json(proposal: ProposalView) -> dict[str, object]:
    return {
        "id": str(proposal.id),
        "kind": proposal.id.kind,
        "name": proposal.id.name,
        "revision": proposal.revision,
        "raw_text": proposal.raw_text,
        "state": proposal.state,
        "error": proposal.error,
        "edit_block": proposal.edit_block,
        "actions": [
            {
                "index": a.index,
                "kind": a.kind,
                "repo": a.repo,
                "target": a.target,
                "text": a.text,
                "state": a.state,
                "receipt": a.receipt,
                "comments": [
                    {"index": c.index, "label": c.label, "text": c.text} for c in a.comments
                ],
            }
            for a in proposal.actions
        ],
    }


def _container_json(container: ContainerView) -> dict[str, object]:
    counts = {
        state: sum(p.state == state for p in container.proposals)
        for state in ("pending", "partial", "applied", "uncertain", "invalid", "awaiting-pr")
    }
    return {
        "name": container.name,
        "identity": asdict(container.identity) if container.identity else None,
        "available": container.available,
        "error": container.error,
        "counts": counts,
        "stores": [
            {"kind": s.kind, "rejected": list(s.rejected), "warnings": list(s.warnings)}
            for s in container.stores
        ],
    }


def overview_json(containers: Sequence[ContainerView]) -> dict[str, object]:
    return {
        "schema": 1,
        "containers": [
            _container_json(c) | {"proposals": [_proposal_json(p) for p in c.proposals]}
            for c in containers
        ],
    }


def detail_json(container: ContainerView, proposal: ProposalView) -> dict[str, object]:
    return {
        "schema": 1,
        "container": _container_json(container),
        "proposal": _proposal_json(proposal),
    }
