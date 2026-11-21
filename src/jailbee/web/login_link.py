"""One-time localhost login links: `jailbee remote web open` mints, `POST /api/auth` consumes.

Imports no FastAPI, so the CLI mints without loading the server. Only the
token's SHA-256 digest is stored; a link works once and for `LINK_TTL`.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import TYPE_CHECKING

from sqlmodel import Session, col, delete

from jailbee.db.models import WebLoginLink
from jailbee.web.auth import Clock, utcnow

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

LINK_TTL = timedelta(seconds=60)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(engine: Engine, *, clock: Clock = utcnow) -> str:
    """A new single-use token valid for `LINK_TTL`; stale rows are dropped."""
    token = secrets.token_urlsafe(32)
    now = clock()
    with Session(engine) as s:
        s.exec(delete(WebLoginLink).where(col(WebLoginLink.expires_at) < now))
        s.add(WebLoginLink(token_hash=_hash(token), expires_at=now + LINK_TTL))
        s.commit()
    return token


def consume(engine: Engine, token: str, *, clock: Clock = utcnow) -> bool:
    """Use ``token``: True once if it is known and unexpired, never again."""
    if not token:
        return False
    now = clock()
    with Session(engine) as s:
        # One DELETE decides: of two concurrent uses only one removes the row.
        used = s.exec(
            delete(WebLoginLink).where(
                col(WebLoginLink.token_hash) == _hash(token),
                col(WebLoginLink.expires_at) >= now,
            )
        )
        s.exec(delete(WebLoginLink).where(col(WebLoginLink.token_hash) == _hash(token)))
        s.commit()
        return used.rowcount == 1
