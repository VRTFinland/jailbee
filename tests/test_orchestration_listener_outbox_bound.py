"""Broker memory bound for outbox puts (#89 review item 5).

At most one outbox-cap (8 MiB) line per container may be buffered or in
flight; a role that may not stage is refused before its put is buffered.
"""

from __future__ import annotations

import queue
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from jailbee.container_outbox.role import CHILD_REFUSAL
from jailbee.orchestration import wire
from jailbee.orchestration.listener import Event, Listener, RequestEvent
from jailbee.orchestration.query import ContainerSnapshot, Queries
from jailbee.orchestration.store import BrokerStore

_BIG = "y" * (3 * wire.MAX_LINE_BYTES)
_SLACK = 65536  # one recv beyond a line's worth


@pytest.fixture
def short_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(dir="/tmp", prefix="jbo"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _connect(path: Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(path / "broker.sock"))
    return s


def _recv_line(s: socket.socket) -> bytes:
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = s.recv(1 << 20)
        assert chunk
        buf += chunk
    return bytes(buf)


def _put(**args: object) -> bytes:
    return wire.encode_request(wire.Request(wire.OUTBOX_PUT, dict(args)))


def _wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _listener(
    short_dir: Path, role: str | None, answer: Callable[[str, wire.Request], wire.Response]
) -> tuple[Listener, Queries]:
    queries = Queries(BrokerStore(short_dir / "broker.db"))
    if role is not None:
        queries.publish({"u": ContainerSnapshot(role, True, "{}", None)})
    queries.answer = answer  # type: ignore[method-assign]  # a recording double
    q: queue.Queue[Event] = queue.Queue()
    lis = Listener(q, queries=queries)
    lis.start()
    lis.open_container("u", short_dir / "u")
    return lis, queries


def _max_inbuf(lis: Listener) -> int:
    box: list[int] = []
    lis._call(lambda: box.append(max((len(c.inbuf) for c in lis._conns.values()), default=0)))
    return box[0]


def test_a_second_outbox_put_waits_while_one_is_in_flight(short_dir: Path) -> None:
    entered, unblock = threading.Event(), threading.Event()
    seen: list[int] = []

    def answer(uuid: str, request: wire.Request) -> wire.Response:
        seen.append(len(str(request.args)))
        if len(seen) == 1:
            entered.set()
            assert unblock.wait(10)
        return wire.ok({"name": f"00{len(seen)}-a.json"})

    lis, _ = _listener(short_dir, "plain", answer)
    first, second = _connect(short_dir / "u"), _connect(short_dir / "u")
    try:
        first.sendall(_put(bodies={"b": _BIG}))
        assert entered.wait(5)
        sender = threading.Thread(target=second.sendall, args=(_put(bodies={"b": _BIG}),))
        sender.start()
        time.sleep(0.5)  # let the listener read whatever it is willing to
        assert _max_inbuf(lis) <= wire.MAX_LINE_BYTES + _SLACK
        assert len(seen) == 1
        unblock.set()
        assert wire.decode_response(_recv_line(first)).ok
        assert wire.decode_response(_recv_line(second)).result == {"name": "002-a.json"}
        sender.join(5)
        assert seen[1] > wire.MAX_LINE_BYTES
    finally:
        unblock.set()
        first.close()
        second.close()
        lis.stop()


def test_a_dropped_partial_put_frees_the_slot(short_dir: Path) -> None:
    # Fails when `_drop_conn` stops releasing an undispatched slot line (the
    # second put then waits forever). Without slots at all it passes by
    # construction; `test_a_second_outbox_put_waits_while_one_is_in_flight`
    # is the one that proves the slot exists.
    lis, _ = _listener(short_dir, "plain", lambda uuid, request: wire.ok({"name": "001-a.json"}))
    partial = _connect(short_dir / "u")
    whole = _connect(short_dir / "u")
    try:
        line = _put(bodies={"b": _BIG})
        partial.sendall(line[: 2 * wire.MAX_LINE_BYTES])  # holds the slot, no newline yet
        _wait_until(lambda: _max_inbuf(lis) >= 2 * wire.MAX_LINE_BYTES)
        partial.close()
        whole.sendall(line)
        assert wire.decode_response(_recv_line(whole)).ok
    finally:
        whole.close()
        lis.stop()


@pytest.mark.parametrize("role", ["child", "none"])
def test_a_non_writer_put_is_refused_without_buffering_or_decoding(
    short_dir: Path, role: str
) -> None:
    seen: list[str] = []

    def answer(uuid: str, request: wire.Request) -> wire.Response:
        seen.append(request.op)
        return wire.ok({"echo": request.op})

    lis, _ = _listener(short_dir, role, answer)
    c = _connect(short_dir / "u")
    peak = [0]
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], _max_inbuf(lis))
            time.sleep(0.005)

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        c.sendall(_put(bodies={"b": _BIG}))
        resp = wire.decode_response(_recv_line(c))
        assert resp.error_code == wire.REFUSED
        if role == "child":
            assert resp.error_detail == CHILD_REFUSAL
        assert seen == []  # never decoded nor handed to a query worker
        # The connection stays usable: the refused line was consumed whole.
        c.sendall(wire.encode_request(wire.Request(wire.OUTBOX_LIST, {"kind": "issue"})))
        assert wire.decode_response(_recv_line(c)).result == {"echo": wire.OUTBOX_LIST}
    finally:
        stop.set()
        watcher.join(5)
        c.close()
        lis.stop()
    assert peak[0] < wire.MAX_LINE_BYTES


def test_a_non_writer_put_past_the_outbox_cap_is_cut_off(short_dir: Path) -> None:
    lis, _ = _listener(short_dir, "child", lambda uuid, request: wire.ok(None))
    c = _connect(short_dir / "u")
    c.setblocking(False)
    head = _put(bodies={"b": ""})[:40]
    blob = head + b"z" * (wire.OUTBOX_MAX_LINE_BYTES + _SLACK)
    sent = 0
    # Buffering the line first would also end in the oversize reply; only the
    # peak buffer tells the skipped line from a buffered one.
    peak = [0]
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], _max_inbuf(lis))
            time.sleep(0.005)

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        deadline = time.monotonic() + 5
        while sent < len(blob) and time.monotonic() < deadline:
            try:
                sent += c.send(blob[sent:])
            except BlockingIOError:
                time.sleep(0.01)
            except OSError:
                break  # the broker closed the connection
        c.settimeout(5)
        resp = wire.decode_response(_recv_line(c))
        assert resp.error_code == wire.BAD_REQUEST
    finally:
        stop.set()
        watcher.join(5)
        c.close()
        lis.stop()
    assert peak[0] < wire.MAX_LINE_BYTES


def _slots(lis: Listener) -> dict[str, int]:
    box: list[dict[str, int]] = []
    lis._call(lambda: box.append(dict(lis._outbox_slots)))
    return box[0]


def _conn_count(lis: Listener) -> int:
    box: list[int] = []
    lis._call(lambda: box.append(len(lis._conns)))
    return box[0]


def test_the_outbox_slot_is_freed_after_a_busy_reply(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lis, _ = _listener(short_dir, "plain", lambda uuid, request: wire.ok({"name": "001-a.json"}))
    real_put = lis._query_queue.put_nowait
    full = [True]

    def put_nowait(event: RequestEvent) -> None:
        if full[0]:
            full[0] = False
            raise queue.Full
        real_put(event)

    monkeypatch.setattr(lis._query_queue, "put_nowait", put_nowait)
    first, second = _connect(short_dir / "u"), _connect(short_dir / "u")
    try:
        first.sendall(_put(bodies={"b": "x"}))
        assert wire.decode_response(_recv_line(first)).error_code == wire.BUSY
        assert _slots(lis) == {}
        second.sendall(_put(bodies={"b": _BIG}))
        assert wire.decode_response(_recv_line(second)).ok
    finally:
        first.close()
        second.close()
        lis.stop()


def test_the_outbox_slot_is_freed_after_a_decode_error(short_dir: Path) -> None:
    lis, _ = _listener(short_dir, "plain", lambda uuid, request: wire.ok({"name": "001-a.json"}))
    bad, good = _connect(short_dir / "u"), _connect(short_dir / "u")
    try:
        # A put head (so it takes the slot) whose line is not valid JSON.
        bad.sendall(_put(bodies={"b": "x"})[:40] + b"\x00 not json\n")
        assert wire.decode_response(_recv_line(bad)).error_code == wire.BAD_REQUEST
        assert _slots(lis) == {}
        good.sendall(_put(bodies={"b": _BIG}))
        assert wire.decode_response(_recv_line(good)).ok
    finally:
        bad.close()
        good.close()
        lis.stop()


def test_a_dispatched_put_keeps_the_slot_past_its_drop_until_answered(short_dir: Path) -> None:
    entered, unblock = threading.Event(), threading.Event()
    seen: list[int] = []

    def answer(uuid: str, request: wire.Request) -> wire.Response:
        seen.append(1)
        if len(seen) == 1:
            entered.set()
            assert unblock.wait(10)
        return wire.ok({"name": f"00{len(seen)}-a.json"})

    lis, _ = _listener(short_dir, "plain", answer)
    first, second = _connect(short_dir / "u"), _connect(short_dir / "u")
    try:
        first.sendall(_put(bodies={"b": "x"}))
        assert entered.wait(5)
        first.close()  # dropped while its put is being answered
        _wait_until(lambda: _conn_count(lis) == 1)
        assert len(_slots(lis)) == 1
        sender = threading.Thread(target=second.sendall, args=(_put(bodies={"b": _BIG}),))
        sender.start()
        time.sleep(0.3)
        assert seen == [1]  # the dropped put still holds the container's slot
        unblock.set()
        # Answering the dropped put frees the slot for the waiting one.
        assert wire.decode_response(_recv_line(second)).result == {"name": "002-a.json"}
        sender.join(5)
        _wait_until(lambda: _slots(lis) == {})
    finally:
        unblock.set()
        second.close()
        lis.stop()
