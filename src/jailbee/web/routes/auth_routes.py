"""Login, logout and the caller's session. Ported from codereader's `auth_routes.py`.

Only login endpoints are exposed: managing auth methods and sessions is
CLI-only (spec #109).
"""

from __future__ import annotations

import math
import unicodedata
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from jailbee.web import auth_admin, login_link
from jailbee.web.deps import Context, RequestRp, RequireSession, request_rp
from jailbee.web.device_name import derive_device_name
from jailbee.web.fido2_auth import Fido2Error

router = APIRouter(prefix="/api", tags=["auth"])


def _no_control_characters(name: str) -> str:
    """Reject Unicode categories Cc and Cf: they would mangle the operator's terminal."""
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in name):
        raise ValueError("must not contain control characters")
    return name


DeviceName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    AfterValidator(_no_control_characters),
]


class PinLogin(BaseModel):
    """`POST /api/auth` with the one-time PIN."""

    model_config = ConfigDict(extra="forbid")
    method: Literal["pin"]
    pin: str
    device_name: DeviceName | None = None
    """Optional: an old client names its device; otherwise the User-Agent does."""


class TotpLogin(BaseModel):
    """`POST /api/auth` with a code from an authenticator app."""

    model_config = ConfigDict(extra="forbid")
    method: Literal["totp"]
    code: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)]
    device_name: DeviceName | None = None
    """Optional: an old client names its device; otherwise the User-Agent does."""


class LinkLogin(BaseModel):
    """`POST /api/auth` with the one-time token of a localhost login link."""

    model_config = ConfigDict(extra="forbid")
    method: Literal["link"]
    token: Annotated[str, StringConstraints(max_length=200)]
    device_name: DeviceName | None = None
    """Optional: an old client names its device; otherwise the User-Agent does."""


class Fido2Login(BaseModel):
    """`POST /api/auth` with a security key's signature over a begun challenge."""

    model_config = ConfigDict(extra="forbid")
    method: Literal["fido2"]
    challenge_id: Annotated[str, StringConstraints(max_length=100)]
    credential: dict[str, Any]
    """The browser's `PublicKeyCredential` as JSON (base64url binary fields)."""
    device_name: DeviceName | None = None
    """Optional: an old client names its device; otherwise the User-Agent does."""


class Fido2Begin(BaseModel):
    challenge_id: str
    options: dict[str, Any]


Login = Annotated[PinLogin | TotpLogin | LinkLogin | Fido2Login, Field(discriminator="method")]


class TokenResponse(BaseModel):
    token: str


class SessionResponse(BaseModel):
    device_name: str
    created_at: datetime
    last_used_at: datetime


class SessionRename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_name: DeviceName


class MethodsResponse(BaseModel):
    methods: list[str]
    auth_disabled: bool = False
    """The server runs with `remote.web.auth.enabled: false`: the browser skips the login."""


@router.get("/auth/methods")
def auth_methods(ctx: Context) -> MethodsResponse:
    if not ctx.deps.auth_enabled:
        return MethodsResponse(methods=[], auth_disabled=True)
    return MethodsResponse(methods=list(auth_admin.enabled_methods(ctx.deps.engine)))


def _source(request: Request) -> str:
    return request.client.host if request.client is not None else ""


def _attempt(
    body: PinLogin | TotpLogin | LinkLogin | Fido2Login, ctx: Context, request: Request
) -> bool:
    if isinstance(body, Fido2Login):
        return ctx.fido2.login_complete(request_rp(request), body.challenge_id, body.credential)
    if isinstance(body, LinkLogin):
        return login_link.consume(ctx.deps.engine, body.token, clock=ctx.deps.clock)
    if isinstance(body, PinLogin):
        return auth_admin.pin_enabled(ctx.deps.engine) and ctx.deps.pin.attempt(body.pin)
    return ctx.totp.verify(body.code)


@router.post("/auth")
def authenticate(body: Login, ctx: Context, request: Request) -> TokenResponse:
    source = _source(request)
    device_name = body.device_name or derive_device_name(request.headers.get("user-agent"))

    def audit(event: auth_admin.AuthEventKind) -> None:
        auth_admin.record_event(
            ctx.deps.engine,
            event,
            clock=ctx.deps.clock,
            method=body.method,
            device_name=device_name,
            source=source,
        )

    limited = body.method not in auth_admin.UNLIMITED_METHODS
    wait = ctx.limiter.check(body.method) if limited else 0.0
    if wait > 0:
        # Not a database row: a flood of refused requests must not churn the audit table.
        auth_admin.log.warning(
            "web auth rate-limited method=%s device=%r source=%s",
            body.method,
            device_name,
            source or "-",
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts, try again later",
            headers={"Retry-After": str(math.ceil(wait))},
        )
    if not _attempt(body, ctx, request):
        audit("login_failed")
        backoff = ctx.limiter.failed(body.method) if limited else None
        if backoff is not None:
            # Once per lengthening of the wait, never per refused attempt.
            audit("backoff")
            auth_admin.log.warning(
                "web auth backoff method=%s next attempt in %ds", body.method, math.ceil(backoff)
            )
        detail = {
            "pin": "Invalid or expired PIN",
            "totp": "Invalid TOTP code",
            "link": "Invalid or expired login link",
            "fido2": "Security key not accepted",
        }[body.method]
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
    token = ctx.sessions.create(device_name)
    ctx.limiter.reset(body.method if limited else None)
    audit("login")
    return TokenResponse(token=token)


@router.post("/auth/fido2/begin")
def fido2_begin(ctx: Context, rp: RequestRp) -> Fido2Begin:
    """Start a security-key login; `POST /api/auth` with ``method: "fido2"`` completes it.

    Counts against no attempt limit: it proves nothing, and the pending
    challenges it creates are bounded (`fido2_auth.MAX_PENDING`).
    """
    try:
        challenge = ctx.fido2.login_begin(rp)
    except Fido2Error as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Fido2Begin(challenge_id=challenge.id, options=challenge.options)


@router.get("/session")
def current_session(authed: RequireSession) -> SessionResponse:
    s = authed.session
    return SessionResponse(
        device_name=s.device_name, created_at=s.created_at, last_used_at=s.last_used_at
    )


@router.put("/session")
def rename_session(
    body: SessionRename, authed: RequireSession, ctx: Context, request: Request
) -> SessionResponse:
    """Rename the caller's own session; the new name shows in the header and the CLI listing."""
    renamed = ctx.sessions.rename(authed.token, body.device_name)
    if renamed is None:
        # Login switched off: there is no stored session to name, so nothing
        # changes and nothing is audited.
        return SessionResponse(
            device_name=authed.session.device_name,
            created_at=authed.session.created_at,
            last_used_at=authed.session.last_used_at,
        )
    auth_admin.record_event(
        ctx.deps.engine,
        "rename",
        clock=ctx.deps.clock,
        device_name=f"{authed.session.device_name} \u2192 {renamed.device_name}",
        source=_source(request),
    )
    return SessionResponse(
        device_name=renamed.device_name,
        created_at=renamed.created_at,
        last_used_at=renamed.last_used_at,
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
