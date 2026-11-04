"""The outbox Sent log: what the host published to GitHub, kept in the state DB.

`record`, `mark_applied` and `forget` are called by the shared host publish
code right after GitHub accepted an action. They are best effort: a database
or filesystem failure here must never fail or undo a publish, so each swallows
`SQLAlchemyError` / `sqlite3.Error` / `OSError` and warns. The readers
(`list_records`, `get_record`, `clear`, `counts`) raise normally; their
callers decide how to degrade.

One row per action, keyed by `dedup_key` (instance, `container_created`,
outbox, manifest, action index) so a retried or resumed publish updates its
row instead of adding one. All datetimes are UTC.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from sqlalchemy import delete, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, col, select

from jailbee.db import get_engine
from jailbee.db.models import SentRecord

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

Outbox = Literal["issue", "review", "source"]
Publisher = Literal["user", "orchestrator"]
State = Literal["applied", "uncertain"]

_BEST_EFFORT_ERRORS = (SQLAlchemyError, sqlite3.Error, OSError)


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


@dataclass(frozen=True)
class SentRow:
    """One stored record, detached from any session."""

    id: int
    sent_at: datetime
    container_prefix: str
    container: str
    instance: str
    container_created: datetime | None
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

    by_instance: dict[tuple[str, datetime | None], int] = field(default_factory=dict)
    by_prefix: dict[str, int] = field(default_factory=dict)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def dedup_key(
    instance: str, container_created: datetime | None, outbox: str, manifest: str, index: int
) -> str:
    """The unique key of one action; an unknown creation time still keys per instance."""
    created = _utc(container_created)
    stamp = created.isoformat() if created is not None else "-"
    return f"{instance}|{stamp}|{outbox}|{manifest}|{index}"


_SUBSEC_RE = re.compile(r"(\.\d{6})\d+")


def parse_created(raw: str | None) -> datetime | None:
    """An Incus ``created_at`` (RFC3339Nano) as an aware UTC datetime, or None.

    The Go zero time Incus reports for an unset timestamp is None too, so an
    unknown creation time keys the record per instance only.
    """
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


def _short_name(instance: str, container_prefix: str) -> str:
    prefix = f"{container_prefix}-"
    if container_prefix and instance.startswith(prefix):
        return instance[len(prefix) :]
    return instance


def _to_row(rec: SentRecord) -> SentRow:
    assert rec.id is not None
    return SentRow(
        id=rec.id,
        sent_at=rec.sent_at,
        container_prefix=rec.container_prefix,
        container=rec.container,
        instance=rec.instance,
        container_created=rec.container_created,
        publisher=rec.publisher,
        state=rec.state,
        outbox=rec.outbox,
        manifest=rec.manifest,
        index=rec.action_index,
        kind=rec.kind,
        repo=rec.repo,
        target=rec.target,
        url=rec.url,
        title=rec.title,
        content=rec.content,
    )


def _upsert(
    key: str,
    state: State,
    action: SentAction,
    instance: str,
    container_prefix: str,
    container_created: datetime | None,
    publisher: Publisher,
    now: datetime,
) -> None:
    with Session(get_engine()) as session:
        existing = session.exec(select(SentRecord).where(SentRecord.dedup_key == key)).first()
        if existing is None:
            session.add(
                SentRecord(
                    dedup_key=key,
                    sent_at=_utc(now) or now,
                    container_prefix=container_prefix,
                    container=_short_name(instance, container_prefix),
                    instance=instance,
                    container_created=_utc(container_created),
                    publisher=publisher,
                    state=state,
                    outbox=action.outbox,
                    manifest=action.manifest,
                    action_index=action.index,
                    kind=action.kind,
                    repo=action.repo,
                    target=action.target,
                    url=action.url,
                    title=action.title,
                    content=action.content,
                )
            )
        else:
            # sent_at and publisher keep the first attempt's provenance.
            if existing.state != "applied":
                existing.state = state
            existing.url = action.url or existing.url
            existing.target = action.target if action.target is not None else existing.target
            existing.title = action.title or existing.title
            existing.content = action.content or existing.content
            session.add(existing)
        session.commit()


def record(
    *,
    instance: str,
    container_prefix: str,
    container_created: datetime | None,
    publisher: Publisher,
    action: SentAction,
    state: State,
    now: datetime | None = None,
) -> None:
    """Upsert the row for `action`; an `applied` row is never downgraded. Best effort."""
    key = dedup_key(instance, container_created, action.outbox, action.manifest, action.index)
    stamp = now if now is not None else datetime.now(UTC)

    def run() -> None:
        args = (key, state, action, instance, container_prefix, container_created, publisher, stamp)
        try:
            _upsert(*args)
        except IntegrityError:
            # Lost an insert race with another publisher of the same action.
            _upsert(*args)

    _best_effort("record the action", run)


def mark_applied(
    *,
    instance: str,
    container_created: datetime | None,
    outbox: str,
    manifest: str,
    index: int,
    url: str | None,
    target: int | None,
) -> None:
    """Turn an `uncertain` row into `applied`, filling the URL/target. Best effort."""
    key = dedup_key(instance, container_created, outbox, manifest, index)

    def run() -> None:
        with Session(get_engine()) as session:
            rec = session.exec(select(SentRecord).where(SentRecord.dedup_key == key)).first()
            if rec is None:
                return
            rec.state = "applied"
            if url:
                rec.url = url
            if target is not None:
                rec.target = target
            session.add(rec)
            session.commit()

    _best_effort("mark the action applied", run)


def forget(
    *,
    instance: str,
    container_created: datetime | None,
    outbox: str,
    manifest: str,
    index: int,
) -> None:
    """Delete the row of a dropped uncertain action. Best effort."""
    key = dedup_key(instance, container_created, outbox, manifest, index)

    def run() -> None:
        with Session(get_engine()) as session:
            session.exec(delete(SentRecord).where(col(SentRecord.dedup_key) == key))
            session.commit()

    _best_effort("forget the action", run)


def list_records(
    *,
    container_prefix: str | None = None,
    instance: str | None = None,
    container_created: datetime | None = None,
    limit: int | None = None,
) -> list[SentRow]:
    """Records newest first, optionally narrowed to a repo or one container incarnation.

    `container_created` only narrows when `instance` is given too (it names an
    incarnation of that instance); `None` then means "any incarnation".
    """
    stmt = select(SentRecord)
    if container_prefix is not None:
        stmt = stmt.where(SentRecord.container_prefix == container_prefix)
    if instance is not None:
        stmt = stmt.where(SentRecord.instance == instance)
        if container_created is not None:
            stmt = stmt.where(SentRecord.container_created == _utc(container_created))
    stmt = stmt.order_by(col(SentRecord.sent_at).desc(), col(SentRecord.id).desc())
    if limit is not None:
        stmt = stmt.limit(limit)
    with Session(get_engine()) as session:
        return [_to_row(rec) for rec in session.exec(stmt)]


def get_record(id: int) -> SentRow | None:
    with Session(get_engine()) as session:
        rec = session.get(SentRecord, id)
        return _to_row(rec) if rec is not None else None


def clear(*, container_prefix: str, before: datetime | None) -> int:
    """Delete a repo's records (those sent before `before`, if given); return the count."""
    stmt = delete(SentRecord).where(col(SentRecord.container_prefix) == container_prefix)
    if before is not None:
        stmt = stmt.where(col(SentRecord.sent_at) < _utc(before))
    with Session(get_engine()) as session:
        result = session.execute(stmt)
        session.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


def counts() -> SentCounts:
    """Record counts per (instance, incarnation) and per repo prefix: two grouped queries."""
    with Session(get_engine()) as session:
        inst = session.exec(
            select(SentRecord.instance, SentRecord.container_created, func.count()).group_by(
                col(SentRecord.instance), col(SentRecord.container_created)
            )
        ).all()
        pref = session.exec(
            select(SentRecord.container_prefix, func.count()).group_by(
                col(SentRecord.container_prefix)
            )
        ).all()
    return SentCounts(
        by_instance={(i, _utc(c)): int(n) for i, c, n in inst},
        by_prefix={p: int(n) for p, n in pref},
    )
