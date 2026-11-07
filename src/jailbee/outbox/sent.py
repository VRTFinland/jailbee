"""The outbox Sent log: what the host published to GitHub, kept in the outbox store.

Records live in ``outbox_sent`` in the broker's ``broker.db`` (see
`jailbee.outbox.publication_db`), keyed by the container's Incus
``volatile.uuid``: one row per action of one exact proposal (uuid, outbox,
manifest, proposal digest, action index), so a retried or resumed publish
updates its row instead of adding one, and a recreated container (new uuid)
gets rows of its own.

Issue publication writes its records inside the journal transaction
(`publication_db.PublicationStore.settle`). The PR family (review, source)
still calls `record`, `mark_applied` and `forget` right after GitHub accepted an
action; these are best effort: a store failure must never fail or undo a
publish, so each swallows `StoreError` / `sqlite3.Error` / `OSError` and warns.
The readers (`list_records`, `get_record`, `clear`, `counts`) raise normally
(`StoreError`, an `OSError`); their callers decide how to degrade. Readers open
the store read-only and never create it. All datetimes are UTC.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from jailbee.incus import IncusError
from jailbee.outbox import publication_db as pdb
from jailbee.outbox_io import JournalError

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

Outbox = Literal["issue", "review", "source"]
Publisher = Literal["user", "orchestrator"]
State = Literal["applied", "uncertain"]

# JournalError / IncusError: an instance identity or uuid lookup that failed.
_BEST_EFFORT_ERRORS: tuple[type[Exception], ...] = (
    sqlite3.Error,
    OSError,
    JournalError,
    IncusError,
)


@dataclass(frozen=True)
class SentAction:
    """What one accepted action sent, taken from the manifest snapshot."""

    outbox: Outbox
    manifest: str
    index: int
    kind: str
    repo: str
    target: int | None
    url: str | None
    title: str | None
    content: str
    digest: str
    """Digest of the proposal the action came from; tells a reused manifest name apart."""


@dataclass(frozen=True)
class SentRow:
    """One stored record."""

    id: int
    sent_at: datetime
    container_prefix: str
    container: str  # short name
    instance: str  # full Incus name
    uuid: str  # the instance incarnation (Incus volatile.uuid)
    publisher: str
    state: str
    outbox: str
    manifest: str
    index: int
    kind: str
    repo: str
    target: int | None
    url: str | None
    title: str | None
    content: str


@dataclass(frozen=True)
class SentCounts:
    """Record counts the dashboards' gatherer carries in its snapshot."""

    by_uuid: dict[str, int] = field(default_factory=dict)
    by_prefix: dict[str, int] = field(default_factory=dict)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


_SUBSEC_RE = re.compile(r"(\.\d{6})\d+")


def parse_created(raw: str | None) -> datetime | None:
    """An Incus ``created_at`` (RFC3339Nano) as an aware UTC datetime, or None."""
    if not raw:
        return None
    text = _SUBSEC_RE.sub(r"\1", raw.strip())
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return None if parsed.year <= 1 else _utc(parsed)


def _warn(what: str, exc: Exception) -> None:
    msg = f"Could not {what} in the outbox Sent log: {exc}"
    if sys.stderr.isatty():
        from jailbee import tui

        tui.warn_plain(msg, stderr=True)
    else:
        log.warning(msg)


def _best_effort(what: str, fn: Callable[[], None]) -> None:
    try:
        fn()
    except _BEST_EFFORT_ERRORS as exc:
        _warn(what, exc)


def short_name(instance: str, container_prefix: str) -> str:
    prefix = f"{container_prefix}-"
    if container_prefix and instance.startswith(prefix):
        return instance[len(prefix) :]
    return instance


def entry(
    action: SentAction,
    *,
    instance: str,
    uuid: str,
    container_prefix: str,
    publisher: Publisher,
) -> pdb.SentEntry:
    """The stored form of ``action`` published from ``instance``."""
    return pdb.SentEntry(
        uuid=uuid,
        container_prefix=container_prefix,
        container=instance,
        publisher=publisher,
        outbox=action.outbox,
        manifest=action.manifest,
        digest=action.digest,
        index=action.index,
        kind=action.kind,
        repo=action.repo,
        target=action.target,
        url=action.url,
        title=action.title,
        content=action.content,
    )


def _to_row(rec: pdb.SentRecord) -> SentRow:
    return SentRow(
        id=rec.id,
        sent_at=datetime.fromtimestamp(rec.sent_at, UTC),
        container_prefix=rec.container_prefix,
        container=short_name(rec.container, rec.container_prefix),
        instance=rec.container,
        uuid=rec.uuid,
        publisher=rec.publisher,
        state=rec.state,
        outbox=rec.outbox,
        manifest=rec.manifest,
        index=rec.index,
        kind=rec.kind,
        repo=rec.repo,
        target=rec.target,
        url=rec.url,
        title=rec.title,
        content=rec.content,
    )


def record(
    *,
    instance: str,
    uuid: str | Callable[[], str],
    container_prefix: str,
    publisher: Publisher,
    action: SentAction | Callable[[], SentAction],
    state: State,
    now: datetime | None = None,
) -> None:
    """Upsert without downgrading applied rows.

    A factory for the action or the uuid is called best effort too, so a
    failed lookup after GitHub accepted the action only warns.
    """

    def run() -> None:
        built = action() if callable(action) else action
        key = uuid() if callable(uuid) else uuid
        stamp = _utc(now) or datetime.now(UTC)
        pdb.open_publications().record_sent(
            entry(
                built,
                instance=instance,
                uuid=key,
                container_prefix=container_prefix,
                publisher=publisher,
            ),
            state,
            now=stamp.timestamp(),
        )

    _best_effort("record the action", run)


def mark_applied(
    *,
    uuid: str,
    outbox: str,
    manifest: str,
    digest: str,
    index: int,
    url: str | None,
    target: int | None,
) -> None:
    """Turn an `uncertain` row into `applied`, filling the URL/target. Best effort."""
    _best_effort(
        "mark the action applied",
        lambda: pdb.open_publications().mark_sent(
            uuid=uuid,
            outbox=outbox,
            manifest=manifest,
            digest=digest,
            index=index,
            url=url,
            target=target,
        ),
    )


def forget(*, uuid: str, outbox: str, manifest: str, digest: str, index: int) -> None:
    """Delete the `uncertain` row of a dropped action; applied rows stay. Best effort."""
    _best_effort(
        "forget the action",
        lambda: pdb.open_publications().forget_sent(
            uuid=uuid, outbox=outbox, manifest=manifest, digest=digest, index=index
        ),
    )


def list_records(
    *,
    container_prefix: str | None = None,
    uuid: str | None = None,
    limit: int | None = None,
) -> list[SentRow]:
    """Records newest first, optionally narrowed to a repo or one container incarnation."""
    rows = pdb.read_sent(
        pdb.default_root(), container_prefix=container_prefix, uuid=uuid, limit=limit
    )
    return [_to_row(rec) for rec in rows]


def get_record(id: int) -> SentRow | None:
    rows = pdb.read_sent(pdb.default_root(), record_id=id)
    return _to_row(rows[0]) if rows else None


def clear(*, container_prefix: str, before: datetime | None) -> int:
    """Delete a repo's records (those sent before `before`, if given); return the count."""
    cutoff = _utc(before)
    return pdb.open_publications().clear_sent(
        container_prefix=container_prefix,
        before=None if cutoff is None else cutoff.timestamp(),
    )


def counts() -> SentCounts:
    """Record counts per instance incarnation (uuid) and per repo prefix."""
    by_uuid, by_prefix = pdb.sent_counts(pdb.default_root())
    return SentCounts(by_uuid=by_uuid, by_prefix=by_prefix)
