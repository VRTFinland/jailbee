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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee import __version__
from jailbee.state_service.paths import ensure_runtime_dir, lock_path, socket_path
from jailbee.state_service.protocol import (
    MAX_WIRE_LINE_BYTES,
    PROTOCOL,
    ActionEntry,
    ActionFinish,
    ActionOutput,
    ActionRejected,
    Active,
    Hello,
    ProtocolError,
    Refresh,
    RunAction,
    Shutdown,
    Snapshot,
    decode,
    encode,
)

if TYPE_CHECKING:
    from jailbee.dashboard.runner import Spawner
    from jailbee.incus import Incus
    from jailbee.state_service.gatherer import Gatherer
    from jailbee.state_service.service_probe import ServiceProbe

log = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 30.0
TICK_SECONDS = 0.1
# A client that cannot take a snapshot within this long is dropped, so one
# wedged dashboard never stalls the others.
DRAIN_TIMEOUT_SECONDS = 5.0
# Larger than a worst-case escaped output tail, but bounded per action recipient.
MAX_ACTION_BUFFER_BYTES = 1024 * 1024
# How long a starting server waits for the previous one to let go of the
# lifetime lock — the gap between that server unlinking its socket and exiting.
LOCK_WAIT_SECONDS = 2.0
# The longest settle window one Refresh may ask for.
MAX_SETTLE_SECONDS = 30.0


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
    dashboard_id: str | None = None
    active: bool = True


def cancel_action_question(prompt: object) -> None:
    """Cancel questions until service-side question routing is implemented."""
    return None


class StateServer:
    def __init__(
        self,
        gatherer: Gatherer,
        *,
        version: str = __version__,
        idle_timeout: float = IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        action_spawn: Spawner | None = None,
    ) -> None:
        self._action_spawn = action_spawn
        self._actions: dict[int, ActionEntry] = {}
        self._action_tasks: set[asyncio.Task[None]] = set()
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
        self._fast_until = 0.0
        self._stopping = False
        self._idle_since = clock()
        self._wake = asyncio.Event()

    async def serve(self, path: Path) -> None:
        """Serve on ``path`` until shut down or idle for ``idle_timeout``."""
        server = await asyncio.start_unix_server(
            self._handle, path=str(path), limit=MAX_WIRE_LINE_BYTES
        )
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
                fast=self._clock() < self._fast_until,
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
                client = _Client(
                    writer, Path(hello.cwd_root) if hello.cwd_root else None, hello.dashboard_id
                )
                self._clients.add(client)
                if client.dashboard_id is not None:
                    for entry in self._actions.values():
                        if entry.origin == client.dashboard_id:
                            if not self._send_action(client, entry):
                                return
                    await writer.drain()
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
                elif isinstance(message, RunAction):
                    self._start_action(client, message)
                elif isinstance(message, Active):
                    client.active = message.value
                elif isinstance(message, Refresh):
                    self._refresh = True
                    settle = min(max(message.settle, 0.0), MAX_SETTLE_SECONDS)
                    self._fast_until = max(self._fast_until, self._clock() + settle)
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

    def _start_action(self, client: _Client, request: RunAction) -> None:
        from jailbee.dashboard.runner import popen_spawn, run_action

        if not client.dashboard_id or request.origin != client.dashboard_id:
            self._send_action(client, ActionRejected(request.entry_id, "dashboard identity mismatch"))
            return
        if request.entry_id <= 0 or request.entry_id >= 2**63 or request.entry_id in self._actions:
            self._send_action(client, ActionRejected(request.entry_id, "invalid or duplicate action id"))
            return
        entry = ActionEntry(
            request.entry_id,
            request.argv,
            request.cwd,
            request.target,
            request.critical,
            request.origin,
            request.transport,
            request.repo_root,
            request.prefix,
        )
        self._actions[entry.entry_id] = entry
        self._send_action(client, entry)
        loop = asyncio.get_running_loop()

        def send_output(chunk: str) -> None:
            loop.call_soon_threadsafe(self._action_output, request.entry_id, chunk)

        def run() -> None:
            try:
                result = run_action(
                    request.argv,
                    cwd=Path(request.cwd),
                    transport=request.transport,
                    policy=None,
                    on_prompt=cancel_action_question,
                    spawn=self._action_spawn or popen_spawn,
                    on_output=send_output,
                    address_flags=request.address_flags,
                )
                finish = ActionFinish(
                    request.entry_id,
                    result.status,
                    result.refused,
                    result.error,
                    result.cancelled,
                    result.declined,
                )
            except Exception as exc:
                finish = ActionFinish(request.entry_id, error=str(exc))
            loop.call_soon_threadsafe(self._action_finish, finish)

        task = asyncio.create_task(asyncio.to_thread(run))
        self._action_tasks.add(task)
        task.add_done_callback(self._action_tasks.discard)

    def _action_output(self, entry_id: int, chunk: str) -> None:
        from jailbee.dashboard.runner import OUTPUT_DROPPED_MARKER, OUTPUT_KEEP_CHARS

        entry = self._actions[entry_id]
        output = entry.output + chunk
        if len(output) > OUTPUT_KEEP_CHARS:
            output = OUTPUT_DROPPED_MARKER + output[-OUTPUT_KEEP_CHARS:]
        self._actions[entry_id] = replace(entry, output=output)
        for recipient in list(self._clients):
            if recipient.dashboard_id == entry.origin:
                self._send_action(recipient, ActionOutput(entry_id, chunk))

    def _action_finish(self, finish: ActionFinish) -> None:
        self._actions[finish.entry_id] = replace(
            self._actions[finish.entry_id],
            finished=True,
            status=finish.status,
            refused=finish.refused,
            error=finish.error,
            cancelled=finish.cancelled,
            declined=finish.declined,
        )
        for recipient in list(self._clients):
            if recipient.dashboard_id == self._actions[finish.entry_id].origin:
                self._send_action(recipient, finish)

    def _send_action(
        self, client: _Client, message: ActionEntry | ActionOutput | ActionFinish | ActionRejected
    ) -> bool:
        transport = client.writer.transport
        if client not in self._clients or transport is None or transport.is_closing():
            self._drop(client)
            return False
        line = encode(message)
        if transport.get_write_buffer_size() + len(line) > MAX_ACTION_BUFFER_BYTES:
            log.info("dropping action client that cannot keep up")
            self._drop(client)
            return False
        try:
            client.writer.write(line)
        except (ConnectionError, OSError, RuntimeError):
            self._drop(client)
            return False
        return True

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
    from jailbee.dashboard.model import global_config_or_defaults
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
    probe: ServiceProbe | None = None
    try:
        # Only the lock holder may remove a socket: whatever is there is stale.
        sock.unlink(missing_ok=True)
        cadence = Cadence.from_config(global_config_or_defaults().dashboard.refresh)
        log.info("state service %s starting (%s)", __version__, cadence)
        from jailbee.db import get_engine
        from jailbee.pr_autofetch import PrHeadAutofetcher
        from jailbee.state_service.service_probe import ServiceProbe, live_fetch
        from jailbee.state_service.session_log import SessionRecorder

        recorder = SessionRecorder(get_engine())
        probe = ServiceProbe(live_fetch(incus))
        asyncio.run(
            StateServer(
                Gatherer(
                    incus,
                    cadence,
                    recorder=recorder,
                    pr_heads=PrHeadAutofetcher(),
                    services=probe,
                )
            ).serve(sock)
        )
        return 0
    finally:
        if probe is not None:
            probe.shutdown()
        sock.unlink(missing_ok=True)
        os.close(lock_fd)
