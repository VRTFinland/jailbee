"""Live transcript follow: one file watch per followed transcript, one read position per browser.

A transcript file is watched only while at least one `Follower` follows it;
the watch closes when the last one leaves. Every follower keeps its own byte
offset (the one its browser's REST view was read to), so a browser that
subscribes late, or after a reconnect, catches up from where it stands. Reads
use the source's incremental `read_from`, which never consumes a partly
written line, and an empty read sends nothing.

Everything here runs on the event loop except the watch callback, which hops
onto the loop, and the read + render, which run in a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.transcripts.base import Entry, TranscriptSource
    from jailbee.web.file_watch import FileWatch, WatchFactory
    from jailbee.web.protocol import FollowKey, RenderedEntry

Render = Callable[[Sequence["Entry"]], list["RenderedEntry"]]
Deliver = Callable[[list["RenderedEntry"], int], Awaitable[None]]
"""Sends one append: the rendered entries and the offset after them."""


class Follower:
    """One browser following one transcript. Created by `TranscriptFollows.follow`."""

    def __init__(
        self,
        key: FollowKey,
        source: TranscriptSource,
        path: Path,
        offset: int,
        deliver: Deliver,
        render: Render,
    ) -> None:
        self.key = key
        self.path = path
        self.offset = offset
        self._source = source
        self._deliver = deliver
        self._render = render
        self._task: asyncio.Task[None] | None = None
        self._again = False

    def wake(self) -> None:
        """Read what is new; coalesces wake-ups that arrive during a read."""
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = asyncio.get_running_loop().create_task(self._pump())

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _pump(self) -> None:
        while True:
            self._again = False
            entries, offset = await asyncio.to_thread(self._read)
            self.offset = offset
            if entries:
                await self._deliver(entries, offset)
            if not self._again:
                return

    def _read(self) -> tuple[list[RenderedEntry], int]:
        read = self._source.read_from(self.path, self.offset)
        return (self._render(read.entries) if read.entries else []), read.offset


class _Watched:
    def __init__(self, watch: FileWatch) -> None:
        self.watch = watch
        self.followers: set[Follower] = set()


class TranscriptFollows:
    """The web app's followed transcripts, keyed by file."""

    def __init__(self, watch: WatchFactory, render: Render) -> None:
        self._watch = watch
        self._render = render
        self._files: dict[Path, _Watched] = {}

    def watching(self, path: Path) -> bool:
        return path in self._files

    def follow(
        self, key: FollowKey, source: TranscriptSource, path: Path, offset: int, deliver: Deliver
    ) -> Follower:
        """Start following; catches up from ``offset`` at once."""
        follower = Follower(key, source, path, offset, deliver, self._render)
        watched = self._files.get(path)
        if watched is None:
            loop = asyncio.get_running_loop()

            def changed() -> None:
                with contextlib.suppress(RuntimeError):  # the loop is already closed
                    loop.call_soon_threadsafe(self._changed, path)

            watched = _Watched(self._watch(path, changed))
            self._files[path] = watched
        watched.followers.add(follower)
        follower.wake()
        return follower

    def unfollow(self, follower: Follower) -> None:
        follower.cancel()
        watched = self._files.get(follower.path)
        if watched is None:
            return
        watched.followers.discard(follower)
        if not watched.followers:
            del self._files[follower.path]
            watched.watch.close()

    def close(self) -> None:
        """Stop every watch (server shutdown)."""
        files, self._files = self._files, {}
        for watched in files.values():
            for follower in watched.followers:
                follower.cancel()
            watched.watch.close()

    def _changed(self, path: Path) -> None:
        watched = self._files.get(path)
        if watched is None:
            return  # a late event from a watch already closed
        for follower in list(watched.followers):
            follower.wake()
