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


def test_socket_available_is_parent_dir_exists(sockdir, path):
    env = {"JAILBEE_ORCHESTRATION_SOCKET": str(path)}
    assert transport.socket_available(env)  # dir exists, socket file does not
    gone = {"JAILBEE_ORCHESTRATION_SOCKET": str(sockdir / "nope" / "b.sock")}
    assert not transport.socket_available(gone)


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


def test_default_socket_needs_a_live_mount(tmp_path):
    # After a hot-unplug Incus leaves the empty mount point behind: only a
    # mount listed in mountinfo means the device is attached.
    info = tmp_path / "mountinfo"
    info.write_text(
        "36 35 98:0 / / rw - ext4 /dev/root rw\n"
        "90 36 0:52 /sock/u /run/jailbee/orchestration ro,relatime - btrfs /dev/x ro\n"
    )
    assert transport.socket_available({}, mountinfo=info)
    info.write_text("36 35 98:0 / / rw - ext4 /dev/root rw\n")
    assert not transport.socket_available({}, mountinfo=info)
    assert not transport.socket_available({}, mountinfo=tmp_path / "missing")


def test_mountinfo_octal_escapes_are_decoded(tmp_path):
    info = tmp_path / "mountinfo"
    info.write_text("90 36 0:52 / /run/a\\040b ro - btrfs /dev/x ro\n")
    env = {"JAILBEE_ORCHESTRATION_SOCKET": "/run/a b/broker.sock"}
    assert transport._mounted(Path("/run/a b"), info)
    assert not transport._mounted(Path("/run/a"), info)
    assert env  # the override path itself keeps the plain directory check
