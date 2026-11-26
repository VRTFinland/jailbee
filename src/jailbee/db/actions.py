"""Persistence for the state service's action snapshots."""

from dataclasses import replace

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jailbee.db.models import ActionEntryRow
from jailbee.state_service.protocol import ActionEntry, decode, encode


def save(engine: Engine, entry: ActionEntry) -> None:
    with Session(engine) as session:
        session.merge(
            ActionEntryRow(
                entry_id=entry.entry_id, state=entry.state, payload=encode(entry).decode()
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
            entry = decode(row.payload.encode())
            if not isinstance(entry, ActionEntry):
                raise ValueError("invalid persisted action entry")
            if entry.state in ("running", "awaiting"):
                entry = replace(entry, state="interrupted", finished=True)
                row.state = entry.state
                row.payload = encode(entry).decode()
                session.add(row)
            entries.append(entry)
        session.commit()
    return entries
