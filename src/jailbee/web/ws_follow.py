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

UNKNOWN_SESSION = "Unknown session"


class ConnectionFollows:
    """The subscriptions of one connection; `close` when the socket ends."""

    def __init__(self, ctx: WebContext, send: Send) -> None:
        self._ctx = ctx
        self._send = send
        self._followers: dict[FollowKey, Follower] = {}

    async def handle(self, message: Subscribe | Unsubscribe) -> None:
        if isinstance(message, Unsubscribe):
            self._drop(message.key)
            return
        await self._subscribe(message.key, message.offset)

    def close(self) -> None:
        for key in list(self._followers):
            self._drop(key)

    async def _subscribe(self, key: FollowKey, offset: int) -> None:
        self._drop(key)
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
            await self._send(TranscriptAppend(key=key, entries=entries, offset=next_offset))

        self._followers[key] = ctx.follows.follow(key, source, path, offset, deliver)

    def _drop(self, key: FollowKey) -> None:
        follower = self._followers.pop(key, None)
        if follower is not None:
            self._ctx.follows.unfollow(follower)
