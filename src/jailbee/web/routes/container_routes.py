"""`/api/containers/{name}/sessions[/history]`: the sessions of one container.

Two requests, so the live sessions render at once however long the history is.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from jailbee.web.container_sessions import (
    ContainerSessionHistoryView,
    ContainerSessionListView,
    live_sessions,
    session_history,
)
from jailbee.web.deps import Context, RequireSession

router = APIRouter()


@router.get("/api/containers/{name}/sessions")
async def sessions_of_container(
    name: str, ctx: Context, _auth: RequireSession
) -> ContainerSessionListView:
    # An excluded repository's container answers exactly as an unknown one.
    snapshot = await ctx.snapshot()
    view = await run_in_threadpool(live_sessions, snapshot, name, ctx.scope, ctx.repo_sessions)
    if view is None:
        raise HTTPException(status_code=404, detail="Unknown container")
    return view


@router.get("/api/containers/{name}/sessions/history")
async def session_history_of_container(
    name: str,
    ctx: Context,
    _auth: RequireSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: str = "",
) -> ContainerSessionHistoryView:
    snapshot = await ctx.snapshot()
    view = await run_in_threadpool(
        session_history,
        snapshot,
        name,
        ctx.scope,
        ctx.deps.engine,
        ctx.repo_sessions,
        limit,
        offset,
        q,
    )
    if view is None:
        raise HTTPException(status_code=404, detail="Unknown container")
    return view
