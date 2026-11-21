"""`/api/repos/{prefix}/sessions`: a repository's sessions, and one session."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from jailbee.web.container_sessions import session_containers
from jailbee.web.deps import Context, RequireSession
from jailbee.web.protocol import SessionListView, SessionView, TranscriptView
from jailbee.web.transcript_render import render_entries

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


@router.get("/api/repos/{prefix}/sessions/{agent}/{session_id}")
async def session_transcript(
    prefix: str, agent: str, session_id: str, ctx: Context, _auth: RequireSession
) -> TranscriptView:
    # Unknown and excluded repositories, agents and sessions all answer the
    # same 404, so the answer reveals nothing about what exists.
    if not ctx.scope.allows(prefix):
        raise HTTPException(status_code=404, detail="Unknown session")

    def load() -> TranscriptView | None:
        found = ctx.repo_sessions.transcript(prefix, agent, session_id)
        if found is None:
            return None
        return TranscriptView(
            session_id=found.info.session_id,
            agent=found.agent,
            title=found.info.title,
            last_activity=found.info.last_activity,
            models=list(found.info.models),
            entries=render_entries(found.entries, ctx.render_cache),
        )

    view = await run_in_threadpool(load)
    if view is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    return view
