"""Login, logout and the caller's session. Ported from codereader's `auth_routes.py`.

Only login endpoints are exposed: managing auth methods and sessions is
CLI-only (spec #109).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from jailbee.web import auth_admin
from jailbee.web.deps import Context, RequireSession

router = APIRouter(prefix="/api", tags=["auth"])

DeviceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class PinLogin(BaseModel):
    """`POST /api/auth` with the one-time PIN."""

    model_config = ConfigDict(extra="forbid")
    method: Literal["pin"]
    pin: str
    device_name: DeviceName


class TotpLogin(BaseModel):
    """`POST /api/auth` with a code from an authenticator app."""

    model_config = ConfigDict(extra="forbid")
    method: Literal["totp"]
    code: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)]
    device_name: DeviceName


Login = Annotated[PinLogin | TotpLogin, Field(discriminator="method")]


class TokenResponse(BaseModel):
    token: str


class SessionResponse(BaseModel):
    device_name: str
    created_at: datetime
    last_used_at: datetime


class MethodsResponse(BaseModel):
    methods: list[str]


@router.get("/auth/methods")
def auth_methods(ctx: Context) -> MethodsResponse:
    return MethodsResponse(methods=list(auth_admin.enabled_methods(ctx.deps.engine)))


def _source(request: Request) -> str:
    return request.client.host if request.client is not None else ""


def _attempt(body: PinLogin | TotpLogin, ctx: Context) -> bool:
    if isinstance(body, PinLogin):
        return auth_admin.pin_enabled(ctx.deps.engine) and ctx.deps.pin.attempt(body.pin)
    return ctx.totp.verify(body.code)


@router.post("/auth")
def authenticate(body: Login, ctx: Context, request: Request) -> TokenResponse:
    source = _source(request)

    def audit(event: auth_admin.AuthEventKind) -> None:
        auth_admin.record_event(
            ctx.deps.engine,
            event,
            clock=ctx.deps.clock,
            method=body.method,
            device_name=body.device_name,
            source=source,
        )

    if not ctx.limiter.allow():
        # Not a database row: a flood of refused requests must not churn the audit table.
        auth_admin.log.warning(
            "web auth rate-limited method=%s device=%r source=%s",
            body.method,
            body.device_name,
            source or "-",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts, try again later",
        )
    if not _attempt(body, ctx):
        audit("login_failed")
        detail = "Invalid or expired PIN" if body.method == "pin" else "Invalid TOTP code"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
    token = ctx.sessions.create(body.device_name)
    audit("login")
    return TokenResponse(token=token)


@router.get("/session")
def current_session(authed: RequireSession) -> SessionResponse:
    s = authed.session
    return SessionResponse(
        device_name=s.device_name, created_at=s.created_at, last_used_at=s.last_used_at
    )


@router.post("/logout")
def logout(authed: RequireSession, ctx: Context, request: Request) -> dict[str, bool]:
    ctx.sessions.revoke(authed.token)
    auth_admin.record_event(
        ctx.deps.engine,
        "logout",
        clock=ctx.deps.clock,
        device_name=authed.session.device_name,
        source=_source(request),
    )
    return {"ok": True}
