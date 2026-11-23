"""The web app factory. Everything the app touches comes in through `WebDeps`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from jailbee.web.context import WebContext, WebDeps
from jailbee.web.origin_guard import HostPolicy, OriginGuard
from jailbee.web.routes import auth_routes, container_routes, repo_routes, ws_routes
from jailbee.web.security_headers import SecurityHeaders

__all__ = ["WebDeps", "create_app"]


def create_app(deps: WebDeps) -> FastAPI:
    """A new app over ``deps``. Holds no state outside the returned object.

    Routes are mounted before the static app, which serves the built browser
    app at ``/`` when ``deps.static_dir`` is given. FastAPI's generated docs
    are off: nothing beyond the login endpoints is reachable unauthenticated.
    Every request passes `OriginGuard` (cross-site WebSocket and DNS rebinding)
    and every response gets `SecurityHeaders`.
    """
    ctx = WebContext(deps)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        ctx.sessions.purge_expired()
        warm_up = asyncio.create_task(ctx.start_warm_up())
        yield
        warm_up.cancel()
        ctx.stop_warm_up()
        ctx.follows.close()
        await ctx.hub.aclose()

    app = FastAPI(
        title="JailBee",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.web = ctx
    app.add_middleware(
        OriginGuard,
        policy=HostPolicy.of(
            auth_enabled=deps.auth_enabled,
            listen=deps.listen,
            allow_remote=deps.allow_unauthenticated_remote,
        ),
    )
    app.add_middleware(SecurityHeaders)
    app.include_router(auth_routes.router)
    app.include_router(ws_routes.router)
    app.include_router(repo_routes.router)
    app.include_router(container_routes.router)
    if deps.static_dir is not None:
        app.mount("/", StaticFiles(directory=deps.static_dir, html=True), name="static")
    return app
