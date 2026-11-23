"""One WebSocket's transcript subscriptions: validate, follow, forget.

A subscription is validated exactly as the REST transcript endpoints
validate (repo scope, the repository's own agents, the source's id checks),
and a refusal is one `SubscribeError` with the same text for every reason.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool

from jailbee.web.protocol import (
    FollowKey,
    MetaMode,
    RenderedEntry,
    Subscribe,
    SubscribeError,
    TranscriptAppend,
    Unsubscribe,
)

if TYPE_CHECKING:
    from jailbee.web.context import WebContext
    from jailbee.web.protocol import _Message
    from jailbee.web.transcript_follow import Follower

Send = Callable[["_Message"], Awaitable[None]]
Authorized = Callable[[], Awaitable[bool]]
"""Whether the connection's session is still live; closes the socket when not."""

UNKNOWN_SESSION = "Unknown session"
MAX_SUBSCRIPTIONS = 32
"""Followed transcripts per socket; each holds a file watch and a read position."""
TOO_MANY_SUBSCRIPTIONS = "Too many subscriptions"


class ConnectionFollows:
    """The subscriptions of one connection; `close` when the socket ends."""

    def __init__(self, ctx: WebContext, send: Send, authorized: Authorized) -> None:
        self._ctx = ctx
        self._send = send
        self._authorized = authorized
        self._followers: dict[FollowKey, Follower] = {}

    async def handle(self, message: Subscribe | Unsubscribe) -> None:
        if isinstance(message, Unsubscribe):
            self._drop(message.key)
            return
        await self._subscribe(message.key, message.offset, message.meta)

    def close(self) -> None:
        for key in list(self._followers):
            self._drop(key)

    async def _subscribe(self, key: FollowKey, offset: int, meta: MetaMode) -> None:
        self._drop(key)
        if len(self._followers) >= MAX_SUBSCRIPTIONS:
            await self._send(SubscribeError(key=key, message=TOO_MANY_SUBSCRIPTIONS))
            return
        ctx = self._ctx
        found = None
        if ctx.scope.allows(key.prefix):
            await ctx.homes_ready()
            found = await run_in_threadpool(
                ctx.repo_sessions.transcript_file,
                key.prefix,
                key.agent,
                key.session_id,
                key.subagent_id,
            )
        if found is None:
            await self._send(SubscribeError(key=key, message=UNKNOWN_SESSION))
            return
        source, path = found

        async def deliver(entries: list[RenderedEntry], next_offset: int) -> None:
            if not await self._authorized():
                return  # revoked: the socket is closed, nothing more is sent
            await self._send(TranscriptAppend(key=key, entries=entries, offset=next_offset))

        self._followers[key] = ctx.follows.follow(key, source, path, offset, deliver, meta)

    def _drop(self, key: FollowKey) -> None:
        follower = self._followers.pop(key, None)
        if follower is not None:
            self._ctx.follows.unfollow(follower)
