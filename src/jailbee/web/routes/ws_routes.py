"""`/api/ws`: the browser's live channel. Ported from codereader's `ws_routes.py`.

After the auth handshake (see `jailbee.web.protocol`) two loops run per
connection: one pushes the front page whenever the state hub wakes it, the
other reads client messages until the browser goes away.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from jailbee.web.auth import SessionInfo
from jailbee.web.context import WebContext
from jailbee.web.deps import get_context
from jailbee.web.protocol import (
    CLOSE_BAD_MESSAGE,
    CLOSE_PROTOCOL_MISMATCH,
    CLOSE_UNAUTHORIZED,
    PROTOCOL_VERSION,
    FrontPage,
    ProtocolError,
    ServerHello,
    Status,
    decode_client,
    encode,
)
from jailbee.web.state import Subscriber
from jailbee.web.view import front_page_view

router = APIRouter()

AUTH_TIMEOUT_SECONDS = 10.0


async def _authenticate(ws: WebSocket, ctx: WebContext) -> tuple[str, SessionInfo] | None:
    """Read the first message; close the socket and return None unless it logs in."""
    try:
        first = await asyncio.wait_for(ws.receive_text(), AUTH_TIMEOUT_SECONDS)
    except (WebSocketDisconnect, TimeoutError):
        return None
    try:
        message = decode_client(first)
    except ProtocolError:
        await ws.close(code=CLOSE_BAD_MESSAGE, reason="Bad message")
        return None
    if message.v != PROTOCOL_VERSION:
        await ws.close(code=CLOSE_PROTOCOL_MISMATCH, reason="Protocol version mismatch")
        return None
    session = await run_in_threadpool(ctx.sessions.validate, message.token)
    if session is None:
        await ws.close(code=CLOSE_UNAUTHORIZED, reason="Invalid token")
        return None
    return message.token, session


async def _push(ws: WebSocket, ctx: WebContext, subscriber: Subscriber, token: str) -> None:
    """Send what changed on each wake-up; close instead once the session is revoked."""
    last_seq: int | None = None
    last_status: str | None = None
    while True:
        await subscriber.wait()
        if not await run_in_threadpool(ctx.sessions.is_active, token):
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="Session revoked")
            return
        status = ctx.hub.status()
        if status != last_status:
            last_status = status
            await ws.send_text(encode(Status(message=status)))
        snapshot = ctx.hub.latest()
        if snapshot is not None and snapshot.seq != last_seq:
            last_seq = snapshot.seq
            await ws.send_text(
                encode(FrontPage(view=front_page_view(snapshot, ctx.scope, ctx.deps.clock())))
            )


async def _receive(ws: WebSocket) -> None:
    """Drain client messages until the browser disconnects.

    Nothing is expected after the handshake yet; later slices dispatch
    transcript subscriptions here.
    """
    with contextlib.suppress(WebSocketDisconnect):
        while True:
            await ws.receive_text()


@router.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    ctx = get_context(ws)
    await ws.accept()
    authed = await _authenticate(ws, ctx)
    if authed is None:
        return
    token, session = authed
    await ws.send_text(encode(ServerHello(device_name=session.device_name)))
    subscriber = ctx.hub.attach()
    tasks = [
        asyncio.create_task(_push(ws, ctx, subscriber, token)),
        asyncio.create_task(_receive(ws)),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                await task
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, RuntimeError):
                task.result()  # a send to a closed socket ends the push loop
    finally:
        ctx.hub.detach(subscriber)
