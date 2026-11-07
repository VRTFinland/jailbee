"""Container-side client for the host broker socket.

Standard library plus ``jailbee.orchestration.wire`` only, and no subprocess:
this module is imported by the inbox commands inside a container.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from jailbee.orchestration import wire

DEFAULT_SOCKET = "/opt/jailbee/orchestration/broker.sock"
SOCKET_ENV = "JAILBEE_ORCHESTRATION_SOCKET"
UNREACHABLE_MESSAGE = (
    "jailbee inbox: the host broker is not reachable; "
    "if this persists, tell the human (`jailbee doctor` on the host)"
)
VERSION_MESSAGE = (
    "jailbee inbox: container and broker versions differ; run `jb apply` / restart the broker"
)
EXIT_UNREACHABLE = 4
NO_REPLY_MESSAGE = (
    "jailbee inbox: no reply from the host broker yet; the request (id printed above) "
    "may or may not have run. Inspect `jailbee inbox ls` and `jailbee inbox status` "
    "before resending with a new id"
)

_BACKOFF_START = 0.5
_BACKOFF_CAP = 5.0
_RETRY_PAUSE = 0.25
_RETRY_PAUSE_CAP = 1.0
CONNECT_RETRY_SECONDS = 10.0  # covers a broker restart
RETRY_ENV = "JAILBEE_BROKER_RETRY_SECONDS"  # overrides it, for tests and operators
_READ_GRACE = 5.0  # socket read slack beyond the server-side long-poll timeout
_UNREACHABLE_LIMIT = 120.0
_KEEPALIVE_TIMEOUT = wire.KEEPALIVE_INTERVAL + 10.0


def retry_seconds() -> float:
    """The connect retry window: `CONNECT_RETRY_SECONDS`, or `RETRY_ENV` when set."""
    try:
        return max(0.0, float(os.environ.get(RETRY_ENV, CONNECT_RETRY_SECONDS)))
    except ValueError:
        return CONNECT_RETRY_SECONDS


class BrokerUnreachable(Exception):  # noqa: N818 - name fixed by the spec
    """The broker cannot be reached; ``str(exc)`` is the stderr message."""

    def __init__(self, message: str = UNREACHABLE_MESSAGE) -> None:
        super().__init__(message)


class BrokerVersionMismatch(BrokerUnreachable):
    """Protocol version differs; retrying cannot help."""

    def __init__(self) -> None:
        super().__init__(VERSION_MESSAGE)


class BrokerError(Exception):
    """An ok=false response other than ``version``; callers map ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def socket_path(environ: Mapping[str, str]) -> Path:
    return Path(environ.get(SOCKET_ENV) or DEFAULT_SOCKET)


class Client:
    def __init__(
        self,
        path: Path | str,
        *,
        connect_timeout: float = 5.0,
        retry_for: float = 0.0,
        missing_message: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """``retry_for`` keeps retrying a refused or missing socket that long
        (a broker restart is routine); ``missing_message`` replaces the generic
        text when the socket file is still absent at the end."""
        self._path = str(path)
        self._connect_timeout = connect_timeout
        self.retry_for = retry_for
        self._missing_message = missing_message
        self._clock = clock
        self._sleep = sleep
        self._sock: socket.socket | None = None
        self._reader: IO[bytes] | None = None
        self.connected = False  # a connect has succeeded at least once
        self.request_written = False  # a request has been fully written
        self.on_keepalive: Callable[[], float | None] | None = None
        self.connect_budget: float | None = None
        self.read_budget: Callable[[], float] | None = None

    def __enter__(self) -> Client:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        reader, sock = self._reader, self._sock
        self._reader = None
        self._sock = None
        for closable in (reader, sock):
            if closable is not None:
                try:
                    closable.close()
                except OSError:
                    pass

    def _connect(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        deadline = self._clock() + self.retry_for
        pause = _RETRY_PAUSE
        while True:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                budget = self._connect_timeout
                if self.connect_budget is not None:
                    budget = min(budget, self.connect_budget)
                sock.settimeout(budget)
                sock.connect(self._path)
                break
            except OSError as exc:  # includes connect timeout, refused, ENOENT
                sock.close()
                left = deadline - self._clock()
                if left <= 0:
                    if isinstance(exc, FileNotFoundError) and self._missing_message:
                        raise BrokerUnreachable(self._missing_message) from exc
                    raise BrokerUnreachable() from exc
                self._sleep(min(pause, left))
                pause = min(pause * 2, _RETRY_PAUSE_CAP)
        self._sock = sock
        self._reader = sock.makefile("rb")
        self.connected = True
        return sock

    def _next_line(self, sock: socket.socket, buffer: bytearray, timeout: float) -> bytes | None:
        """Bound the whole frame, not just each recv of a partial JSON line."""
        until = time.monotonic() + timeout
        while True:
            newline = buffer.find(b"\n")
            if newline >= 0:
                line = bytes(buffer[: newline + 1])
                del buffer[: newline + 1]
                if len(line) > wire.MAX_LINE_BYTES:
                    raise wire.WireError(wire.BAD_REQUEST, "keepalive frame too large")
                return line
            if len(buffer) >= wire.MAX_LINE_BYTES:
                raise wire.WireError(wire.BAD_REQUEST, "keepalive frame too large")
            remaining = until - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            sock.settimeout(remaining)
            data = sock.recv(min(65536, wire.MAX_LINE_BYTES + 1 - len(buffer)))
            if not data:
                return None
            buffer.extend(data)

    def call(self, op: str, args: dict[str, Any], *, timeout: float | None) -> Any:
        try:
            payload = wire.encode_request(wire.Request(op=op, args=args))
        except wire.WireError as exc:
            raise BrokerError(exc.code, exc.detail) from exc
        sock = self._connect()
        if self.read_budget is not None:
            timeout = self.read_budget()
        try:
            sock.settimeout(timeout)
            sock.sendall(payload)
            self.request_written = True
            assert self._reader is not None
            limit = wire.response_line_limit(op)
            buffer = bytearray()
            while True:
                if op == "next" and args.get("keepalive") is True and timeout is not None:
                    line = self._next_line(sock, buffer, timeout)
                else:
                    line = wire.read_line(self._reader, max_bytes=limit)
                if line is None:
                    self.close()
                    raise BrokerUnreachable()
                resp = wire.decode_response(line, max_bytes=limit)
                if not (
                    op == "next"
                    and args.get("keepalive") is True
                    and resp.ok
                    and resp.result == {"kind": "keepalive"}
                ):
                    break
                if self.on_keepalive is not None:
                    timeout = self.on_keepalive()
                    sock.settimeout(timeout)
        except TimeoutError:
            self.close()
            raise
        except wire.WireError as exc:
            self.close()
            if exc.code == wire.VERSION_ERROR:
                raise BrokerVersionMismatch() from exc
            raise BrokerUnreachable() from exc
        except OSError as exc:
            self.close()
            raise BrokerUnreachable() from exc
        if resp.ok:
            return resp.result
        if resp.error_code == wire.VERSION_ERROR:
            raise BrokerVersionMismatch()
        raise BrokerError(resp.error_code or wire.BAD_REQUEST, resp.error_detail)


def call_next(
    client_factory: Callable[[], Client],
    args: dict[str, Any],
    *,
    timeout: float | None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Long-poll ``next``, reconnecting after a dropped connection.

    The very first connect failing is raised at once; after one successful
    connect, drops are retried with backoff until the caller's deadline, at
    which point ``{"kind": "timeout"}`` is returned. Continuous unavailability
    is bounded even without a deadline. Only broker keepalives prove progress;
    repeatedly connecting to a hung executor must not reset that bound.
    ``client_factory`` re-resolves the socket path for each reconnect.
    """
    deadline = None if timeout is None else clock() + timeout
    ever_connected = False
    backoff = _BACKOFF_START
    unreachable_since: float | None = None
    last_progress = clock()

    def read_budget() -> float:
        budget = _KEEPALIVE_TIMEOUT
        if deadline is not None:
            budget = min(budget, max(deadline - clock(), 0.0) + _READ_GRACE)
        if unreachable_since is not None:
            budget = min(budget, max(_UNREACHABLE_LIMIT - (clock() - unreachable_since), 0.001))
        return budget

    def alive() -> float:
        nonlocal unreachable_since, backoff, ever_connected, last_progress
        last_progress = clock()
        unreachable_since = None
        backoff = _BACKOFF_START
        ever_connected = True
        if deadline is not None and clock() >= deadline:
            raise TimeoutError
        return read_budget()

    while True:
        remaining = None if deadline is None else deadline - clock()
        if remaining is not None and remaining <= 0:
            return {"kind": "timeout"}
        if unreachable_since is not None and clock() - unreachable_since >= _UNREACHABLE_LIMIT:
            raise BrokerUnreachable()
        attempt = {**args, "keepalive": True}
        if ever_connected and "replace" in attempt:
            # A reconnect must not steal the slot from a newer explicit waiter.
            attempt["replace"] = False
        if remaining is not None:
            attempt["timeout"] = remaining
        client = None
        try:
            client = client_factory()
            if unreachable_since is None:
                last_progress = clock()
            client.on_keepalive = alive
            client.read_budget = read_budget
            connect_budget = read_budget()
            if deadline is not None:
                connect_budget = min(connect_budget, max(deadline - clock(), 0.001))
            client.connect_budget = connect_budget
            # The factory's connect retry window (#92: rides out a broker
            # restart) must not outlive the caller's deadline; a reconnect's
            # window is also bounded by the continuous-outage limit (#102).
            retry_for = getattr(client, "retry_for", 0.0)  # test doubles may lack it
            if deadline is not None:
                retry_for = min(retry_for, max(deadline - clock(), 0.0))
            if unreachable_since is not None:
                outage_left = _UNREACHABLE_LIMIT - (clock() - unreachable_since)
                retry_for = min(retry_for, max(outage_left, 0.0))
            client.retry_for = retry_for
            return client.call("next", attempt, timeout=read_budget())
        except BrokerVersionMismatch:
            raise
        except BrokerUnreachable:
            connected = client is not None and client.connected
            ever_connected = ever_connected or connected
            if not ever_connected:
                raise
            if connected:
                backoff = _BACKOFF_START
            if unreachable_since is None:
                unreachable_since = clock()
        except TimeoutError:
            if deadline is not None and clock() >= deadline:
                return {"kind": "timeout"}
            ever_connected = ever_connected or (client is not None and client.connected)
            if unreachable_since is None:
                unreachable_since = last_progress
        finally:
            if client is not None:
                client.close()
        assert unreachable_since is not None
        pause = min(backoff, max(_UNREACHABLE_LIMIT - (clock() - unreachable_since), 0.0))
        if deadline is not None:
            pause = min(pause, max(deadline - clock(), 0.0))
        sleep(pause)
        backoff = min(backoff * 2, _BACKOFF_CAP)


def call_send(
    client_factory: Callable[[], Client],
    args: dict[str, Any],
    *,
    timeout: float | None,
) -> Any:
    """Send once; re-send the same request once if the connection dropped
    after it was written, or while it was being written (the broker never got
    a full line, and is idempotent by manifest id).

    A client-side ``TimeoutError`` propagates unchanged; callers map it to
    exit 3, like a "no reply" timeout."""
    deadline = None if timeout is None else time.monotonic() + timeout
    first = client_factory()
    try:
        return first.call("send", args, timeout=timeout)
    except BrokerVersionMismatch:
        raise
    except BrokerUnreachable:
        if not (first.request_written or first.connected):
            raise
    finally:
        first.close()
    remaining = None if deadline is None else deadline - time.monotonic()
    if remaining is not None and remaining <= 0:
        raise TimeoutError
    with client_factory() as second:
        return second.call("send", args, timeout=remaining)
