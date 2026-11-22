"""`/api/ws`: the browser's live channel. Ported from codereader's `ws_routes.py`.

After the auth handshake (see `jailbee.web.protocol`) two loops run per
connection: one pushes the front page whenever the state hub wakes it, the
other reads client messages (transcript subscriptions, `ws_follow`) until the
browser goes away. Every send goes through one `_Outbox`, so the push loop
and live transcript appends never interleave on the socket.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from jailbee.web.auth import SessionInfo
from jailbee.web.context import WebContext
from jailbee.web.deps import anonymous_session, get_context
from jailbee.web.protocol import (
    CLOSE_BAD_MESSAGE,
    CLOSE_PROTOCOL_MISMATCH,
    CLOSE_UNAUTHORIZED,
    PROTOCOL_VERSION,
    ClientAuth,
    FrontPage,
    ProtocolError,
    ServerHello,
    Status,
    decode_client,
    encode,
)
from jailbee.web.state import Subscriber
from jailbee.web.view import front_page_view
from jailbee.web.ws_follow import ConnectionFollows

if TYPE_CHECKING:
    from jailbee.web.protocol import _Message

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
    if not isinstance(message, ClientAuth):
        await ws.close(code=CLOSE_BAD_MESSAGE, reason="Expected auth")
        return None
    if message.v != PROTOCOL_VERSION:
        await ws.close(code=CLOSE_PROTOCOL_MISMATCH, reason="Protocol version mismatch")
        return None
    if not ctx.deps.auth_enabled:
        return message.token, anonymous_session(ctx.deps.clock())
    session = await run_in_threadpool(ctx.sessions.validate, message.token)
    if session is None:
        await ws.close(code=CLOSE_UNAUTHORIZED, reason="Invalid token")
        return None
    return message.token, session


class _Outbox:
    """Serialises sends on one socket."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._lock = asyncio.Lock()

    async def send(self, message: _Message) -> None:
        async with self._lock:
            await self._ws.send_text(encode(message))

    async def send_quietly(self, message: _Message) -> None:
        """For background senders: a socket already gone is not an error."""
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await self.send(message)


Authorized = Callable[[], Awaitable[bool]]


def _session_check(ws: WebSocket, ctx: WebContext, token: str) -> Authorized:
    """Asked before every push and live append: False (and the socket closed
    with 4001) once the session is revoked from the CLI or logged out."""

    async def authorized() -> bool:
        if not ctx.deps.auth_enabled or await run_in_threadpool(ctx.sessions.is_active, token):
            return True
        with contextlib.suppress(WebSocketDisconnect, RuntimeError):
            await ws.close(code=CLOSE_UNAUTHORIZED, reason="Session revoked")
        return False

    return authorized


async def _push(
    ctx: WebContext, subscriber: Subscriber, authorized: Authorized, outbox: _Outbox
) -> None:
    """Send what changed on each wake-up; stop once the session is revoked."""
    last_seq: int | None = None
    last_status: str | None = None
    while True:
        await subscriber.wait()
        if not await authorized():
            return
        status = ctx.hub.status()
        if status != last_status:
            last_status = status
            await outbox.send(Status(message=status))
        snapshot = ctx.hub.latest()
        if snapshot is not None and snapshot.seq != last_seq:
            last_seq = snapshot.seq
            await outbox.send(
                FrontPage(view=front_page_view(snapshot, ctx.scope, ctx.deps.clock()))
            )


async def _receive(ws: WebSocket, follows: ConnectionFollows) -> None:
    """Dispatch client messages until the browser disconnects.

    A message that does not parse closes the socket; a repeated auth is ignored.
    """
    with contextlib.suppress(WebSocketDisconnect):
        while True:
            text = await ws.receive_text()
            try:
                message = decode_client(text)
            except ProtocolError:
                await ws.close(code=CLOSE_BAD_MESSAGE, reason="Bad message")
                return
            if not isinstance(message, ClientAuth):
                await follows.handle(message)


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
    outbox = _Outbox(ws)
    authorized = _session_check(ws, ctx, token)
    follows = ConnectionFollows(ctx, outbox.send_quietly, authorized)
    tasks = [
        asyncio.create_task(_push(ctx, subscriber, authorized, outbox)),
        asyncio.create_task(_receive(ws, follows)),
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
        follows.close()
        ctx.hub.detach(subscriber)
