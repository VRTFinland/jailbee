"""The setup-mode app: registers FIDO2 keys, and nothing else.

Ported from codereader's `setup_app.py` and `routes/setup_routes.py`, cut
down to the one thing the CLI cannot do itself: a WebAuthn registration
needs a browser at the dashboard's public origin. `jailbee remote web auth
fido2 setup` serves this app instead of the dashboard, on the dashboard's
own address by default, so a reverse proxy sends the browser here at the same
domain the keys will later log in at. The normal app (`create_app`) mounts
none of these routes.

Every registration is authorised by the setup PIN the command prints (spent
on each attempt, like the login PIN). The app serves the built browser app;
``/`` redirects to its setup view.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StringConstraints
from starlette.requests import HTTPConnection

from jailbee.web import auth_admin
from jailbee.web.auth import Clock, PinHolder, RateLimiter, utcnow
from jailbee.web.deps import RequestRp
from jailbee.web.fido2_auth import Fido2Auth, Fido2Error

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

SETUP_VIEW = "index.html#/setup"
"""Where ``/`` sends the browser: the setup view of the built app (relative,
so it works at a domain root and under a proxy path alike)."""


@dataclass(frozen=True)
class SetupDeps:
    engine: Engine
    pin: PinHolder
    clock: Clock = utcnow
    monotonic: Callable[[], float] = time.monotonic
    static_dir: Path | None = None
    fido2: Fido2Auth | None = None
    """The ceremony runner; None builds one over ``engine``."""


@dataclass
class SetupContext:
    deps: SetupDeps
    fido2: Fido2Auth = field(init=False)
    limiter: RateLimiter = field(init=False)

    def __post_init__(self) -> None:
        self.fido2 = self.deps.fido2 or Fido2Auth(
            self.deps.engine, clock=self.deps.clock, monotonic=self.deps.monotonic
        )
        self.limiter = RateLimiter(self.deps.monotonic)


def _context(conn: HTTPConnection) -> SetupContext:
    return cast(SetupContext, conn.app.state.setup)


Context = Annotated[SetupContext, Depends(_context)]

KeyName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]


class BeginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pin: str


class BeginResponse(BaseModel):
    challenge_id: str
    options: dict[str, Any]


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: str
    credential: dict[str, Any]
    name: KeyName = ""


class KeyResponse(BaseModel):
    id: int
    name: str


router = APIRouter(prefix="/api", tags=["setup"])


@router.get("/mode")
def mode() -> dict[str, str]:
    return {"mode": "setup"}


def _source(request: Request) -> str:
    return request.client.host if request.client is not None else ""


@router.post("/setup/fido2/begin")
def register_begin(
    body: BeginRequest, ctx: Context, rp: RequestRp, request: Request
) -> BeginResponse:
    if not ctx.limiter.allow():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts, try again later",
        )
    if not ctx.deps.pin.attempt(body.pin):
        auth_admin.record_event(
            ctx.deps.engine,
            "login_failed",
            clock=ctx.deps.clock,
            method="setup-pin",
            source=_source(request),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or used setup PIN"
        )
    challenge = ctx.fido2.register_begin(rp)
    return BeginResponse(challenge_id=challenge.id, options=challenge.options)


@router.post("/setup/fido2/complete")
def register_complete(
    body: CompleteRequest, ctx: Context, rp: RequestRp, request: Request
) -> KeyResponse:
    try:
        key = ctx.fido2.register_complete(rp, body.challenge_id, body.credential, body.name)
    except Fido2Error as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    auth_admin.record_event(
        ctx.deps.engine,
        "key_added",
        clock=ctx.deps.clock,
        method="fido2",
        device_name=key.name,
        source=_source(request),
    )
    return KeyResponse(id=key.id, name=key.name)


def create_setup_app(deps: SetupDeps) -> FastAPI:
    """A new setup-mode app over ``deps``. Holds no state outside the returned object."""
    app = FastAPI(title="JailBee setup", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.setup = SetupContext(deps)
    app.include_router(router)
    if deps.static_dir is not None:

        @app.get("/", include_in_schema=False)
        def root() -> RedirectResponse:
            return RedirectResponse(SETUP_VIEW)

        app.mount("/", StaticFiles(directory=deps.static_dir, html=True), name="static")
    return app
