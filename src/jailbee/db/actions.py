"""Persistence for the state service's action snapshots."""

import json
import logging
from dataclasses import asdict, replace

from pydantic import TypeAdapter
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jailbee.db.models import ActionEntryRow
from jailbee.state_service.protocol import ActionEntry

log = logging.getLogger(__name__)
_ADAPTER = TypeAdapter(ActionEntry)


def _encode(entry: ActionEntry) -> str:
    return json.dumps({"version": 1, "entry": asdict(entry)}, separators=(",", ":"))


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
            ActionEntryRow(
                entry_id=entry.entry_id, state=entry.state, payload=_encode(entry)
            )
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
            except (ValueError, TypeError) as exc:
                log.warning("Skipping invalid persisted action %s: %s", row.entry_id, exc)
                continue
            if entry.state in ("running", "awaiting"):
                entry = replace(entry, state="interrupted", finished=True)
                row.state = entry.state
                row.payload = _encode(entry)
                session.add(row)
            entries.append(entry)
        session.commit()
    return entries
