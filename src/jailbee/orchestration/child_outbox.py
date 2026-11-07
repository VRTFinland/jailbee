"""Fail-closed retention checks for a child's staged GitHub writes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.outbox.models import Kind


class ChildOutboxError(Exception):
    """The child outbox could not be established safely."""


@dataclass(frozen=True)
class PendingGithub:
    issue: tuple[str, ...]
    pr: tuple[str, ...]
    summaries: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.issue) + len(self.pr)


def _staged(uuid: str | None, kind: Kind) -> dict[str, str]:
    """The instance's staged proposals of ``kind`` (name to manifest) from the host store."""
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import publication_db, store_db

    if uuid is None:
        return {}  # no store key: the broker cannot hold proposals for it
    try:
        rows = store_db.read_proposals(publication_db.default_root(), uuid, kind)
    except StoreError as exc:
        raise ChildOutboxError(f"outbox could not be checked: {exc}") from exc
    return {row.name: row.manifest for row in rows}


def _display(text: str) -> str:
    """Escape lone surrogates without changing authoritative filesystem names."""
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _summary(kind: str, name: str, text: str | None) -> str:
    prefix = f"{kind}/{_display(name)}"
    if text is None:
        return prefix
    try:
        data = json.loads(text)
        actions = data["actions"]
        if not isinstance(actions, list) or not actions:
            return prefix
        action = actions[0]
        verb = action["type"]
        if not isinstance(verb, str):
            return prefix
        detail = _display(verb[:40])
        if verb == "create" and isinstance(action.get("title"), str):
            detail += " " + json.dumps(action["title"][:200], ensure_ascii=True)
        elif verb == "review" and isinstance(action.get("comments"), list):
            detail += f" ({len(action['comments'])} comments)"
        if len(actions) > 1:
            detail += f" (+{len(actions) - 1} actions)"
        return f"{prefix}: {detail}"
    except (ValueError, KeyError, TypeError, IndexError, RecursionError):
        return prefix


def probe(uuid: str | None) -> PendingGithub:
    """Count a child's staged proposals of both kinds, with best-effort summaries.

    Everything comes from the host outbox store, keyed by the instance's
    `volatile.uuid`, so a stopped child is checked like a running one and no
    ``incus exec`` runs. Raises `ChildOutboxError` when the store is unreadable.
    """
    issues = _staged(uuid, "issue")
    prs = _staged(uuid, "pr")
    issue = tuple(sorted(issues))
    pr = tuple(sorted(prs))
    summaries = [_summary("issue", name, issues[name]) for name in issue]
    summaries.extend(_summary("pr", name, prs[name]) for name in pr)
    return PendingGithub(issue, pr, tuple(summaries))


def describe(child_short: str, p: PendingGithub) -> str:
    groups = []
    if p.issue:
        groups.append("issue: " + ", ".join(_display(name) for name in p.issue))
    if p.pr:
        groups.append("pr: " + ", ".join(_display(name) for name in p.pr))
    return describe_count(child_short, p.count, "; ".join(groups))


def describe_count(child_short: str, count: int, names: str = "") -> str:
    listing = f" ({names})" if names else ""
    return (
        f"child `{child_short}` has {count} staged GitHub action(s) in its outbox{listing}; "
        "they are never published from a child — ask it to send the content, "
        "then destroy with force: true"
    )


def unchecked(child_short: str) -> str:
    return (
        f"child `{child_short}` outbox could not be checked; restart it so its outbox "
        "can be checked (or use force: true to destroy anyway)"
    )
