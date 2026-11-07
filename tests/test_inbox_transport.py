import shutil
import socket
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from jailbee.inbox import transport
from jailbee.inbox.transport import (
    BrokerError,
    BrokerUnreachable,
    Client,
    call_next,
    call_send,
)
from jailbee.orchestration import wire


class FakeServer:
    """One handler per accepted connection; the handler gets (request, conn)
    and returns a Response, or None to drop the connection without answering."""

    def __init__(self, path: Path, handlers: list[Callable[[wire.Request], wire.Response | None]]):
        self.requests: list[wire.Request] = []
        self._handlers = handlers
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(path))
        self._srv.listen(8)
        self._srv.settimeout(10)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        for handler in self._handlers:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn, conn.makefile("rb") as reader:
                conn.settimeout(10)
                line = wire.read_line(reader)
                if line is None:
                    continue
                req = wire.decode_request(line)
                self.requests.append(req)
                resp = handler(req)
                if resp is not None:
                    conn.sendall(wire.encode_response(resp))
        self._srv.close()  # handlers exhausted: later connects are refused

    def close(self) -> None:
        self._srv.close()
        self._thread.join(timeout=5)


@pytest.fixture
def sockdir():
    d = tempfile.mkdtemp(dir="/tmp")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def path(sockdir):
    return sockdir / "b.sock"


def serve(path, *handlers):
    return FakeServer(path, list(handlers))


def test_socket_path_default_and_override():
    assert str(transport.socket_path({})) == transport.DEFAULT_SOCKET
    assert transport.socket_path({"JAILBEE_ORCHESTRATION_SOCKET": "/x/y"}) == Path("/x/y")


def test_exit_code_and_message():
    assert transport.EXIT_UNREACHABLE == 4
    assert "host broker is not reachable" in transport.UNREACHABLE_MESSAGE


def test_call_round_trip(path):
    srv = serve(path, lambda r: wire.ok({"echo": r.args}))
    try:
        with Client(path) as c:
            assert c.call("list", {"a": 1}, timeout=5) == {"echo": {"a": 1}}
        assert srv.requests[0].op == "list"
    finally:
        srv.close()


def test_error_response_is_broker_error(path):
    srv = serve(path, lambda r: wire.fail(wire.BUSY, "slow down"))
    try:
        with Client(path) as c, pytest.raises(BrokerError) as ei:
            c.call("send", {}, timeout=5)
        assert ei.value.code == "busy"
        assert ei.value.detail == "slow down"
    finally:
        srv.close()


def test_version_error_is_unreachable(path):
    srv = serve(path, lambda r: wire.fail(wire.VERSION_ERROR, "x"))
    try:
        with Client(path) as c, pytest.raises(BrokerUnreachable) as ei:
            c.call("list", {}, timeout=5)
        assert str(ei.value) == transport.VERSION_MESSAGE
    finally:
        srv.close()


def test_missing_socket(path):
    with Client(path) as c, pytest.raises(BrokerUnreachable) as ei:
        c.call("list", {}, timeout=1)
    assert str(ei.value) == transport.UNREACHABLE_MESSAGE


def test_refused_stale_socket(path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()  # file remains, nobody listening
    assert path.exists()
    with Client(path) as c, pytest.raises(BrokerUnreachable):
        c.call("list", {}, timeout=1)


def test_client_timeout(path):
    release = threading.Event()

    def slow(_r):
        release.wait(5)
        return None

    srv = serve(path, slow)
    try:
        with Client(path) as c, pytest.raises(TimeoutError):
            c.call("next", {}, timeout=0.2)
    finally:
        release.set()
        srv.close()


def test_call_next_reconnects_after_drop(path):
    srv = serve(path, lambda r: None, lambda r: wire.ok({"kind": "message", "id": "m1"}))
    try:
        result = call_next(
            lambda: Client(path),
            {"wake_on": None},
            timeout=20,
            sleep=lambda s: None,
        )
        assert result == {"kind": "message", "id": "m1"}
        assert len(srv.requests) == 2
        assert srv.requests[1].args["timeout"] <= 20
        assert srv.requests[1].args["wake_on"] is None
    finally:
        srv.close()


def test_call_next_first_connect_failure_is_immediate(path):
    with pytest.raises(BrokerUnreachable):
        call_next(lambda: Client(path), {}, timeout=20, sleep=lambda s: pytest.fail("slept"))


def test_call_next_deadline_returns_timeout(path):
    srv = serve(path, lambda r: None)  # drops once, then nobody listens
    now = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        # Fake time must not reconnect before the server has closed its listener.
        # A real reconnect resets backoff, so that race changes the second pause.
        srv._thread.join(timeout=5)
        assert not srv._thread.is_alive()
        sleeps.append(s)
        now[0] += s

    try:
        result = call_next(
            lambda: Client(path, connect_timeout=1),
            {},
            timeout=3.0,
            clock=lambda: now[0],
            sleep=sleep,
        )
    finally:
        srv.close()
    assert result == {"kind": "timeout"}
    assert sleeps[0] == 0.5
    assert sleeps[1] == 1.0
    assert sum(sleeps) == pytest.approx(3.0)


def test_call_next_version_mismatch_not_retried(path):
    srv = serve(path, lambda r: wire.fail(wire.VERSION_ERROR))
    try:
        with pytest.raises(BrokerUnreachable):
            call_next(lambda: Client(path), {}, timeout=20, sleep=lambda s: pytest.fail("slept"))
    finally:
        srv.close()


def test_unbounded_wait_stops_after_continuous_outage():
    now = [0.0]
    attempts = []

    class Dropped:
        connected = True

        def call(self, op, args, *, timeout):
            raise BrokerUnreachable()

        def close(self):
            pass

    def factory():
        attempts.append(now[0])
        if len(attempts) == 1:
            return Dropped()
        if now[0] > 120:
            pytest.fail("wait retried beyond the outage limit")
        raise BrokerUnreachable()

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(BrokerUnreachable):
        call_next(factory, {}, timeout=None, clock=lambda: now[0], sleep=sleep)
    assert now[0] == 120
    assert 0 < attempts[-1] < 120


def test_missed_keepalives_reconnect_but_do_not_reset_outage():
    now = [0.0]
    timeouts = []
    closed = []

    class Hung:
        connected = True

        def call(self, op, args, *, timeout):
            assert args["keepalive"] is True
            assert timeout is not None
            timeouts.append(timeout)
            now[0] += timeout
            if now[0] > 120:
                pytest.fail("connected but hung broker reset the outage bound")
            raise TimeoutError

        def close(self):
            closed.append(True)

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(BrokerUnreachable):
        call_next(Hung, {}, timeout=None, clock=lambda: now[0], sleep=sleep)
    assert timeouts[0] == 40
    assert len(timeouts) > 1
    assert len(closed) == len(timeouts)
    assert now[0] == 120


def test_keepalive_resets_outage_only_after_broker_progress():
    now = [0.0]
    attempts = []

    class Stub:
        connected = True

        def call(self, op, args, *, timeout):
            attempts.append(now[0])
            if len(attempts) == 1:
                raise BrokerUnreachable()
            if len(attempts) == 2:
                now[0] = 100
                self.on_keepalive()
                now[0] = 200
                raise BrokerUnreachable()
            return {"kind": "message", "id": "ready"}

        def close(self):
            pass

    def sleep(seconds):
        now[0] += seconds

    assert call_next(Stub, {}, timeout=None, clock=lambda: now[0], sleep=sleep) == {
        "kind": "message",
        "id": "ready",
    }
    assert len(attempts) == 3


def test_client_ignores_keepalive_until_final_wake(monkeypatch):
    monkeypatch.setattr(transport.time, "monotonic", lambda: 0.0)

    class Socket:
        def __init__(self):
            self.timeouts = []

        def settimeout(self, value):
            self.timeouts.append(value)

        def sendall(self, payload):
            pass

        def recv(self, size):
            return next(frames)

        def close(self):
            pass

    frames = iter(
        [
            wire.encode_response(wire.ok({"kind": "keepalive"})),
            wire.encode_response(wire.ok({"kind": "keepalive"})),
            wire.encode_response(wire.ok({"kind": "message", "id": "ready"})),
        ]
    )
    sock = Socket()
    client = Client("unused")
    client._sock = sock
    client._reader = sock
    budgets = iter([35, 25])
    client.on_keepalive = lambda: next(budgets)
    try:
        assert client.call("next", {"keepalive": True}, timeout=40) == {
            "kind": "message",
            "id": "ready",
        }
        assert sock.timeouts == [40, 40, 35, 35, 25, 25]
    finally:
        client.close()


def test_partial_keepalive_cannot_extend_frame_watchdog(monkeypatch):
    now = [0.0]
    reads = []
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])

    class Socket:
        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, path):
            pass

        def makefile(self, mode):
            return self

        def sendall(self, payload):
            pass

        def recv(self, size):
            reads.append(self.timeout)
            if self.timeout < 15:
                now[0] += self.timeout
                raise TimeoutError
            now[0] += 15
            return b" "

        def close(self):
            pass

    monkeypatch.setattr(transport.socket, "socket", lambda *a: Socket())
    with Client("unused") as client, pytest.raises(TimeoutError):
        client.call("next", {"keepalive": True}, timeout=40)
    assert now[0] == 40
    assert reads == [40, 25, 10]


@pytest.mark.parametrize("deadline", [None, 120.0])
def test_reconnect_connect_timeout_respects_remaining_budget(monkeypatch, deadline):
    now = [0.0]
    connect_timeouts = []
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])

    class Socket:
        def settimeout(self, timeout):
            self.timeout = timeout

        def connect(self, path):
            connect_timeouts.append(self.timeout)
            if len(connect_timeouts) == 1:
                now[0] = 118
                raise ConnectionRefusedError
            now[0] += self.timeout
            raise TimeoutError

        def close(self):
            pass

    class Dropped:
        connected = True

        def call(self, *args, **kw):
            raise BrokerUnreachable

        def close(self):
            pass

    count = [0]

    def factory():
        count[0] += 1
        return Dropped() if count[0] == 1 else Client("unused")

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(transport.socket, "socket", lambda *a: Socket())
    if deadline is None:
        with pytest.raises(BrokerUnreachable):
            call_next(factory, {}, timeout=None, clock=lambda: now[0], sleep=sleep)
    else:
        assert call_next(factory, {}, timeout=deadline, clock=lambda: now[0], sleep=sleep) == {
            "kind": "timeout"
        }
    assert connect_timeouts == [5.0, 1.0]
    assert now[0] == 120


def test_keepalives_do_not_extend_finite_wait_deadline():
    now = [0.0]
    calls = []

    class Streaming:
        connected = True

        def call(self, op, args, *, timeout):
            calls.append(timeout)
            assert args["timeout"] == 55
            now[0] = 30
            assert self.on_keepalive() == 30
            now[0] = 60
            self.on_keepalive()
            pytest.fail("keepalive extended the caller deadline")

        def close(self):
            pass

    assert call_next(Streaming, {}, timeout=55, clock=lambda: now[0]) == {"kind": "timeout"}
    assert calls == [40]


def test_old_broker_without_keepalives_is_unavailable():
    now = [0.0]

    class Legacy:
        connected = True

        def call(self, op, args, *, timeout):
            now[0] += timeout
            if now[0] > 120:
                pytest.fail("legacy broker silently hung the waiter")
            raise TimeoutError

        def close(self):
            pass

    def sleep(seconds):
        now[0] += seconds

    with pytest.raises(BrokerUnreachable):
        call_next(Legacy, {}, timeout=None, clock=lambda: now[0], sleep=sleep)
    assert now[0] == 120


def test_reconnect_resolves_socket_path_again(path, sockdir, monkeypatch):
    from jailbee.inbox import socket_cli

    other = sockdir / "new.sock"
    old_server = serve(path, lambda r: None)
    new_server = serve(other, lambda r: wire.ok({"kind": "message", "id": "moved"}))
    monkeypatch.setenv(transport.SOCKET_ENV, str(path))

    def sleep(seconds):
        monkeypatch.setenv(transport.SOCKET_ENV, str(other))

    try:
        assert call_next(socket_cli._factory, {}, timeout=None, sleep=sleep) == {
            "kind": "message",
            "id": "moved",
        }
        assert len(old_server.requests) == len(new_server.requests) == 1
    finally:
        old_server.close()
        new_server.close()


def test_call_send_resends_once(path):
    srv = serve(path, lambda r: None, lambda r: wire.ok({"state": "queued"}))
    try:
        assert call_send(lambda: Client(path), {"id": "x"}, timeout=5) == {"state": "queued"}
        assert srv.requests[0] == srv.requests[1]
    finally:
        srv.close()


def test_call_send_two_drops_unreachable(path):
    srv = serve(path, lambda r: None, lambda r: None)
    try:
        with pytest.raises(BrokerUnreachable):
            call_send(lambda: Client(path), {"id": "x"}, timeout=5)
        assert len(srv.requests) == 2
    finally:
        srv.close()


def test_call_send_error_response_is_final_and_not_resent(path):
    srv = serve(
        path,
        lambda r: wire.fail(wire.STORE, "disk full"),
        lambda r: wire.ok({"state": "queued"}),
    )
    try:
        with pytest.raises(BrokerError) as exc:
            call_send(lambda: Client(path), {"id": "x"}, timeout=5)
        assert exc.value.code == wire.STORE
        assert len(srv.requests) == 1
    finally:
        srv.close()


def test_call_send_connect_failure_not_resent(path):
    made = []

    def factory():
        made.append(1)
        return Client(path)

    with pytest.raises(BrokerUnreachable):
        call_send(factory, {}, timeout=1)
    assert len(made) == 1


def test_call_next_backoff_resets_after_reconnect():
    script = [True, False, False, True, False, None]  # connected?; None = success

    class Stub:
        def __init__(self, step):
            self.step = step
            self.connected = step is True

        def call(self, op, args, *, timeout):
            if self.step is None:
                return {"kind": "message"}
            raise BrokerUnreachable()

        def close(self):
            pass

    it = iter(script)
    sleeps: list[float] = []
    result = call_next(lambda: Stub(next(it)), {}, timeout=None, sleep=sleeps.append)
    assert result == {"kind": "message"}
    assert sleeps == [0.5, 1.0, 2.0, 0.5, 1.0]


def test_call_next_retries_missing_factory_after_connect():
    closed = []
    steps = iter(["connected", "missing", "missing", "connected", "missing", "success"])
    sleeps = []

    class Stub:
        connected = True

        def __init__(self, step):
            self.step = step

        def call(self, op, args, *, timeout):
            if self.step == "success":
                return {"kind": "message"}
            raise BrokerUnreachable()

        def close(self):
            closed.append(self.step)

    def factory():
        step = next(steps)
        if step == "missing":
            raise BrokerUnreachable("socket missing")
        return Stub(step)

    assert call_next(factory, {}, timeout=None, sleep=sleeps.append) == {"kind": "message"}
    assert sleeps == [0.5, 1.0, 2.0, 0.5, 1.0]
    assert closed == ["connected", "connected", "success"]


def test_call_next_factory_failures_honor_cap_and_deadline():
    now = [0.0]
    sleeps = []
    calls = [0]

    class Stub:
        connected = True

        def call(self, op, args, *, timeout):
            raise BrokerUnreachable()

        def close(self):
            pass

    def factory():
        calls[0] += 1
        if calls[0] == 1:
            return Stub()
        raise BrokerUnreachable("socket missing")

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    assert call_next(factory, {}, timeout=14, clock=lambda: now[0], sleep=sleep) == {
        "kind": "timeout"
    }
    assert sleeps == [0.5, 1.0, 2.0, 4.0, 5.0, 1.5]
    assert calls == [6]


def test_call_next_reconnect_does_not_repeat_takeover():
    attempts = []

    class Stub:
        connected = True

        def call(self, op, args, *, timeout):
            attempts.append(args.copy())
            if len(attempts) == 1:
                raise BrokerUnreachable()
            return {"kind": "message"}

        def close(self):
            pass

    original = {"replace": True}
    assert call_next(Stub, original, timeout=None, sleep=lambda _: None) == {"kind": "message"}
    assert [a["replace"] for a in attempts] == [True, False]
    assert original == {"replace": True}


def test_call_next_first_factory_failure_preserves_detail():
    error = BrokerUnreachable("socket missing")

    def factory():
        raise error

    with pytest.raises(BrokerUnreachable) as caught:
        call_next(factory, {}, timeout=20, sleep=lambda s: pytest.fail("slept"))
    assert caught.value is error


def test_call_send_resends_after_partial_write(path, monkeypatch):
    # the first connection is accepted but the request never reaches it
    srv = serve(path, lambda r: None, lambda r: wire.ok({"state": "queued"}))
    real = Client.call
    state = {"n": 0}

    def flaky(self, op, args, *, timeout):
        state["n"] += 1
        if state["n"] == 1:
            self._connect()
            raise BrokerUnreachable()  # dropped during sendall: request_written False
        return real(self, op, args, timeout=timeout)

    monkeypatch.setattr(Client, "call", flaky)
    try:
        assert call_send(lambda: Client(path), {"id": "x"}, timeout=5) == {"state": "queued"}
    finally:
        srv.close()


def test_send_reconnect_uses_remaining_deadline(monkeypatch):
    now = [0.0]
    timeouts = []

    class Stub:
        connected = True
        request_written = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            pass

        def call(self, op, args, *, timeout):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                now[0] = 4.0
                raise BrokerUnreachable
            return {"state": "started"}

    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    assert call_send(Stub, {}, timeout=5) == {"state": "started"}
    assert timeouts == [5, 1]


def test_call_send_timeout_propagates(path):
    release = threading.Event()

    def slow(_r):
        release.wait(5)
        return None

    srv = serve(path, slow)
    try:
        with pytest.raises(TimeoutError):
            call_send(lambda: Client(path), {"id": "x"}, timeout=0.2)
        assert len(srv.requests) == 1
    finally:
        release.set()
        srv.close()
