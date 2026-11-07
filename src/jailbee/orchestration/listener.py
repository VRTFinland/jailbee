r"""Host-side broker sockets: selector thread plus bounded inbox query workers.

Every container gets a listening AF_UNIX socket at ``<sock_dir>/broker.sock``.
Requests are decoded with :mod:`jailbee.orchestration.wire`. Mutations and
long-polls are queued to the broker executor. Snapshot queries read immutable
completed-round data here; SQLite inbox queries run on bounded daemon workers.
Responses are written back through :meth:`Listener.respond`. All selector and
socket state is owned by the listener thread; other threads hand work over
through a command queue plus a wakeup socket pair.

A client that half-closes (``SHUT_WR``) before its reply is dropped: EOF is
treated as a disconnect, so clients must keep the write side open until the
response has arrived.

Each connection has at most one request in flight: the next line is parsed
only after :meth:`Listener.respond` answered the previous one (a parked
long-poll counts as in flight), and only while the unsent replies stay under
``OUTBUF_CAP``. Each container also has at most ``MAX_INFLIGHT_PER_CONTAINER``
requests queued or in flight across its connections; a slot comes back only
when the request is answered or released (:meth:`Listener.release`), never
on a disconnect, so open/send/close loops cannot grow the executor's queue. A
disconnect is queued only for an executor request in flight; query workers
release their own slots when they finish, even after the connection drops.
SQLite inbox queries additionally share ``MAX_STORE_QUERIES_PER_CONTAINER``
queued or running slots per container. Excess queries wait unconsumed in the
connection's input buffer; snapshot and executor requests do not use these
slots. The bounded global worker queue still answers ``BUSY`` when full.

While a connection is held back its socket is still read, so a
disconnect is noticed, until a line's worth is buffered (``wire.MAX_LINE_BYTES``,
or ``wire.OUTBOX_MAX_LINE_BYTES`` for an ``outbox.put``); then reading pauses
too. A pipelining client therefore costs bounded memory and never has more
than one event ahead of another container's request.

Stopping the listener leaves the socket files in place, so clients get
ECONNREFUSED while the broker is down; only :meth:`Listener.close_container`
(the role ended) and :meth:`Listener.rotate_container` (a copy may hold the
directory) remove a socket file, together with its directory.

Standard library only.
"""

from __future__ import annotations

import contextlib
import errno
import itertools
import logging
import os
import queue
import selectors
import socket
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jailbee.orchestration import wire
from jailbee.orchestration.query import QUERY_OPS, SNAPSHOT_OPS

if TYPE_CHECKING:
    from jailbee.orchestration.query import Queries

log = logging.getLogger(__name__)

SOCKET_NAME = "broker.sock"
MAX_CONNECTIONS_PER_CONTAINER = 8
MAX_INFLIGHT_PER_CONTAINER = MAX_CONNECTIONS_PER_CONTAINER  # queued, unanswered requests
MAX_STORE_QUERIES_PER_CONTAINER = 2  # queued or running SQLite inbox queries
_ACCEPT_PAUSE = 1.0  # seconds a listening socket rests after EMFILE/ENFILE
_RECV_BYTES = 65536
_SUN_PATH_MAX = 107  # Linux sockaddr_un.sun_path minus the terminating NUL
OUTBUF_CAP = 4 * 1024 * 1024  # unsent reply bytes above which intake stops
_QUERY_WORKERS = 4
_QUERY_QUEUE_CAP = 32


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
    in_flight: bool = False
    store_held: bool = False


def _line_limit(conn: _Conn) -> int:
    """The cap for the line at the head of ``conn``'s buffer (an outbox put gets more)."""
    return wire.request_line_limit(bytes(conn.inbuf[:64]))


def _prepare_dir(sock_dir: Path) -> bool:
    """Create (0700, ours, no symlink) ``sock_dir``; return True when it was created."""
    if sock_dir.is_symlink():
        raise OSError(f"{sock_dir} is a symlink")
    created = not sock_dir.exists()
    sock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(sock_dir)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{sock_dir} is not a directory owned by the current user")
    os.chmod(sock_dir, 0o700)
    return created


def _bind(sock: socket.socket, path: Path) -> None:
    """Bind ``sock`` at ``path``, through the directory's fd when ``path`` is too long.

    ``sun_path`` holds 107 bytes; ``<home>/.local/state/.../sock/<uuid>.<token>/
    broker.sock`` can exceed that. ``/proc/self/fd/<dir fd>/broker.sock`` names
    the same file in a few dozen bytes.
    """
    if len(os.fsencode(path)) <= _SUN_PATH_MAX:
        sock.bind(str(path))
        return
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        sock.bind(f"/proc/self/fd/{fd}/{path.name}")
    finally:
        os.close(fd)


class Listener:
    def __init__(self, requests: queue.Queue[Event], *, queries: Queries | None = None) -> None:
        self._requests = requests
        self._queries = queries
        self._query_queue: queue.Queue[RequestEvent] = queue.Queue(maxsize=_QUERY_QUEUE_CAP)
        self._query_stop = threading.Event()
        self._query_lock = threading.Lock()  # orders worker handoffs against shutdown
        self._query_threads: list[threading.Thread] = []
        self._query_pending: set[int] = set()  # listener-owned; no executor disconnect event
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
        self._dirs: dict[str, Path] = {}  # uuid -> socket directory (under _lock)
        self._paused: dict[str, float] = {}  # uuid -> monotonic resume time
        # Requests queued and not yet answered or released: conn id -> uuid,
        # and their count per uuid. A disconnect frees nothing (H2).
        self._pending: dict[int, str] = {}
        self._inflight: dict[str, int] = {}
        self._store_queries: dict[str, int] = {}  # queued or running, listener-owned
        self._store_pending: set[int] = set()  # request classification survives disconnects
        self._live: set[int] = set()  # open connection ids (under _lock)
        self._thread: threading.Thread | None = None
        self._running = False

    # -- public API -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        if self._queries is not None:
            for i in range(_QUERY_WORKERS):
                worker = threading.Thread(
                    target=self._query_worker, name=f"broker-query-{i}", daemon=True
                )
                self._query_threads.append(worker)
                worker.start()
        self._thread = threading.Thread(target=self._run, name="broker-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # Daemon workers may be inside SQLite's bounded busy wait. Do not let
        # them delay shutdown or post replies into the closed selector.
        with self._query_lock:
            self._query_stop.set()
        thread = self._thread
        if thread is not None:
            self._running = False
            self._wake()
            thread.join()
            self._thread = None
        self._shutdown_all()
        deadline = time.monotonic() + 0.2
        for worker in self._query_threads:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        while True:
            try:
                self._query_queue.get_nowait()
            except queue.Empty:
                break
        self._pending.clear()
        self._inflight.clear()
        self._query_pending.clear()
        self._store_queries.clear()
        self._store_pending.clear()
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
        created = _prepare_dir(sock_dir)
        path = sock_dir / SOCKET_NAME
        try:
            self._call(lambda: self._open(uuid, path))
        except BaseException:
            if created:  # leave no empty directory behind per failed round
                with contextlib.suppress(OSError):
                    sock_dir.rmdir()
            raise
        return created

    def socket_dir(self, uuid: str) -> Path | None:
        """The directory ``uuid``'s socket lives in, or None when not served."""
        with self._lock:
            return self._dirs.get(uuid)

    def close_container(self, uuid: str) -> None:
        self._call(lambda: self._close_server(uuid, events=True, unlink=True))

    def rotate_container(self, uuid: str, sock_dir: Path) -> None:
        """Move ``uuid``'s socket to the fresh ``sock_dir`` and remove the old directory.

        Used when another instance may hold the old directory (a raw copy
        carrying ``uuid``'s device, whose detach failed or was lazy): every
        connection drops, and the old path stays dead, so the copy can never
        reach the socket again; only a device re-attached to ``sock_dir``
        can. Raises OSError when ``uuid`` is not served.
        """
        if not self.has_container(uuid):
            raise OSError(f"no socket for {uuid}")
        created = _prepare_dir(sock_dir)

        def rotate() -> None:
            server = self._servers.get(uuid)
            if server is None:
                raise OSError(f"no socket for {uuid}")
            self._close_server(uuid, events=True, unlink=True)
            self._open(uuid, sock_dir / SOCKET_NAME)

        try:
            self._call(rotate)
        except BaseException:
            if created:
                with contextlib.suppress(OSError):
                    sock_dir.rmdir()
            raise

    def is_connected(self, conn_id: int) -> bool:
        """Whether the connection is still open (its answer can still arrive)."""
        with self._lock:
            return conn_id in self._live

    def release(self, conn_id: int) -> None:
        """Free the request budget of ``conn_id`` without answering (idempotent).

        For a request the executor skips (its connection dropped) or a parked
        long-poll whose connection dropped.
        """
        self._post(lambda: self._finish(conn_id))

    def respond(
        self, conn_id: int, response: wire.Response, *, max_bytes: int = wire.MAX_LINE_BYTES
    ) -> None:
        """Answer the request in flight; ``max_bytes`` caps the reply line
        (`wire.response_line_limit` of the request's op)."""
        try:
            data = wire.encode_response(response, max_bytes=max_bytes)
        except wire.WireError as exc:
            data = wire.encode_response(wire.fail(exc.code, exc.detail))
        self._post(lambda: self._reply(conn_id, data))

    def _query_worker(self) -> None:
        queries = self._queries
        assert queries is not None
        while not self._query_stop.is_set():
            try:
                event = self._query_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._query_stop.is_set():
                return
            if not self.is_connected(event.conn_id):
                with self._query_lock:
                    if not self._query_stop.is_set():
                        self.release(event.conn_id)
                continue
            response = queries.answer(event.uuid, event.request)
            limit = wire.response_line_limit(event.request.op)
            with self._query_lock:
                if not self._query_stop.is_set():
                    self.respond(event.conn_id, response, max_bytes=limit)

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
        while not done.wait(0.1):
            thread = self._thread
            if thread is None or not thread.is_alive():
                # The loop is gone (shutdown): nobody else will run the command.
                self._run_commands()
                done.wait()
                break
        if box:
            raise box[0]

    # -- listener thread --------------------------------------------------

    def _run(self) -> None:
        while self._running:
            timeout = self._resume_paused()
            try:
                events = self._sel.select(timeout=timeout)
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

    def _resume_paused(self) -> float:
        """Re-register rested listening sockets; return the select timeout."""
        timeout = 1.0
        now = time.monotonic()
        for uuid, until in list(self._paused.items()):
            if until <= now:
                del self._paused[uuid]
                server = self._servers.get(uuid)
                if server is not None:
                    with contextlib.suppress(Exception):
                        self._sel.register(server.sock, selectors.EVENT_READ, ("listen", uuid))
            else:
                timeout = min(timeout, until - now)
        return timeout

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
            _bind(sock, path)
            os.chmod(path, 0o600)
            sock.listen(16)
        except BaseException:
            sock.close()
            raise
        self._servers[uuid] = _Server(uuid, sock, path)
        self._sel.register(sock, selectors.EVENT_READ, ("listen", uuid))
        with self._lock:
            self._known.add(uuid)
            self._dirs[uuid] = path.parent

    def _close_server(self, uuid: str, events: bool, unlink: bool = False) -> None:
        server = self._servers.pop(uuid, None)
        self._paused.pop(uuid, None)
        with self._lock:
            self._known.discard(uuid)
            self._dirs.pop(uuid, None)
        if server is None:
            return
        for conn_id in list(server.conns):
            self._drop_conn(conn_id, event=events)
        with contextlib.suppress(Exception):
            self._sel.unregister(server.sock)
        server.sock.close()
        if unlink:
            # The directory goes too: a stale mount of it (a copy's device
            # whose detach failed or was lazy) must never see a socket again.
            with contextlib.suppress(OSError):
                server.path.unlink()
            with contextlib.suppress(OSError):
                server.path.parent.rmdir()

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
            except OSError as exc:
                if exc.errno in (errno.EMFILE, errno.ENFILE):
                    log.error("accept for %s hit the fd limit; pausing %.0fs", uuid, _ACCEPT_PAUSE)
                    with contextlib.suppress(Exception):
                        self._sel.unregister(server.sock)
                    self._paused[uuid] = time.monotonic() + _ACCEPT_PAUSE
                else:
                    log.exception("accept failed for %s", uuid)
                return
            sock.setblocking(False)
            if len(server.conns) >= MAX_CONNECTIONS_PER_CONTAINER:
                with contextlib.suppress(OSError):
                    sock.send(wire.encode_response(wire.fail(wire.BUSY, "too many connections")))
                sock.close()
                continue
            conn_id = next(self._ids)
            self._conns[conn_id] = _Conn(conn_id, uuid, sock)
            with self._lock:
                self._live.add(conn_id)
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
            if conn.inbuf and not self._held(conn):
                self._parse(conn)  # the reply backlog drained below the cap
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
        self._parse(conn)

    def _held(self, conn: _Conn) -> bool:
        """Whether no further request may be taken from this connection yet."""
        return (
            conn.in_flight
            or conn.close_after_flush
            or len(conn.outbuf) > OUTBUF_CAP
            or self._inflight.get(conn.uuid, 0) >= MAX_INFLIGHT_PER_CONTAINER
            or (
                conn.store_held
                and self._store_queries.get(conn.uuid, 0) >= MAX_STORE_QUERIES_PER_CONTAINER
            )
        )

    def _parse(self, conn: _Conn) -> None:
        """Queue the next complete request line unless the connection is held."""
        while not self._held(conn):
            limit = _line_limit(conn)
            nl = conn.inbuf.find(b"\n")
            if nl < 0:
                if len(conn.inbuf) >= limit:
                    self._oversize(conn, limit)
                return
            if nl + 1 > limit:
                self._oversize(conn, limit)
                return
            line = bytes(conn.inbuf[: nl + 1])
            try:
                request = wire.decode_request(line, max_bytes=limit)
            except wire.WireError as exc:
                del conn.inbuf[: nl + 1]
                self._send(conn.conn_id, wire.encode_response(wire.fail(exc.code, exc.detail)))
                if conn.conn_id not in self._conns:
                    return  # dropped while replying (e.g. EPIPE)
                continue
            store_query = (
                self._queries is not None
                and request.op in QUERY_OPS
                and request.op not in SNAPSHOT_OPS
            )
            if (
                store_query
                and self._store_queries.get(conn.uuid, 0) >= MAX_STORE_QUERIES_PER_CONTAINER
            ):
                # Leave the line untouched and bound further reads while it waits.
                conn.store_held = True
                self._update_interest(conn)
                return
            conn.store_held = False
            del conn.inbuf[: nl + 1]
            if store_query:
                self._store_pending.add(conn.conn_id)
                self._store_queries[conn.uuid] = self._store_queries.get(conn.uuid, 0) + 1
            conn.in_flight = True
            self._pending[conn.conn_id] = conn.uuid
            self._inflight[conn.uuid] = self._inflight.get(conn.uuid, 0) + 1
            event = RequestEvent(conn.uuid, conn.conn_id, request)
            if self._queries is not None and request.op in QUERY_OPS:
                self._query_pending.add(conn.conn_id)
                if request.op in SNAPSHOT_OPS:
                    self.respond(conn.conn_id, self._queries.answer(conn.uuid, request))
                else:
                    try:
                        self._query_queue.put_nowait(event)
                    except queue.Full:
                        self.respond(conn.conn_id, wire.fail(wire.BUSY, "query workers are busy"))
            else:
                self._requests.put(event)
        self._update_interest(conn)

    def _oversize(self, conn: _Conn, limit: int) -> None:
        conn.inbuf.clear()
        detail = f"line exceeds {limit} bytes"
        self._send(
            conn.conn_id, wire.encode_response(wire.fail(wire.BAD_REQUEST, detail)), close=True
        )

    def _reply(self, conn_id: int, data: bytes) -> None:
        """Send the answer to the request in flight, then take the next one."""
        conn = self._conns.get(conn_id)
        if conn is not None:
            conn.in_flight = False
            self._send(conn_id, data)
        self._finish(conn_id)
        if conn is not None and conn_id in self._conns:
            self._parse(conn)

    def _finish(self, conn_id: int) -> None:
        """Return the request budget of ``conn_id``; resume connections it held."""
        uuid = self._pending.pop(conn_id, None)
        self._query_pending.discard(conn_id)
        if uuid is None:
            return
        if conn_id in self._store_pending:
            self._store_pending.remove(conn_id)
            store_left = self._store_queries[uuid] - 1
            if store_left > 0:
                self._store_queries[uuid] = store_left
            else:
                self._store_queries.pop(uuid)
        left = self._inflight.get(uuid, 0) - 1
        if left > 0:
            self._inflight[uuid] = left
        else:
            self._inflight.pop(uuid, None)
        server = self._servers.get(uuid)
        if server is None:
            return
        for other_id in sorted(server.conns):
            if self._inflight.get(uuid, 0) >= MAX_INFLIGHT_PER_CONTAINER:
                return
            other = self._conns.get(other_id)
            if other is not None and other.inbuf and not self._held(other):
                self._parse(other)

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
        if not conn.outbuf and conn.close_after_flush:
            self._drop_conn(conn.conn_id, event=True)
            return
        # Taking the next request after a drained backlog is the caller's job
        # (the write event or `_reply`): `_parse` itself sends through here.
        self._update_interest(conn)

    def _update_interest(self, conn: _Conn) -> None:
        """Watch for writes while replies are unsent; stop reading a full, held buffer."""
        events = 0
        if conn.outbuf:
            events |= selectors.EVENT_WRITE
        if not (self._held(conn) and len(conn.inbuf) >= _line_limit(conn)):
            events |= selectors.EVENT_READ
        registered = conn.sock in self._sel.get_map() if conn.sock.fileno() >= 0 else False
        with contextlib.suppress(KeyError, ValueError, OSError):
            if not events:
                if registered:
                    self._sel.unregister(conn.sock)
            elif registered:
                self._sel.modify(conn.sock, events, ("conn", conn.conn_id))
            else:
                self._sel.register(conn.sock, events, ("conn", conn.conn_id))

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
        with self._lock:
            self._live.discard(conn_id)
        # Only a request still in flight (perhaps a parked long-poll) needs the
        # executor to hear of the disconnect; its budget stays taken until the
        # executor answers or releases it.
        if event and conn_id in self._pending and conn_id not in self._query_pending:
            self._requests.put(DisconnectEvent(conn.uuid, conn_id))
