"""Login, logout and the caller's session. Ported from codereader's `auth_routes.py`.

Only login endpoints are exposed: managing auth methods and sessions is
CLI-only (spec #109).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, StringConstraints

from jailbee.web.deps import Context, RequireSession

router = APIRouter(prefix="/api", tags=["auth"])

DeviceName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class PinLogin(BaseModel):
    """`POST /api/auth` with the one-time PIN. Later slices add TOTP and FIDO2 bodies."""

    model_config = ConfigDict(extra="forbid")
    method: Literal["pin"]
    pin: str
    device_name: DeviceName


class TokenResponse(BaseModel):
    token: str


class SessionResponse(BaseModel):
    device_name: str
    created_at: datetime
    last_used_at: datetime


class MethodsResponse(BaseModel):
    methods: list[str]


@router.get("/auth/methods")
def auth_methods() -> MethodsResponse:
    return MethodsResponse(methods=["pin"])


@router.post("/auth")
def authenticate(body: PinLogin, ctx: Context) -> TokenResponse:
    if not ctx.limiter.allow():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts, try again later",
        )
    if not ctx.deps.pin.attempt(body.pin):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired PIN"
        )
    return TokenResponse(token=ctx.sessions.create(body.device_name))


@router.get("/session")
def current_session(authed: RequireSession) -> SessionResponse:
    s = authed.session
    return SessionResponse(
        device_name=s.device_name, created_at=s.created_at, last_used_at=s.last_used_at
    )


@router.post("/logout")
def logout(authed: RequireSession, ctx: Context) -> dict[str, bool]:
    ctx.sessions.revoke(authed.token)
    return {"ok": True}
