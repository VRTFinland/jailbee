"""The state service process: gather on one schedule, push to every dashboard.

Run as `jailbee _state-service`, spawned detached by the first dashboard that
finds no server (`client.StateClient`). One per host user, enforced by a
lifetime `flock`; exits once no client has been connected for
``IDLE_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee import __version__
from jailbee.state_service.paths import ensure_runtime_dir, lock_path, socket_path
from jailbee.state_service.protocol import (
    PROTOCOL,
    Active,
    Hello,
    ProtocolError,
    Refresh,
    Shutdown,
    Snapshot,
    decode,
    encode,
)

if TYPE_CHECKING:
    from jailbee.incus import Incus
    from jailbee.state_service.gatherer import Gatherer

log = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 30.0
TICK_SECONDS = 0.1
# A client that cannot take a snapshot within this long is dropped, so one
# wedged dashboard never stalls the others.
DRAIN_TIMEOUT_SECONDS = 5.0
# How long a starting server waits for the previous one to let go of the
# lifetime lock — the gap between that server unlinking its socket and exiting.
LOCK_WAIT_SECONDS = 2.0


def _abort(writer: asyncio.StreamWriter) -> None:
    """Drop a connection now, without flushing.

    `close()` only completes once the write buffer is empty, so a client that
    stopped reading would stay open and block `Server.wait_closed()` forever.
    """
    transport = writer.transport
    if transport is not None and not transport.is_closing():
        transport.abort()


@dataclass(eq=False)
class _Client:
    writer: asyncio.StreamWriter
    cwd_root: Path | None
    active: bool = True


class StateServer:
    def __init__(
        self,
        gatherer: Gatherer,
        *,
        version: str = __version__,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gatherer = gatherer
        self._version = version
        self._idle_timeout = idle_timeout
        self._clock = clock
        self._clients: set[_Client] = set()
        # Every open connection, registered or not: a client of another
        # version is never in `_clients` but must still be closed on exit.
        self._writers: set[asyncio.StreamWriter] = set()
        self._latest: Snapshot | None = None
        self._refresh = False
        self._stopping = False
        self._idle_since = clock()
        self._wake = asyncio.Event()

    async def serve(self, path: Path) -> None:
        """Serve on ``path`` until shut down or idle for ``idle_timeout``."""
        server = await asyncio.start_unix_server(self._handle, path=str(path))
        os.chmod(path, 0o600)
        try:
            await self._schedule()
        finally:
            server.close()
            for client in list(self._clients):
                self._drop(client)
            # `wait_closed` blocks on open connections (3.12+), so close them all.
            for writer in list(self._writers):
                _abort(writer)
            await server.wait_closed()

    async def _schedule(self) -> None:
        while not self._stopping:
            if not self._clients and self._clock() - self._idle_since >= self._idle_timeout:
                log.info("no clients for %.0fs; exiting", self._idle_timeout)
                return
            refresh, self._refresh = self._refresh, False
            # Cleared before the tick, not after: a Refresh or Shutdown that
            # arrives mid-tick must cut the next wait short, not be erased.
            self._wake.clear()
            result = await asyncio.to_thread(
                self._gatherer.tick,
                active=any(c.active for c in self._clients),
                refresh=refresh,
                roots=sorted({c.cwd_root for c in self._clients if c.cwd_root is not None}),
            )
            if result is not None:
                if isinstance(result, Snapshot):
                    self._latest = result
                await self._broadcast(encode(result))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), TICK_SECONDS)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client: _Client | None = None
        self._writers.add(writer)
        try:
            hello = decode(await reader.readline())
            if not isinstance(hello, Hello):
                raise ProtocolError(f"expected hello, got {type(hello).__name__}")
            writer.write(encode(Hello(PROTOCOL, self._version)))
            await writer.drain()
            # A client of another version only gets to ask us to go away.
            if hello.protocol == PROTOCOL and hello.version == self._version:
                client = _Client(writer, Path(hello.cwd_root) if hello.cwd_root else None)
                self._clients.add(client)
                if self._latest is not None:
                    writer.write(encode(self._latest))
                    await writer.drain()
            while line := await reader.readline():
                message = decode(line)
                if isinstance(message, Shutdown):
                    log.info("shutdown requested by a client")
                    self._stopping = True
                elif client is None:
                    continue
                elif isinstance(message, Active):
                    client.active = message.value
                elif isinstance(message, Refresh):
                    self._refresh = True
                self._wake.set()
        except (ProtocolError, ConnectionError, ValueError) as exc:
            # ValueError: a line over the reader's limit (LimitOverrunError's base).
            log.info("dropping a client: %s", exc)
        finally:
            self._writers.discard(writer)
            if client is not None:
                self._drop(client)
            else:
                writer.close()

    async def _broadcast(self, line: bytes) -> None:
        clients = list(self._clients)
        for client in clients:
            client.writer.write(line)
        for client in clients:
            try:
                await asyncio.wait_for(client.writer.drain(), DRAIN_TIMEOUT_SECONDS)
            except (TimeoutError, ConnectionError) as exc:
                log.info("dropping a client that cannot keep up: %s", exc)
                self._drop(client)

    def _drop(self, client: _Client) -> None:
        if client in self._clients:
            self._clients.discard(client)
            if not self._clients:
                self._idle_since = self._clock()
        _abort(client.writer)


def _take_lifetime_lock(path: Path) -> int | None:
    """An fd holding the exclusive lock on ``path``, or None if another server has it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


def run_service(incus: Incus) -> int:
    """Body of `jailbee _state-service`. Exits 0 when another server is running."""
    from jailbee.dashboard import global_config_or_defaults
    from jailbee.state_service.gatherer import Cadence, Gatherer

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    ensure_runtime_dir()
    lock_fd = _take_lifetime_lock(lock_path())
    if lock_fd is None:
        log.info("another state service holds the lock; exiting")
        return 0
    sock = socket_path()
    try:
        # Only the lock holder may remove a socket: whatever is there is stale.
        sock.unlink(missing_ok=True)
        cadence = Cadence.from_config(global_config_or_defaults().dashboard.refresh)
        log.info("state service %s starting (%s)", __version__, cadence)
        asyncio.run(StateServer(Gatherer(incus, cadence)).serve(sock))
        return 0
    finally:
        sock.unlink(missing_ok=True)
        os.close(lock_fd)
