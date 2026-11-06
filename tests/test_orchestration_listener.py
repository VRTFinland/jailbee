from __future__ import annotations

import queue
import shutil
import socket
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from jailbee.orchestration import wire
from jailbee.orchestration.listener import DisconnectEvent, Event, Listener, RequestEvent


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
    evs = [q.get(timeout=2), q.get(timeout=2)]
    assert [e.request.op for e in evs if isinstance(e, RequestEvent)] == ["one", "two"]
    assert evs[0].conn_id == evs[1].conn_id


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
    ev = q.get(timeout=2)
    assert isinstance(ev, DisconnectEvent)


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
    assert not (d / "broker.sock").exists()
    assert d.is_dir()
    assert not lis.has_container("u")
    assert lis.open_container("u", d) is False
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
