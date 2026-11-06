"""Container-side client for the host broker socket.

Standard library plus ``jailbee.orchestration.wire`` only, and no subprocess:
this module is imported by the inbox commands inside a container.
"""

from __future__ import annotations

import os
import re
import socket
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from jailbee.orchestration import wire

DEFAULT_SOCKET = "/run/jailbee/orchestration/broker.sock"
SOCKET_ENV = "JAILBEE_ORCHESTRATION_SOCKET"
UNREACHABLE_MESSAGE = (
    "jailbee inbox: the host broker is not reachable; "
    "if this persists, tell the human (`jailbee doctor` on the host)"
)
VERSION_MESSAGE = (
    "jailbee inbox: container and broker versions differ; run `jb apply` / restart the broker"
)
EXIT_UNREACHABLE = 4

_BACKOFF_START = 0.5
_BACKOFF_CAP = 5.0
_READ_GRACE = 5.0  # socket read slack beyond the server-side long-poll timeout


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


MOUNTINFO = Path("/proc/self/mountinfo")


def _mounted(directory: Path, mountinfo: Path) -> bool:
    """Whether `directory` is a mount point listed in `mountinfo`.

    `os.path.ismount` cannot tell: Incus bind-mounts the host directory, often
    from the same filesystem, so device and inode tests say "not a mount".
    """
    try:
        text = mountinfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    target = str(directory)
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 4:
            point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])
            if point == target:
                return True
    return False


def socket_available(environ: Mapping[str, str], *, mountinfo: Path = MOUNTINFO) -> bool:
    """True when the container has the orchestration device attached.

    The default socket's parent is the device's mount point. Incus leaves the
    empty directory behind after the device is removed from a running
    container, so only a live mount counts; inside it, a missing socket file or
    a refused connection means "broker down" (exit 4). An explicit
    `JAILBEE_ORCHESTRATION_SOCKET` (tests, debugging) only needs its directory.
    """
    path = socket_path(environ)
    if environ.get(SOCKET_ENV):
        return os.path.isdir(path.parent)
    return _mounted(path.parent, mountinfo)


class Client:
    def __init__(self, path: Path | str, *, connect_timeout: float = 5.0) -> None:
        self._path = str(path)
        self._connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self._reader: IO[bytes] | None = None
        self.connected = False  # a connect has succeeded at least once
        self.request_written = False  # a request has been fully written

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
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(self._connect_timeout)
            sock.connect(self._path)
        except OSError as exc:  # includes connect timeout, refused, ENOENT
            sock.close()
            raise BrokerUnreachable() from exc
        self._sock = sock
        self._reader = sock.makefile("rb")
        self.connected = True
        return sock

    def call(self, op: str, args: dict[str, Any], *, timeout: float | None) -> Any:
        try:
            payload = wire.encode_request(wire.Request(op=op, args=args))
        except wire.WireError as exc:
            raise BrokerError(exc.code, exc.detail) from exc
        sock = self._connect()
        try:
            sock.settimeout(timeout)
            sock.sendall(payload)
            self.request_written = True
            assert self._reader is not None
            line = wire.read_line(self._reader)
        except TimeoutError:
            self.close()
            raise
        except wire.WireError as exc:
            self.close()
            raise BrokerUnreachable() from exc
        except OSError as exc:
            self.close()
            raise BrokerUnreachable() from exc
        if line is None:
            self.close()
            raise BrokerUnreachable()
        try:
            resp = wire.decode_response(line)
        except wire.WireError as exc:
            self.close()
            if exc.code == wire.VERSION_ERROR:
                raise BrokerVersionMismatch() from exc
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
    which point ``{"kind": "timeout"}`` is returned.
    """
    deadline = None if timeout is None else clock() + timeout
    ever_connected = False
    backoff = _BACKOFF_START
    while True:
        remaining = None if deadline is None else deadline - clock()
        if remaining is not None and remaining <= 0:
            return {"kind": "timeout"}
        attempt = dict(args)
        if remaining is not None:
            attempt["timeout"] = remaining
        read_timeout = None if remaining is None else remaining + _READ_GRACE
        client = client_factory()
        try:
            return client.call("next", attempt, timeout=read_timeout)
        except BrokerVersionMismatch:
            raise
        except BrokerUnreachable:
            ever_connected = ever_connected or client.connected
            if not ever_connected:
                raise
            if client.connected:
                backoff = _BACKOFF_START  # the broker was reachable again
        except TimeoutError:
            return {"kind": "timeout"}
        finally:
            client.close()
        pause = backoff
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
    with client_factory() as second:
        return second.call("send", args, timeout=timeout)
