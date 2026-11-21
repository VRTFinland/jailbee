"""The web server's on-demand state-service connection.

The server is a `StateClient` consumer like the terminal dashboard, but holds
the connection only while a browser is looking: `StateHub` opens it when the
first WebSocket attaches and closes it ``grace_seconds`` after the last one
detaches, so a page reload does not tear it down and respawn it.

`StateClient` reports from its reader thread; the hub hops those reports onto
the event loop and wakes every attached `Subscriber`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from jailbee.state_service.protocol import Snapshot

log = logging.getLogger(__name__)

DEFAULT_GRACE_SECONDS = 10.0


class SnapshotSource(Protocol):
    """What the hub needs of a `StateClient`."""

    def start(self) -> None: ...

    def latest(self) -> Snapshot | None: ...

    def status(self) -> str | None: ...

    def close(self) -> None: ...


SnapshotSourceFactory = Callable[[Callable[[], None]], SnapshotSource]
"""Builds an unstarted source; the argument is its ``on_update`` callback,
which may be called from any thread."""


def state_client_source(on_update: Callable[[], None]) -> SnapshotSource:
    """The real source: a host-global `StateClient` (no repo of its own)."""
    from jailbee.state_service.client import StateClient

    return StateClient(None, on_update=on_update)


class Subscriber:
    """One WebSocket's wake-up signal. Coalescing: many updates, one wake."""

    def __init__(self) -> None:
        self._changed = asyncio.Event()

    def notify(self) -> None:
        self._changed.set()

    async def wait(self) -> None:
        await self._changed.wait()
        self._changed.clear()


class StateHub:
    """Shares one snapshot source among the attached WebSockets.

    All methods but the source's callback run on the event loop.
    """

    def __init__(self, factory: SnapshotSourceFactory, *, grace_seconds: float) -> None:
        self._factory = factory
        self._grace = grace_seconds
        self._source: SnapshotSource | None = None
        self._subscribers: set[Subscriber] = set()
        self._closer: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def connected(self) -> bool:
        return self._source is not None

    def latest(self) -> Snapshot | None:
        return self._source.latest() if self._source is not None else None

    def status(self) -> str | None:
        return self._source.status() if self._source is not None else None

    def attach(self) -> Subscriber:
        if self._closer is not None:
            self._closer.cancel()
            self._closer = None
        if self._source is None:
            self._loop = asyncio.get_running_loop()
            source = self._factory(self._on_update)
            source.start()
            self._source = source
        subscriber = Subscriber()
        self._subscribers.add(subscriber)
        if self.latest() is not None or self.status() is not None:
            subscriber.notify()
        return subscriber

    def detach(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)
        if not self._subscribers and self._source is not None and self._closer is None:
            self._closer = asyncio.get_running_loop().create_task(self._close_after_grace())

    async def aclose(self) -> None:
        """Close the source now (server shutdown)."""
        if self._closer is not None:
            self._closer.cancel()
            self._closer = None
        await self._close_source()

    async def _close_after_grace(self) -> None:
        await asyncio.sleep(self._grace)
        self._closer = None
        if not self._subscribers:
            await self._close_source()

    async def _close_source(self) -> None:
        source, self._source = self._source, None
        if source is not None:
            # `StateClient.close` joins its reader thread: keep it off the loop.
            await asyncio.to_thread(source.close)

    def _on_update(self) -> None:
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):  # the loop is already closed
            loop.call_soon_threadsafe(self._fan_out)

    def _fan_out(self) -> None:
        for subscriber in self._subscribers:
            subscriber.notify()
