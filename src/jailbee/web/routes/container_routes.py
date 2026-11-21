"""`/api/containers/{name}/sessions`: the sessions of one container."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from jailbee.web.container_sessions import ContainerSessionListView, container_sessions
from jailbee.web.deps import Context, RequireSession

router = APIRouter()


@router.get("/api/containers/{name}/sessions")
async def sessions_of_container(
    name: str, ctx: Context, _auth: RequireSession
) -> ContainerSessionListView:
    # An excluded repository's container answers exactly as an unknown one.
    view = await run_in_threadpool(
        container_sessions,
        ctx.hub.latest(),
        name,
        ctx.scope,
        ctx.deps.engine,
        ctx.repo_sessions,
    )
    if view is None:
        raise HTTPException(status_code=404, detail="Unknown container")
    return view
