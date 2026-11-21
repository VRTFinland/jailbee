"""`/api/repos/{prefix}/sessions`: the session list of one repository."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from jailbee.web.container_sessions import session_containers
from jailbee.web.deps import Context, RequireSession
from jailbee.web.protocol import SessionListView, SessionView

router = APIRouter()


@router.get("/api/repos/{prefix}/sessions")
async def repo_sessions(prefix: str, ctx: Context, _auth: RequireSession) -> SessionListView:
    # An excluded repository answers exactly as an unknown one does, and its
    # homes are never resolved.
    rows = (
        await run_in_threadpool(ctx.repo_sessions.list, prefix)
        if ctx.scope.allows(prefix)
        else None
    )
    if rows is None:
        raise HTTPException(status_code=404, detail="Unknown repository")
    containers = await run_in_threadpool(
        session_containers, ctx.hub.latest(), prefix, ctx.deps.engine
    )
    return SessionListView(
        sessions=[
            SessionView(
                session_id=r.session_id,
                agent=r.agent,
                title=r.title,
                last_activity=r.last_activity,
                models=list(r.models),
                container=containers.get((r.agent, r.session_id)),
            )
            for r in rows
        ]
    )
