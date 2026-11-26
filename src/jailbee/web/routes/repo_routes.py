"""`/api/repos/{prefix}/sessions`: a repository's sessions, and one session."""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from jailbee.web import auth_admin, chat
from jailbee.web.container_sessions import live_session_containers, session_containers
from jailbee.web.deps import Context, RequireSession
from jailbee.web.protocol import (
    ChatAvailability,
    EntryContent,
    MetaMode,
    SessionListView,
    SessionView,
    TranscriptView,
)
from jailbee.web.repo_sessions import SessionTranscript
from jailbee.web.transcript_render import entry_content, render_entries

router = APIRouter()


@router.get("/api/repos/{prefix}/sessions")
async def repo_sessions(prefix: str, ctx: Context, _auth: RequireSession) -> SessionListView:
    # An excluded repository answers exactly as an unknown one does, and its
    # homes are never resolved.
    if ctx.scope.allows(prefix):
        await ctx.homes_ready()
    rows = (
        await run_in_threadpool(ctx.repo_sessions.list, prefix)
        if ctx.scope.allows(prefix)
        else None
    )
    if rows is None:
        raise HTTPException(status_code=404, detail="Unknown repository")
    snapshot = ctx.hub.latest()
    containers = await run_in_threadpool(
        session_containers, snapshot, prefix, ctx.deps.engine, rows
    )
    live = live_session_containers(snapshot, prefix)
    listed = {(r.agent, r.session_id) for r in rows}
    return SessionListView(
        # A just-started session has no transcript timestamp yet; keep it ahead
        # of recorded sessions rather than treating it as the oldest one.
        sessions=[
            SessionView(
                session_id=sid,
                agent=agent,
                title="",
                last_activity=None,
                models=[],
                container=owner,
                live=True,
            )
            for (agent, sid), owner in live.items()
            if (agent, sid) not in listed
        ]
        + [
            SessionView(
                session_id=r.session_id,
                agent=r.agent,
                title=r.title,
                last_activity=r.last_activity,
                models=list(r.models),
                container=live.get(
                    (r.agent, r.session_id), containers.get((r.agent, r.session_id))
                ),
                live=(r.agent, r.session_id) in live,
            )
            for r in rows
        ]
    )


async def _transcript_view(
    ctx: Context,
    prefix: str,
    load: Callable[[], SessionTranscript | None],
    meta: MetaMode = "hidden",
    *,
    subagent: bool = False,
) -> TranscriptView:
    # Unknown and excluded repositories, agents, sessions and subagents all
    # answer the same 404, so the answer reveals nothing about what exists.
    if not ctx.scope.allows(prefix):
        raise HTTPException(status_code=404, detail="Unknown session")
    await ctx.homes_ready()
    if ctx.deps.agent_chat:
        await ctx.snapshot()

    def build() -> TranscriptView | None:
        found = load()
        if found is None:
            return None
        return TranscriptView(
            session_id=found.info.session_id,
            agent=found.agent,
            title=found.info.title,
            last_activity=found.info.last_activity,
            models=list(found.info.models),
            entries=render_entries(found.entries, ctx.render_cache, meta),
            offset=found.offset,
            chat=(
                ChatAvailability(
                    available=False, code="unsupported_agent", message="Subagent chat unavailable"
                )
                if subagent
                else chat.availability(ctx, prefix, found.agent, found.info.session_id)
            ),
        )

    view = await run_in_threadpool(build)
    if view is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    return view


@router.get("/api/repos/{prefix}/sessions/{agent}/{session_id}")
async def session_transcript(
    prefix: str,
    agent: str,
    session_id: str,
    ctx: Context,
    _auth: RequireSession,
    meta: MetaMode = "hidden",
) -> TranscriptView:
    return await _transcript_view(
        ctx, prefix, lambda: ctx.repo_sessions.transcript(prefix, agent, session_id), meta
    )


class MessageRequest(BaseModel):
    text: str = Field(max_length=65536)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message must not be blank")
        return value


class MessageResponse(BaseModel):
    code: str
    message: str


@router.post("/api/repos/{prefix}/sessions/{agent}/{session_id}/messages")
async def send_message(
    prefix: str,
    agent: str,
    session_id: str,
    body: MessageRequest,
    ctx: Context,
    auth: RequireSession,
) -> MessageResponse:
    if not ctx.scope.allows(prefix):
        raise HTTPException(status_code=404, detail="Unknown session")
    await ctx.homes_ready()
    if ctx.deps.agent_chat:
        await ctx.snapshot()
    found = await run_in_threadpool(ctx.repo_sessions.transcript, prefix, agent, session_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    outcome = await run_in_threadpool(chat.send, ctx, prefix, agent, session_id, body.text)
    await run_in_threadpool(
        auth_admin.record_event,
        ctx.deps.engine,
        "agent_message",
        clock=ctx.deps.clock,
        method=outcome.code,
        device_name=auth.session.device_name,
        source=json.dumps(
            {"prefix": prefix, "agent": agent, "session_id": session_id, "text": body.text}
        ),
    )
    return MessageResponse(code=outcome.code, message=outcome.message)


@router.get("/api/repos/{prefix}/sessions/{agent}/{session_id}/summary")
async def session_summary(
    prefix: str, agent: str, session_id: str, ctx: Context, _auth: RequireSession
) -> SessionView:
    if not ctx.scope.allows(prefix):
        raise HTTPException(status_code=404, detail="Unknown session")
    await ctx.homes_ready()
    rows = await run_in_threadpool(ctx.repo_sessions.rows, prefix, [(agent, session_id)])
    row = rows.get((agent, session_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    containers = await run_in_threadpool(
        session_containers, ctx.hub.latest(), prefix, ctx.deps.engine, [row]
    )
    return SessionView(
        session_id=row.session_id,
        agent=row.agent,
        title=row.title,
        last_activity=row.last_activity,
        models=list(row.models),
        container=containers.get((agent, session_id)),
    )


@router.get("/api/repos/{prefix}/sessions/{agent}/{session_id}/subagents/{agent_id}")
async def subagent_transcript(
    prefix: str,
    agent: str,
    session_id: str,
    agent_id: str,
    ctx: Context,
    _auth: RequireSession,
    meta: MetaMode = "hidden",
) -> TranscriptView:
    return await _transcript_view(
        ctx,
        prefix,
        lambda: ctx.repo_sessions.subagent_transcript(prefix, agent, session_id, agent_id),
        meta,
        subagent=True,
    )


async def _entry_content(
    ctx: Context,
    prefix: str,
    load: Callable[[], SessionTranscript | None],
    entry_id: str,
) -> EntryContent:
    # The same 404 for an excluded repository, an unknown transcript and an
    # entry that carries no agent context. Only the requested entry is rendered.
    if not ctx.scope.allows(prefix):
        raise HTTPException(status_code=404, detail="Unknown session")
    await ctx.homes_ready()

    def build() -> str | None:
        found = load()
        return None if found is None else entry_content(found.entries, entry_id)

    html = await run_in_threadpool(build)
    if html is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    return EntryContent(id=entry_id, html=html)


@router.get("/api/repos/{prefix}/sessions/{agent}/{session_id}/entries/{entry_id}")
async def session_entry(
    prefix: str, agent: str, session_id: str, entry_id: str, ctx: Context, _auth: RequireSession
) -> EntryContent:
    return await _entry_content(
        ctx, prefix, lambda: ctx.repo_sessions.transcript(prefix, agent, session_id), entry_id
    )


@router.get(
    "/api/repos/{prefix}/sessions/{agent}/{session_id}/subagents/{agent_id}/entries/{entry_id}"
)
async def subagent_entry(
    prefix: str,
    agent: str,
    session_id: str,
    agent_id: str,
    entry_id: str,
    ctx: Context,
    _auth: RequireSession,
) -> EntryContent:
    return await _entry_content(
        ctx,
        prefix,
        lambda: ctx.repo_sessions.subagent_transcript(prefix, agent, session_id, agent_id),
        entry_id,
    )
