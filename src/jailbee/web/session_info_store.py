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

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from jailbee.db.models import SessionInfoRow
from jailbee.transcripts.base import SessionInfo, Stamp

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

SESSION_INFO_FORMAT = 1
"""Bump when what a source derives for a `SessionInfo` changes, so rows
stored by an older reader are read again instead of served."""


class DbSessionInfoStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, path: Path) -> tuple[Stamp, SessionInfo] | None:
        try:
            with Session(self._engine) as db:
                row = db.get(SessionInfoRow, str(path))
        except SQLAlchemyError:
            log.debug("session info cache unreadable", exc_info=True)
            return None
        if row is None or row.format != SESSION_INFO_FORMAT:
            return None
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

    def save(self, path: Path, stamp: Stamp, info: SessionInfo) -> None:
        row = SessionInfoRow(
            path=str(path),
            size=stamp[0],
            mtime_ns=stamp[1],
            format=SESSION_INFO_FORMAT,
            session_id=info.session_id,
            title=info.title,
            started=info.started,
            last_activity=info.last_activity,
            models=json.dumps(list(info.models)),
        )
        try:
            with Session(self._engine) as db:
                db.merge(row)
                db.commit()
        except SQLAlchemyError:
            log.debug("session info cache not written", exc_info=True)
