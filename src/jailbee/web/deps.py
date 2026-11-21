"""FastAPI dependencies: the app context and the authenticated session.

Ported from codereader's `deps.py`; the token store comes from the app's
own `WebContext` instead of a module global.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from starlette.requests import HTTPConnection

from jailbee.web.auth import SessionInfo
from jailbee.web.context import WebContext


def get_context(conn: HTTPConnection) -> WebContext:
    return cast(WebContext, conn.app.state.web)


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not token:
        return None
    return token


def anonymous_session(now: datetime) -> SessionInfo:
    return SessionInfo(0, "local (no login)", now, now)


@dataclass(frozen=True)
class Authed:
    """The request's session and the token that named it."""

    token: str
    session: SessionInfo


def require_session(request: Request, ctx: Annotated[WebContext, Depends(get_context)]) -> Authed:
    if not ctx.deps.auth_enabled:
        return Authed("", anonymous_session(ctx.deps.clock()))
    token = bearer_token(request)
    session = ctx.sessions.validate(token) if token else None
    if token is None or session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid token"
        )
    return Authed(token, session)


Context = Annotated[WebContext, Depends(get_context)]
RequireSession = Annotated[Authed, Depends(require_session)]
