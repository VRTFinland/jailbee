"""Host-side socket listener for the broker: one thread, pure I/O.

Every container gets a listening AF_UNIX socket at ``<sock_dir>/broker.sock``.
Requests are decoded with :mod:`jailbee.orchestration.wire` and put on a queue
as events; responses are written back through :meth:`Listener.respond`. All
selector and socket state is owned by the listener thread; other threads hand
work over through a command queue plus a wakeup socket pair.

Standard library only.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import queue
import selectors
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jailbee.orchestration import wire

log = logging.getLogger(__name__)

SOCKET_NAME = "broker.sock"
_RECV_BYTES = 65536


@dataclass(frozen=True)
class RequestEvent:
    uuid: str
    conn_id: int
    request: wire.Request


@dataclass(frozen=True)
class DisconnectEvent:
    uuid: str
    conn_id: int


Event = RequestEvent | DisconnectEvent


@dataclass
class _Server:
    uuid: str
    sock: socket.socket
    path: Path
    conns: set[int] = field(default_factory=set)


@dataclass
class _Conn:
    conn_id: int
    uuid: str
    sock: socket.socket
    inbuf: bytearray = field(default_factory=bytearray)
    outbuf: bytearray = field(default_factory=bytearray)
    close_after_flush: bool = False


class Listener:
    def __init__(self, requests: queue.Queue[Event]) -> None:
        self._requests = requests
        self._sel = selectors.DefaultSelector()
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self._sel.register(self._wake_r, selectors.EVENT_READ, ("wake", None))
        self._commands: queue.Queue[Callable[[], None]] = queue.Queue()
        self._servers: dict[str, _Server] = {}
        self._conns: dict[int, _Conn] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._known: set[str] = set()
        self._thread: threading.Thread | None = None
        self._running = False

    # -- public API -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="broker-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is not None:
            self._running = False
            self._wake()
            thread.join()
            self._thread = None
        self._shutdown_all()
        for s in (self._wake_r, self._wake_w):
            with contextlib.suppress(OSError):
                s.close()
        with contextlib.suppress(Exception):
            self._sel.close()

    def has_container(self, uuid: str) -> bool:
        with self._lock:
            return uuid in self._known

    def open_container(self, uuid: str, sock_dir: Path) -> bool:
        """Start listening for ``uuid``; return True when ``sock_dir`` was created."""
        created = not sock_dir.exists()
        sock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if created:
            os.chmod(sock_dir, 0o700)
        path = sock_dir / SOCKET_NAME
        self._call(lambda: self._open(uuid, path))
        return created

    def close_container(self, uuid: str) -> None:
        self._call(lambda: self._close_server(uuid, events=True))

    def respond(self, conn_id: int, response: wire.Response) -> None:
        try:
            data = wire.encode_response(response)
        except wire.WireError as exc:
            data = wire.encode_response(wire.fail(exc.code, exc.detail))
        self._post(lambda: self._send(conn_id, data, close=False))

    # -- cross-thread plumbing -------------------------------------------

    def _wake(self) -> None:
        with contextlib.suppress(OSError):
            self._wake_w.send(b"x")

    def _post(self, fn: Callable[[], None]) -> None:
        self._commands.put(fn)
        self._wake()

    def _call(self, fn: Callable[[], Any]) -> None:
        if self._thread is None or threading.current_thread() is self._thread:
            fn()
            return
        done = threading.Event()
        box: list[BaseException] = []

        def run() -> None:
            try:
                fn()
            except BaseException as exc:  # relayed to the caller
                box.append(exc)
            finally:
                done.set()

        self._post(run)
        done.wait()
        if box:
            raise box[0]

    # -- listener thread --------------------------------------------------

    def _run(self) -> None:
        while self._running:
            try:
                events = self._sel.select(timeout=1.0)
            except Exception:
                log.exception("listener select failed")
                continue
            for key, mask in events:
                kind, ident = key.data
                try:
                    if kind == "wake":
                        self._drain_wake()
                    elif kind == "listen":
                        self._accept(ident)
                    else:
                        self._service_conn(ident, mask)
                except Exception:
                    log.exception("listener event failed")
                    if kind == "conn":
                        self._drop_conn(ident, event=True)
            self._run_commands()
        self._run_commands()

    def _drain_wake(self) -> None:
        with contextlib.suppress(OSError):
            while self._wake_r.recv(4096):
                pass

    def _run_commands(self) -> None:
        while True:
            try:
                fn = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                log.exception("listener command failed")

    def _open(self, uuid: str, path: Path) -> None:
        if uuid in self._servers:
            self._close_server(uuid, events=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.setblocking(False)
            sock.bind(str(path))
            os.chmod(path, 0o600)
            sock.listen(16)
        except BaseException:
            sock.close()
            raise
        self._servers[uuid] = _Server(uuid, sock, path)
        self._sel.register(sock, selectors.EVENT_READ, ("listen", uuid))
        with self._lock:
            self._known.add(uuid)

    def _close_server(self, uuid: str, events: bool) -> None:
        server = self._servers.pop(uuid, None)
        with self._lock:
            self._known.discard(uuid)
        if server is None:
            return
        for conn_id in list(server.conns):
            self._drop_conn(conn_id, event=events)
        with contextlib.suppress(Exception):
            self._sel.unregister(server.sock)
        server.sock.close()
        with contextlib.suppress(OSError):
            server.path.unlink()

    def _shutdown_all(self) -> None:
        for uuid in list(self._servers):
            self._close_server(uuid, events=False)

    def _accept(self, uuid: str) -> None:
        server = self._servers.get(uuid)
        if server is None:
            return
        while True:
            try:
                sock, _ = server.sock.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                log.exception("accept failed for %s", uuid)
                return
            sock.setblocking(False)
            conn_id = next(self._ids)
            self._conns[conn_id] = _Conn(conn_id, uuid, sock)
            server.conns.add(conn_id)
            self._sel.register(sock, selectors.EVENT_READ, ("conn", conn_id))

    def _service_conn(self, conn_id: int, mask: int) -> None:
        conn = self._conns.get(conn_id)
        if conn is None:
            return
        if mask & selectors.EVENT_WRITE:
            self._flush(conn)
            if conn_id not in self._conns:
                return
        if mask & selectors.EVENT_READ:
            self._read(conn)

    def _read(self, conn: _Conn) -> None:
        try:
            data = conn.sock.recv(_RECV_BYTES)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop_conn(conn.conn_id, event=True)
            return
        if not data:
            self._drop_conn(conn.conn_id, event=True)
            return
        if conn.close_after_flush:
            return
        conn.inbuf += data
        while True:
            nl = conn.inbuf.find(b"\n")
            if nl < 0:
                if len(conn.inbuf) >= wire.MAX_LINE_BYTES:
                    self._oversize(conn)
                return
            if nl + 1 > wire.MAX_LINE_BYTES:
                self._oversize(conn)
                return
            line = bytes(conn.inbuf[: nl + 1])
            del conn.inbuf[: nl + 1]
            try:
                request = wire.decode_request(line)
            except wire.WireError as exc:
                self._send(conn.conn_id, wire.encode_response(wire.fail(exc.code, exc.detail)))
                continue
            self._requests.put(RequestEvent(conn.uuid, conn.conn_id, request))

    def _oversize(self, conn: _Conn) -> None:
        conn.inbuf.clear()
        detail = f"line exceeds {wire.MAX_LINE_BYTES} bytes"
        self._send(
            conn.conn_id, wire.encode_response(wire.fail(wire.BAD_REQUEST, detail)), close=True
        )

    def _send(self, conn_id: int, data: bytes, close: bool = False) -> None:
        conn = self._conns.get(conn_id)
        if conn is None:
            return
        conn.outbuf += data
        if close:
            conn.close_after_flush = True
        self._flush(conn)

    def _flush(self, conn: _Conn) -> None:
        while conn.outbuf:
            try:
                sent = conn.sock.send(conn.outbuf)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                self._drop_conn(conn.conn_id, event=True)
                return
            del conn.outbuf[:sent]
        if conn.outbuf:
            events = selectors.EVENT_READ | selectors.EVENT_WRITE
        elif conn.close_after_flush:
            self._drop_conn(conn.conn_id, event=True)
            return
        else:
            events = selectors.EVENT_READ
        with contextlib.suppress(KeyError, ValueError):
            self._sel.modify(conn.sock, events, ("conn", conn.conn_id))

    def _drop_conn(self, conn_id: int, event: bool) -> None:
        conn = self._conns.pop(conn_id, None)
        if conn is None:
            return
        server = self._servers.get(conn.uuid)
        if server is not None:
            server.conns.discard(conn_id)
        with contextlib.suppress(Exception):
            self._sel.unregister(conn.sock)
        with contextlib.suppress(OSError):
            conn.sock.close()
        if event:
            self._requests.put(DisconnectEvent(conn.uuid, conn_id))
