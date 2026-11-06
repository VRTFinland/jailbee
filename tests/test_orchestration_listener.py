from __future__ import annotations

import contextlib
import errno
import os
import queue
import shutil
import socket
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from jailbee.orchestration import wire
from jailbee.orchestration.listener import (
    MAX_CONNECTIONS_PER_CONTAINER,
    MAX_INFLIGHT_PER_CONTAINER,
    DisconnectEvent,
    Event,
    Listener,
    RequestEvent,
)


@pytest.fixture
def short_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(dir="/tmp", prefix="jbl"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def env(short_dir: Path) -> Iterator[tuple[Listener, queue.Queue[Event], Path]]:
    q: queue.Queue[Event] = queue.Queue()
    lis = Listener(q)
    lis.start()
    yield lis, q, short_dir
    lis.stop()


def connect(path: Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(str(path / "broker.sock"))
    return s


def connect_retrying(path: Path) -> socket.socket:
    """`connect`, waiting while the listen backlog is full (EAGAIN on AF_UNIX)."""
    deadline = time.monotonic() + 5
    while True:
        try:
            return connect(path)
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.01)


def recv_line(s: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(4096)
        assert chunk
        buf += chunk
    return buf


def req(op: str = "next", **args: object) -> bytes:
    return wire.encode_request(wire.Request(op, dict(args)))


def test_request_tagged_with_uuid(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    lis.open_container("u1", root / "u1")
    assert lis.has_container("u1")
    c = connect(root / "u1")
    c.sendall(req("ping", a=1))
    ev = q.get(timeout=2)
    assert isinstance(ev, RequestEvent)
    assert ev.uuid == "u1"
    assert ev.request == wire.Request("ping", {"a": 1})
    c.close()


def test_two_containers_do_not_cross(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    lis.open_container("a", root / "a")
    lis.open_container("b", root / "b")
    ca, cb = connect(root / "a"), connect(root / "b")
    cb.sendall(req("x"))
    ca.sendall(req("y"))
    seen: dict[str, str] = {}
    for _ in range(2):
        ev = q.get(timeout=2)
        assert isinstance(ev, RequestEvent)
        seen[ev.request.op] = ev.uuid
    assert seen == {"x": "b", "y": "a"}


def test_several_requests_on_one_connection(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    lis.open_container("u", root / "u")
    c = connect(root / "u")
    c.sendall(req("one") + req("two"))
    first = q.get(timeout=2)
    assert isinstance(first, RequestEvent) and first.request.op == "one"
    # One request in flight per connection: "two" waits for the reply to "one".
    with pytest.raises(queue.Empty):
        q.get(timeout=0.3)
    lis.respond(first.conn_id, wire.ok())
    second = q.get(timeout=2)
    assert isinstance(second, RequestEvent) and second.request.op == "two"
    assert first.conn_id == second.conn_id
    assert recv_line(c) == wire.encode_response(wire.ok())


def test_flooding_connection_cannot_starve_another_container(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    lis, q, root = env
    lis.open_container("flood", root / "flood")
    lis.open_container("calm", root / "calm")
    flood = connect(root / "flood")
    flood.sendall(b"".join(req("spam", n=i) for i in range(500)))
    time.sleep(0.3)
    calm = connect(root / "calm")
    calm.sendall(req("mine"))
    ahead = 0
    while True:
        ev = q.get(timeout=2)
        assert isinstance(ev, RequestEvent)
        if ev.uuid == "calm":
            break
        ahead += 1
    assert ahead <= 1


def test_unread_replies_stop_request_intake(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    """A client that never reads its replies cannot grow the reply buffer or the queue."""
    lis, q, root = env
    lis.open_container("u", root / "u")
    c = connect(root / "u")
    c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    big = wire.ok({"pad": "x" * 200_000})
    stop = threading.Event()

    def flood() -> None:
        with contextlib.suppress(OSError):
            while not stop.is_set():
                c.sendall(req("spam"))

    sender = threading.Thread(target=flood, daemon=True)
    sender.start()
    answered = 0
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            ev = q.get(timeout=0.3)
        except queue.Empty:
            break
        assert isinstance(ev, RequestEvent)
        lis.respond(ev.conn_id, big)
        answered += 1
    stop.set()
    # The intake stalls once the unread replies exceed the cap.
    assert answered < 40
    buffered = [len(conn.outbuf) for conn in lis._conns.values()]
    assert buffered and max(buffered) < 8 * 1024 * 1024
    c.close()


def test_respond_delivers(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    lis.open_container("u", root / "u")
    c = connect(root / "u")
    c.sendall(req())
    ev = q.get(timeout=2)
    lis.respond(ev.conn_id, wire.ok({"k": 1}))
    assert recv_line(c) == wire.encode_response(wire.ok({"k": 1}))
    lis.respond(9999, wire.ok())  # unknown: no-op


def test_malformed_json_gets_bad_request(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    lis.open_container("u", root / "u")
    c = connect(root / "u")
    c.sendall(b"not json\n")
    resp = wire.decode_response(recv_line(c))
    assert resp.error_code == wire.BAD_REQUEST
    c.sendall(req("after"))
    ev = q.get(timeout=2)
    assert isinstance(ev, RequestEvent) and ev.request.op == "after"


def test_version_mismatch(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    lis.open_container("u", root / "u")
    c = connect(root / "u")
    c.sendall(b'{"v":99,"op":"x","args":{}}\n')
    assert wire.decode_response(recv_line(c)).error_code == wire.VERSION_ERROR
    with pytest.raises(queue.Empty):
        q.get(timeout=0.2)


def test_oversize_line_closes(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    lis.open_container("u", root / "u")
    c = connect(root / "u")
    c.sendall(b"x" * (wire.MAX_LINE_BYTES + 10))
    assert wire.decode_response(recv_line(c)).error_code == wire.BAD_REQUEST
    assert c.recv(1) == b""
    with pytest.raises(queue.Empty):  # no request was in flight: nothing to tell
        q.get(timeout=0.3)


def test_client_disconnect(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    lis.open_container("u", root / "u")
    c = connect(root / "u")
    c.sendall(req())
    first = q.get(timeout=2)
    c.close()
    ev = q.get(timeout=2)
    assert ev == DisconnectEvent("u", first.conn_id)


def test_close_container(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    d = root / "u"
    lis.open_container("u", d)
    c = connect(d)
    c.sendall(req())
    first = q.get(timeout=2)
    lis.close_container("u")
    assert q.get(timeout=2) == DisconnectEvent("u", first.conn_id)
    assert not d.exists()
    assert not lis.has_container("u")
    assert lis.open_container("u", d) is True
    c2 = connect(d)
    c2.sendall(req("again"))
    ev = q.get(timeout=2)
    assert isinstance(ev, RequestEvent) and ev.request.op == "again"


def test_reopen_closes_old(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    d = root / "u"
    lis.open_container("u", d)
    c = connect(d)
    c.sendall(req())
    first = q.get(timeout=2)
    lis.open_container("u", d)
    assert q.get(timeout=2) == DisconnectEvent("u", first.conn_id)
    connect(d)


def test_modes_and_created_dir(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, _, root = env
    d = root / "p" / "u"
    assert lis.open_container("u", d) is True
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "broker.sock").stat().st_mode) == 0o600
    existing = root / "e"
    existing.mkdir()
    assert lis.open_container("e", existing) is False


def test_stale_socket_replaced(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    d = root / "u"
    d.mkdir()
    (d / "broker.sock").write_text("stale")
    lis.open_container("u", d)
    connect(d).sendall(req())
    assert isinstance(q.get(timeout=2), RequestEvent)


def test_stop_joins(short_dir: Path) -> None:
    q: queue.Queue[Event] = queue.Queue()
    lis = Listener(q)
    lis.start()
    lis.open_container("u", short_dir / "u")
    thread = lis._thread
    assert thread is not None and thread.is_alive()
    lis.stop()
    assert not thread.is_alive()


def test_stop_keeps_socket_file(short_dir: Path) -> None:
    q: queue.Queue[Event] = queue.Queue()
    lis = Listener(q)
    lis.start()
    d = short_dir / "u"
    lis.open_container("u", d)
    lis.stop()
    assert (d / "broker.sock").exists()
    with pytest.raises(ConnectionRefusedError):
        connect(d)


def test_connection_cap_answers_busy(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    d = root / "u"
    lis.open_container("u", d)
    held = []
    for _ in range(MAX_CONNECTIONS_PER_CONTAINER):
        c = connect(d)
        c.sendall(req())
        q.get(timeout=2)  # accepted and read
        held.append(c)
    extra = connect(d)
    assert wire.decode_response(recv_line(extra)).error_code == wire.BUSY
    assert extra.recv(10) == b""
    held[0].close()
    gone = q.get(timeout=2)
    assert isinstance(gone, DisconnectEvent)
    lis.release(gone.conn_id)  # the executor frees the dropped request's slot
    again = connect(d)
    again.sendall(req("ok"))
    ev = q.get(timeout=2)
    assert isinstance(ev, RequestEvent) and ev.request.op == "ok"


class _EmfileSock:
    def __init__(self, real: socket.socket) -> None:
        self.real = real
        self.calls = 0

    def fileno(self) -> int:
        return self.real.fileno()

    def accept(self) -> tuple[socket.socket, object]:
        self.calls += 1
        raise OSError(errno.EMFILE, "too many open files")

    def close(self) -> None:
        self.real.close()


def test_accept_emfile_pauses_listening(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, _q, root = env
    d = root / "u"
    lis.open_container("u", d)
    fake: list[_EmfileSock] = []

    def swap() -> None:
        server = lis._servers["u"]
        proxy = _EmfileSock(server.sock)
        fake.append(proxy)
        server.sock = proxy  # type: ignore[assignment]  # test double

    lis._call(swap)
    c = connect(d)
    time.sleep(0.4)
    assert fake[0].calls == 1  # not spinning
    time.sleep(1.5)
    assert 2 <= fake[0].calls <= 4  # re-registered after the pause
    c.close()


def test_no_request_queued_after_disconnect(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, q, root = env
    d = root / "u"
    lis.open_container("u", d)
    c = connect(d)
    c.sendall(b"bad\n" + req("late"))
    c.close()
    events: list[Event] = []
    deadline = time.monotonic() + 0.7
    while time.monotonic() < deadline:
        try:
            events.append(q.get(timeout=0.1))
        except queue.Empty:
            pass
    kinds = [type(e) for e in events]
    if DisconnectEvent in kinds:
        assert RequestEvent not in kinds[kinds.index(DisconnectEvent) :]


def test_call_does_not_hang_when_thread_exited(short_dir: Path) -> None:
    q: queue.Queue[Event] = queue.Queue()
    lis = Listener(q)
    lis.start()
    d = short_dir / "u"
    lis.open_container("u", d)
    thread = lis._thread
    assert thread is not None
    lis._running = False
    lis._wake()
    thread.join()  # loop gone, but stop() has not finished yet
    t = threading.Thread(target=lambda: lis.close_container("u"))
    t.start()
    t.join(timeout=3)
    assert not t.is_alive()
    assert not lis.has_container("u")
    lis.stop()


def test_existing_dir_becomes_0700(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, _, root = env
    d = root / "e"
    d.mkdir()
    d.chmod(0o755)
    lis.open_container("e", d)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_symlinked_dir_refused(env: tuple[Listener, queue.Queue[Event], Path]) -> None:
    lis, _, root = env
    target = root / "t"
    target.mkdir()
    link = root / "l"
    link.symlink_to(target)
    with pytest.raises(OSError):
        lis.open_container("l", link)
    assert not lis.has_container("l")


def test_large_response_survives_partial_writes(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    lis, q, root = env
    d = root / "u"
    lis.open_container("u", d)
    c = connect(d)
    c.settimeout(10)
    c.sendall(req())
    ev = q.get(timeout=2)
    blob = "x" * 600_000
    lis.respond(ev.conn_id, wire.ok({"blob": blob}))
    time.sleep(0.3)  # the kernel buffer fills; the rest waits for EVENT_WRITE
    assert lis._conns[ev.conn_id].outbuf
    resp = wire.decode_response(recv_line(c))
    assert resp.result == {"blob": blob}


def test_rotate_container_moves_the_socket_to_a_fresh_directory(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    """A copy holding the old directory (a stale or lazily detached mount) finds no socket."""
    lis, q, root = env
    lis.open_container("u", root / "u")
    old = connect(root / "u")
    old.sendall(req("next"))
    ev = q.get(timeout=2)
    lis.rotate_container("u", root / "u.g1")
    gone = q.get(timeout=2)
    assert isinstance(gone, DisconnectEvent) and gone.conn_id == ev.conn_id
    assert old.recv(1) == b""
    assert not (root / "u").exists()  # the old directory is gone, not merely emptied
    with pytest.raises(OSError):
        connect(root / "u")
    assert lis.has_container("u")
    assert lis.socket_dir("u") == root / "u.g1"
    assert stat.S_IMODE((root / "u.g1").stat().st_mode) == 0o700
    new = connect(root / "u.g1")
    new.sendall(req("again"))
    again = q.get(timeout=2)
    assert isinstance(again, RequestEvent) and again.uuid == "u"
    assert lis.socket_dir("unknown") is None


def test_rotate_container_of_an_unserved_uuid_is_refused(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    lis, _, root = env
    with pytest.raises(OSError):
        lis.rotate_container("unknown", root / "x")
    assert not lis.has_container("unknown")


def test_close_container_removes_the_socket_directory(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    """A later open must never reuse a directory a stale mount may still hold."""
    lis, _, root = env
    lis.open_container("u", root / "u")
    lis.close_container("u")
    assert not (root / "u").exists()


def test_a_run_of_invalid_lines_is_answered_without_recursion(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    lis, q, root = env
    lis.open_container("u", root / "u")
    c = connect(root / "u")
    c.sendall(b"x\n" * 3000)
    with c.makefile("rb") as reader:
        for _ in range(3000):
            line = reader.readline()
            assert line, "connection dropped"
            assert wire.decode_response(line).error_code == wire.BAD_REQUEST
    c.sendall(req("after"))
    ev = q.get(timeout=2)
    assert isinstance(ev, RequestEvent) and ev.request.op == "after"


def test_open_send_close_loops_cannot_flood_the_queue(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    """A request's budget is released by its answer, never by a disconnect."""
    lis, q, root = env
    lis.open_container("bad", root / "bad")
    lis.open_container("good", root / "good")
    for _ in range(100):
        c = connect_retrying(root / "bad")
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            c.sendall(req("list"))  # refused as busy now and then: fine
        c.close()
    good = connect(root / "good")
    good.sendall(req("list"))
    ahead: list[Event] = []
    while True:
        ev = q.get(timeout=5)
        if isinstance(ev, RequestEvent) and ev.uuid == "good":
            break
        ahead.append(ev)
    # At most one request (plus its disconnect) per budget slot gets ahead.
    assert len(ahead) <= 2 * MAX_INFLIGHT_PER_CONTAINER
    time.sleep(0.3)
    while not q.empty():
        ahead.append(q.get_nowait())
    assert len([e for e in ahead if isinstance(e, RequestEvent)]) <= MAX_INFLIGHT_PER_CONTAINER
    assert all(not lis.is_connected(e.conn_id) for e in ahead)


def test_the_budget_frees_when_the_request_is_answered_or_released(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    lis, q, root = env
    lis.open_container("u", root / "u")
    events: list[RequestEvent] = []
    for _ in range(MAX_INFLIGHT_PER_CONTAINER):
        c = connect(root / "u")
        c.sendall(req("list"))
        c.close()
    while len(events) < MAX_INFLIGHT_PER_CONTAINER:
        ev = q.get(timeout=2)
        if isinstance(ev, RequestEvent):
            events.append(ev)
    held = connect(root / "u")
    held.sendall(req("list"))
    with pytest.raises(queue.Empty):
        while True:
            ev = q.get(timeout=0.3)
            assert not isinstance(ev, RequestEvent), "budget exceeded"
    lis.release(events[0].conn_id)  # a dropped connection's request, skipped
    ev = q.get(timeout=2)
    assert isinstance(ev, RequestEvent) and lis.is_connected(ev.conn_id)
    lis.release(events[0].conn_id)  # idempotent: frees nothing twice
    lis.respond(ev.conn_id, wire.ok([]))
    assert wire.decode_response(recv_line(held)).ok
    deadline = time.monotonic() + 2
    while lis._inflight.get("u") != MAX_INFLIGHT_PER_CONTAINER - 1:
        assert time.monotonic() < deadline, lis._inflight  # the double release freed one
        time.sleep(0.01)


def test_a_disconnect_without_a_request_in_flight_queues_nothing(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    lis, q, root = env
    lis.open_container("u", root / "u")
    for _ in range(20):
        connect_retrying(root / "u").close()
    deadline = time.monotonic() + 5
    while lis._servers["u"].conns and time.monotonic() < deadline:
        time.sleep(0.01)  # every close noticed, so the next connection is accepted
    c = connect(root / "u")
    c.sendall(req("list"))
    ev = q.get(timeout=2)
    assert isinstance(ev, RequestEvent)


def test_a_held_connection_stops_reading_once_a_line_s_worth_is_buffered(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    """A pipelining client behind a parked request costs at most about one line of memory."""
    lis, q, root = env
    lis.open_container("u", root / "u")
    c = connect(root / "u")
    c.sendall(req("next"))
    ev = q.get(timeout=2)
    c.setblocking(False)
    blob = b"y" * (4 * wire.MAX_LINE_BYTES)
    sent = 0
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and sent < len(blob):
        try:
            sent += c.send(blob[sent:])
        except BlockingIOError:
            time.sleep(0.05)
    time.sleep(0.3)
    buffered = len(lis._conns[ev.conn_id].inbuf)
    assert wire.MAX_LINE_BYTES <= buffered < wire.MAX_LINE_BYTES + 65536
    assert sent < len(blob)  # the rest waits in the kernel: reading paused


def _connect_long(directory: Path) -> socket.socket:
    """Connect to a socket whose path exceeds the AF_UNIX limit (as a test client)."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(f"/proc/self/fd/{fd}/broker.sock")
        return s
    finally:
        os.close(fd)


def test_a_socket_path_beyond_the_af_unix_limit_still_binds(
    env: tuple[Listener, queue.Queue[Event], Path],
) -> None:
    """`~/.local/state/jailbee/orchestration/sock/<uuid>.<token>/broker.sock` is ~118 bytes."""
    lis, q, root = env
    long_dir = root / ("h" * 60) / f"{'u' * 36}.{'f' * 16}"
    assert len(str(long_dir / "broker.sock")) > 108
    lis.open_container("u", long_dir)
    assert (long_dir / "broker.sock").exists()
    c = _connect_long(long_dir)
    c.sendall(req("list"))
    assert isinstance(q.get(timeout=2), RequestEvent)
    lis.rotate_container("u", long_dir.with_name(f"{'u' * 36}.{'e' * 16}"))
    assert not long_dir.exists()


def test_a_failed_open_leaves_no_directory_behind(
    env: tuple[Listener, queue.Queue[Event], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    lis, _, root = env

    def refuse(self: socket.socket, address: object) -> None:
        raise OSError(errno.EADDRINUSE, "nope")

    monkeypatch.setattr(socket.socket, "bind", refuse)
    with pytest.raises(OSError):
        lis.open_container("u", root / "u.1")
    assert not (root / "u.1").exists()
    lis_dir_existing = root / "keep"
    lis_dir_existing.mkdir(mode=0o700)
    with pytest.raises(OSError):
        lis.open_container("k", lis_dir_existing)
    assert lis_dir_existing.exists()  # not ours to remove
