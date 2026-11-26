"""Persistence for the state service's action snapshots."""

import json
import logging
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

from pydantic import TypeAdapter
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jailbee.db.models import ActionEntryRow
from jailbee.state_service.protocol import ActionEntry

log = logging.getLogger(__name__)
_ADAPTER = TypeAdapter(ActionEntry)
MAX_FINISHED_ACTIONS = 100
MAX_FINISHED_AGE = timedelta(days=7)


def prune_finished(
    entries: list[ActionEntry], *, now: datetime
) -> tuple[list[ActionEntry], set[int]]:
    """Keep at most 100 newest finished actions from the last seven days."""
    cutoff = now - MAX_FINISHED_AGE
    finished = sorted(
        (entry for entry in entries if entry.finished),
        key=lambda entry: (entry.finished_at or datetime.min.replace(tzinfo=UTC), entry.entry_id),
        reverse=True,
    )
    keep = {
        entry.entry_id
        for entry in finished[:MAX_FINISHED_ACTIONS]
        if entry.finished_at is not None and entry.finished_at >= cutoff
    }
    return [entry for entry in entries if not entry.finished or entry.entry_id in keep], {
        entry.entry_id for entry in finished if entry.entry_id not in keep
    }


def _encode(entry: ActionEntry) -> str:
    payload = asdict(entry)
    if entry.finished_at is not None:
        payload["finished_at"] = entry.finished_at.isoformat()
    return json.dumps({"version": 1, "entry": payload}, separators=(",", ":"))


def _decode(payload: str) -> ActionEntry:
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("invalid action payload")
    if "version" in raw:
        if raw["version"] != 1:
            raise ValueError("unsupported action payload version")
        raw = raw.get("entry")
    else:
        # Version zero was the wire message itself.
        if raw.pop("type", None) != "action_entry":
            raise ValueError("invalid legacy action payload")
    return _ADAPTER.validate_python(raw)


def save(engine: Engine, entry: ActionEntry) -> None:
    with Session(engine) as session:
        session.merge(
            ActionEntryRow(entry_id=entry.entry_id, state=entry.state, payload=_encode(entry))
        )
        session.commit()


def delete(engine: Engine, entry_id: int) -> None:
    with Session(engine) as session:
        row = session.get(ActionEntryRow, entry_id)
        if row is not None:
            session.delete(row)
            session.commit()


def load(engine: Engine) -> list[ActionEntry]:
    entries = []
    with Session(engine) as session:
        for row in session.exec(select(ActionEntryRow)).all():
            try:
                entry = _decode(row.payload)
                if entry.entry_id != row.entry_id:
                    raise ValueError("action id mismatch")
                if entry.finished_at is not None and entry.finished_at.utcoffset() is None:
                    raise ValueError("action timestamp has no timezone")
            except (ValueError, TypeError) as exc:
                log.warning("Skipping invalid persisted action %s: %s", row.entry_id, exc)
                continue
            if entry.state in ("running", "awaiting"):
                entry = replace(
                    entry, state="interrupted", finished=True, finished_at=datetime.now(UTC)
                )
                row.state = entry.state
                row.payload = _encode(entry)
                session.add(row)
            elif entry.finished and entry.finished_at is None:
                # Existing history predates completion timestamps; give it one
                # migration-time origin so retention can age it predictably.
                entry = replace(entry, finished_at=datetime.now(UTC))
                row.payload = _encode(entry)
                session.add(row)
            entries.append(entry)
        pruned, evicted = prune_finished(entries, now=datetime.now(UTC))
        for entry_id in evicted:
            row = session.get(ActionEntryRow, entry_id)
            if row is not None:
                session.delete(row)
        session.commit()
    return pruned
