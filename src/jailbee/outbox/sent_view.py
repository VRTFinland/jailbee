"""Presentation and scope logic for `jailbee outbox sent`; no Click, no Incus here."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jailbee.outbox import sent
from jailbee.outbox.inspect import safe_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jailbee.outbox.sent import SentRow
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

SCHEMA = 1
_UNCERTAIN_NOTE = (
    "uncertain: GitHub may or may not have accepted this action; "
    "check the target, then resolve or drop it in the outbox"
)


def in_scope(rows: Sequence[SentRow], scope: RemoteRepoScope | None) -> list[SentRow]:
    """Rows of repos the caller may see (everything without a scope)."""
    if scope is None:
        return list(rows)
    return [r for r in rows if scope.allows(r.container_prefix)]


def visible_record(id: int, scope: RemoteRepoScope | None) -> SentRow | None:
    """One record, or None when missing or outside the caller's repo scope."""
    row = sent.get_record(id)
    if row is None or (scope is not None and not scope.allows(row.container_prefix)):
        return None
    return row


def repos_with_records(scope: RemoteRepoScope | None) -> list[str]:
    """Repo prefixes that have records and lie in scope, sorted."""
    prefixes = sent.counts().by_prefix
    return sorted(p for p in prefixes if scope is None or scope.allows(p))


def parse_before(text: str) -> datetime:
    """An ISO date or datetime as UTC; a naive value is taken to be UTC."""
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"not an ISO date or datetime: {text!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _when(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M") if value is not None else "-"


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _target(row: SentRow) -> str:
    return f"{row.repo}#{row.target}" if row.target is not None else row.repo


def _state(row: SentRow) -> str:
    return "uncertain?" if row.state == "uncertain" else row.state


def summary(row: SentRow) -> str:
    """One line of what was sent: the title, else the first content line."""
    text = row.title or next((ln for ln in row.content.splitlines() if ln.strip()), "")
    text = safe_text(" ".join(text.split()))
    return text if len(text) <= 60 else text[:59] + "…"


def table_header() -> tuple[str, ...]:
    return ("ID", "Sent (UTC)", "Container", "Action", "Target", "State", "By", "Summary")


def table_row(row: SentRow) -> tuple[str, ...]:
    return (
        str(row.id),
        _when(row.sent_at),
        safe_text(row.container or row.instance),
        f"{row.outbox}/{row.kind}",
        safe_text(_target(row)),
        _state(row),
        row.publisher,
        summary(row),
    )


def row_json(row: SentRow) -> dict[str, object]:
    return {
        "id": row.id,
        "sent_at": _iso(row.sent_at),
        "container_prefix": row.container_prefix,
        "container": row.container,
        "instance": row.instance,
        "container_created": _iso(row.container_created),
        "publisher": row.publisher,
        "state": row.state,
        "outbox": row.outbox,
        "manifest": row.manifest,
        "index": row.index,
        "kind": row.kind,
        "repo": row.repo,
        "target": row.target,
        "url": row.url,
        "title": row.title,
        "content": row.content,
    }


def rows_json(rows: Sequence[SentRow]) -> dict[str, object]:
    return {"schema": SCHEMA, "records": [row_json(r) for r in rows]}


def show_lines(row: SentRow) -> list[str]:
    """The full record as text; content is printed verbatim (controls stripped)."""
    lines = [
        f"Record {row.id}",
        f"Sent:       {_when(row.sent_at)} UTC",
        f"Container:  {safe_text(row.container)} ({safe_text(row.instance)})",
        f"Created:    {_when(row.container_created)} UTC",
        f"Repo:       {safe_text(row.container_prefix)}",
        f"Published:  by {row.publisher}",
        f"Action:     {row.outbox}/{row.kind}",
        f"Manifest:   {safe_text(row.manifest)} #{row.index}",
        f"Target:     {safe_text(_target(row))}",
        f"State:      {_state(row)}",
        f"URL:        {safe_text(row.url) if row.url else '-'}",
    ]
    if row.title:
        lines.append(f"Title:      {safe_text(row.title)}")
    if row.state == "uncertain":
        lines += ["", _UNCERTAIN_NOTE]
    lines += ["", safe_text(row.content)]
    return lines
