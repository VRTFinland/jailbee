"""`SessionInfoStore` over the state database (`session_info_cache`).

Keeps the web dashboard's session-list rows across server restarts and
upgrades, so only a transcript that changed since is read again. A database
error degrades to "not stored": the row is read from the file instead.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from jailbee.db.models import SessionInfoRow
from jailbee.transcripts.base import SESSION_INFO_FORMAT, SessionInfo, Stamp

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

class DbSessionInfoStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load_all(self) -> dict[Path, tuple[Stamp, SessionInfo]]:
        try:
            with Session(self._engine) as db:
                rows = db.exec(
                    select(SessionInfoRow).where(SessionInfoRow.format == SESSION_INFO_FORMAT)
                ).all()
                found = {Path(row.path): _entry(row) for row in rows}
        except SQLAlchemyError:
            log.debug("session info cache unreadable", exc_info=True)
            return {}
        return {path: entry for path, entry in found.items() if entry is not None}

    def save(self, entries: Sequence[tuple[Path, Stamp, SessionInfo]]) -> bool:
        rows = [
            {
                "path": str(path),
                "size": stamp[0],
                "mtime_ns": stamp[1],
                "format": SESSION_INFO_FORMAT,
                "session_id": info.session_id,
                "title": info.title,
                "started": info.started,
                "last_activity": info.last_activity,
                "models": json.dumps(list(info.models)),
            }
            for path, stamp, info in entries
        ]
        if not rows:
            return True
        table = SessionInfoRow.__table__  # type: ignore[attr-defined]  # SQLModel sets it on table classes
        statement = sqlite_insert(table)
        statement = statement.on_conflict_do_update(
            index_elements=["path"],
            set_={key: statement.excluded[key] for key in rows[0] if key != "path"},
        )
        try:
            with self._engine.begin() as conn:
                conn.execute(statement, rows)
        except SQLAlchemyError:
            log.debug("session info cache not written", exc_info=True)
            return False
        return True


def _entry(row: SessionInfoRow) -> tuple[Stamp, SessionInfo] | None:
    try:
        models = json.loads(row.models)
    except ValueError:
        return None
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        return None
    info = SessionInfo(
        session_id=row.session_id,
        path=Path(row.path),
        title=row.title,
        started=row.started,
        last_activity=row.last_activity,
        models=tuple(models),
    )
    return (row.size, row.mtime_ns), info
