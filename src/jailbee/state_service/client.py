"""A dashboard's connection to the state service.

`StateClient` connects — spawning the server if none answers — and keeps the
latest snapshot from a reader thread. It never gathers anything itself: when
the service is unreachable the dashboard says so and keeps its last snapshot,
rather than silently falling back to polling incus on its own.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from typing import IO, TYPE_CHECKING

from jailbee import __version__
from jailbee.upgrade import parse_version
from jailbee.state_service import StaleDashboard, StateServiceError, StateServiceUnavailable
from jailbee.state_service.paths import ensure_runtime_dir, log_path, socket_path, spawn_lock_path
from jailbee.state_service.protocol import (
    PROTOCOL,
    Active,
    GatherError,
    Hello,
    Message,
    ProtocolError,
    Refresh,
    Shutdown,
    Snapshot,
    decode,
    encode,
)

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5.0
BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 10.0)
DISCONNECTED = "state service disconnected — reconnecting"


def spawn_server() -> None:
    """Start `jailbee _state-service` detached, its output appended to the log."""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as logf:
        subprocess.Popen(
            [sys.executable, "-m", "jailbee", "_state-service"],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            cwd="/",  # do not pin the first dashboard's directory for the server's lifetime
            start_new_session=True,
        )


@contextlib.contextmanager
def _flock(path: Path) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _try_connect(path: Path) -> socket.socket | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError):
        sock.close()
        return None
    return sock


class StateClient:
    """One dashboard's live view of the state service.

    ``on_update`` is called from the reader thread after every snapshot or
    status change — the Qt frontend turns it into a signal; the TUI polls
    `latest`/`status` each frame and passes none.
    """

    def __init__(
        self,
        cwd_root: Path | None,
        *,
        on_update: Callable[[], None] | None = None,
        spawn: Callable[[], None] = spawn_server,
        version: str = __version__,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        backoff: Sequence[float] = BACKOFF_SECONDS,
    ) -> None:
        self._cwd_root = cwd_root
        self._on_update = on_update
        self._spawn = spawn
        self._version = version
        self._connect_timeout = connect_timeout
        self._backoff = backoff
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._latest: Snapshot | None = None
        self._status: str | None = None
        self._active = True
        self._closed = threading.Event()
        self._first = threading.Event()
        self._thread = threading.Thread(target=self._run, name="jailbee-state-client", daemon=True)

    # ---- the dashboard's side ------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def wait_first_snapshot(self, timeout: float) -> Snapshot:
        """The first snapshot, or `StateServiceUnavailable` after ``timeout``
        (at once when this dashboard turned out to be stale)."""
        if not self._first.wait(timeout):
            reason = self.status() or "no snapshot from the state service"
            raise StateServiceUnavailable(f"{reason} (log: {log_path()})")
        snapshot = self.latest()
        if snapshot is None:  # `_first` was set by a stale stop, not a snapshot
            raise StateServiceUnavailable(self.status() or "the state client stopped")
        return snapshot

    def latest(self) -> Snapshot | None:
        with self._lock:
            return self._latest

    def status(self) -> str | None:
        """None while healthy; otherwise what is wrong, ready to show."""
        with self._lock:
            return self._status

    def refresh(self) -> None:
        self._send(Refresh())

    def set_active(self, value: bool) -> None:
        with self._lock:
            self._active = value
        self._send(Active(value))

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            if self._sock is not None:
                with contextlib.suppress(OSError):
                    self._sock.shutdown(socket.SHUT_RDWR)
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    # ---- the reader thread ---------------------------------------------------

    def _send(self, message: Message) -> None:
        """Best effort: a message to a dead connection is dropped, and the
        reader thread's reconnect re-sends what matters (the active flag)."""
        with self._lock:
            sock = self._sock
            if sock is None:
                return
            with contextlib.suppress(OSError):
                sock.sendall(encode(message))

    def _set_status(self, text: str | None) -> None:
        with self._lock:
            changed = self._status != text
            self._status = text
        if changed:
            self._notify()

    def _notify(self) -> None:
        """Call ``on_update``; a failing callback must not kill the reader."""
        if self._on_update is None:
            return
        try:
            self._on_update()
        except Exception:
            log.exception("state client on_update callback failed")

    def _run(self) -> None:
        attempt = 0
        while not self._closed.is_set():
            try:
                sock, reader = self._connect()
            except StaleDashboard as exc:
                # Retrying would respawn a server after each idle exit, and
                # the newer one is what everyone else is already using.
                self._set_status(str(exc))
                self._first.set()  # wakes `wait_first_snapshot`
                return
            except (OSError, ProtocolError, StateServiceError) as exc:
                log.debug("state service connect failed", exc_info=True)
                self._set_status(f"state service unavailable: {exc}")
                self._closed.wait(self._backoff[min(attempt, len(self._backoff) - 1)])
                attempt += 1
                continue
            with self._lock:
                if self._closed.is_set():
                    # close() ran while we were connecting: it saw no socket
                    # to shut down, so this connection is ours to drop.
                    reader.close()
                    sock.close()
                    return
                self._sock = sock
                # Replayed under the lock, so a concurrent `set_active` lands
                # wholly before this send (and is what we replay) or after it.
                if not self._active:
                    with contextlib.suppress(OSError):
                        sock.sendall(encode(Active(False)))
            got_snapshot = False
            try:
                for line in reader:
                    if self._dispatch(decode(line)):
                        got_snapshot = True
            except (OSError, ProtocolError):
                log.debug("state service connection lost", exc_info=True)
            finally:
                with self._lock:
                    self._sock = None
                reader.close()
                sock.close()
            if self._closed.is_set():
                return
            self._set_status(DISCONNECTED)
            if got_snapshot:
                attempt = 0
            else:
                # The server answered but never delivered: back off rather
                # than hammer a server that drops us straight away.
                self._closed.wait(self._backoff[min(attempt, len(self._backoff) - 1)])
                attempt += 1

    def _dispatch(self, message: Message) -> bool:
        """Apply ``message``; True when it was a snapshot."""
        if isinstance(message, Snapshot):
            with self._lock:
                self._latest = message
                self._status = None
            self._first.set()
            self._notify()
            return True
        if isinstance(message, GatherError):
            self._set_status(f"refresh failed: {message.message}")
        return False

    def _connect(self) -> tuple[socket.socket, IO[bytes]]:
        ensure_runtime_dir()
        path = socket_path()
        sock = _try_connect(path)
        if sock is None:
            with _flock(spawn_lock_path()):
                sock = _try_connect(path) or self._spawn_and_connect(path)
        return self._handshake(sock, path)

    def _spawn_and_connect(self, path: Path) -> socket.socket:
        self._spawn()
        deadline = time.monotonic() + self._connect_timeout
        while time.monotonic() < deadline:
            sock = _try_connect(path)
            if sock is not None:
                return sock
            time.sleep(0.05)
        raise StateServiceError(f"the state service did not start (log: {log_path()})")

    def _handshake(self, sock: socket.socket, path: Path) -> tuple[socket.socket, IO[bytes]]:
        sock.settimeout(self._connect_timeout)
        reader = sock.makefile("rb")
        try:
            cwd = str(self._cwd_root) if self._cwd_root is not None else None
            sock.sendall(encode(Hello(PROTOCOL, self._version, cwd)))
            reply = decode(reader.readline())
            if not isinstance(reply, Hello):
                raise ProtocolError(f"expected hello, got {type(reply).__name__}")
            if (reply.protocol, reply.version) != (PROTOCOL, self._version):
                # The newer jailbee wins: a stale dashboard must not shut
                # down a newer server (its respawn runs the new code, so the
                # two would flap forever). A differing protocol integer, an
                # older server or an unparseable version: the server is
                # replaced by our own.
                theirs, ours = parse_version(reply.version), parse_version(self._version)
                if reply.protocol == PROTOCOL and theirs and ours and theirs > ours:
                    raise StaleDashboard(
                        f"jailbee was upgraded to {reply.version}; restart this dashboard"
                    )
                sock.sendall(encode(Shutdown()))
                self._await_gone(path)
                raise StateServiceError(f"replaced a state service from jailbee {reply.version}")
        except BaseException:
            reader.close()
            sock.close()
            raise
        sock.settimeout(None)
        return sock, reader

    def _await_gone(self, path: Path) -> None:
        deadline = time.monotonic() + self._connect_timeout
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
