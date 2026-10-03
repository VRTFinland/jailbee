"""StateServer over a real unix socket, with a fake gatherer."""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jailbee.dashboard import RepoGroup
from jailbee.global_config import GlobalConfig
from jailbee.state_service import paths
from jailbee.state_service.protocol import (
    PROTOCOL,
    Active,
    GatherError,
    Hello,
    Refresh,
    Shutdown,
    Snapshot,
    decode,
    encode,
)
from jailbee.state_service.server import StateServer, run_service

T0 = datetime(2026, 10, 4, tzinfo=UTC)


class FakeGatherer:
    """Gathers whenever asked to: on every tick with an active client or a refresh."""

    def __init__(self, fail_on: int | None = None, once: bool = False):
        self.once = once  # only the first gather yields a snapshot
        self.calls: list[tuple[bool, bool, tuple[Path, ...]]] = []
        self.seq = 0
        self.fail_on = fail_on

    def tick(self, *, active, refresh, roots):
        self.calls.append((active, refresh, tuple(roots)))
        if not (active or refresh):
            return None
        if self.once and self.seq:
            return None
        self.seq += 1
        if self.seq == self.fail_on:
            return GatherError("boom")
        return Snapshot(self.seq, T0, True, [RepoGroup(f"g{self.seq}", None, None, [])])


def wait_until(predicate, what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


class Running:
    """A StateServer on its own event loop thread."""

    def __init__(self, gatherer, path, **kw):
        self.server = StateServer(gatherer, version="v1", **kw)
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve(path)), daemon=True
        )
        self.thread.start()
        wait_until(path.exists, "the server to bind")


class Conn:
    def __init__(self, path, *, version="v1", cwd=None):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.reader = self.sock.makefile("rb")
        self.send(Hello(PROTOCOL, version, cwd))
        self.hello = self.recv()

    def send(self, message):
        self.sock.sendall(encode(message))

    def recv(self):
        return decode(self.reader.readline())

    def close(self):
        self.reader.close()
        self.sock.close()


@pytest.fixture
def sock_path(runtime_dir):
    paths.ensure_runtime_dir()
    return paths.socket_path()


def test_hello_is_answered_with_the_server_version(sock_path):
    Running(FakeGatherer(), sock_path)
    conn = Conn(sock_path)
    assert conn.hello == Hello(PROTOCOL, "v1")


def test_snapshots_reach_every_client(sock_path):
    Running(FakeGatherer(), sock_path)
    a = Conn(sock_path)
    b = Conn(sock_path)
    seq = a.recv().seq
    while (nxt := a.recv()).seq <= seq:
        pass
    seen = b.recv()
    while seen.seq < nxt.seq:
        seen = b.recv()
    assert seen == nxt


def test_a_late_joiner_gets_the_cached_latest_snapshot(sock_path):
    Running(FakeGatherer(once=True), sock_path)  # gathers exactly once
    a = Conn(sock_path)
    first = a.recv()
    b = Conn(sock_path)
    assert b.recv() == first  # no further broadcast will ever come: only the cache


def test_a_client_that_never_reads_cannot_keep_the_server_alive(sock_path, mocker):
    mocker.patch("jailbee.state_service.server.DRAIN_TIMEOUT_SECONDS", 0.3)

    class Big(FakeGatherer):
        def tick(self, *, active, refresh, roots):
            snap = super().tick(active=active, refresh=refresh, roots=roots)
            if isinstance(snap, Snapshot):
                snap.groups[0].prefix = "x" * 200_000
            return snap

    running = Running(Big(), sock_path, idle_timeout=0.5)
    stuck = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    stuck.connect(str(sock_path))
    stuck.sendall(encode(Hello(PROTOCOL, "v1")))  # hello done, then never read
    running.thread.join(timeout=8)
    assert not running.thread.is_alive()
    stuck.close()


def test_server_gathers_only_for_active_clients(sock_path):
    gatherer = FakeGatherer()  # gathers on every tick an active client exists
    Running(gatherer, sock_path)
    conn = Conn(sock_path)
    conn.recv()
    conn.send(Active(False))
    # Wait until the server has ticked with no active client.
    wait_until(lambda: not gatherer.calls[-1][0], "Active(False) to land")
    wait_until(lambda: len(gatherer.calls) > 0 and not gatherer.calls[-1][0], "an idle tick")
    seq_before, ticks_before = gatherer.seq, len(gatherer.calls)
    wait_until(lambda: len(gatherer.calls) >= ticks_before + 3, "more idle ticks")
    assert gatherer.seq == seq_before  # the schedule ticks, but nothing is gathered


def test_refresh_is_honoured_for_an_inactive_client(sock_path):
    gatherer = FakeGatherer()
    Running(gatherer, sock_path)
    conn = Conn(sock_path)
    conn.recv()
    conn.send(Active(False))
    wait_until(lambda: not gatherer.calls[-1][0], "Active(False) to land")
    conn.send(Refresh())
    wait_until(lambda: any(r for _a, r, _ in gatherer.calls), "a refresh tick")


def test_server_gathers_the_union_of_client_roots(sock_path):
    gatherer = FakeGatherer()
    Running(gatherer, sock_path)
    # Held in a list: a garbage-collected Conn closes its socket and drops out.
    conns = [Conn(sock_path, cwd="/repos/a"), Conn(sock_path, cwd="/repos/b"), Conn(sock_path)]
    wait_until(
        lambda: (
            bool(gatherer.calls) and gatherer.calls[-1][2] == (Path("/repos/a"), Path("/repos/b"))
        ),
        "the union of roots",
    )
    assert len(conns) == 3


def test_a_gather_error_is_broadcast(sock_path):
    Running(FakeGatherer(fail_on=1), sock_path)
    conn = Conn(sock_path)
    assert conn.recv() == GatherError("boom")


def test_a_client_of_another_version_gets_no_snapshots_but_can_shut_it_down(sock_path):
    running = Running(FakeGatherer(), sock_path)
    conn = Conn(sock_path, version="v0")
    assert conn.hello == Hello(PROTOCOL, "v1")
    conn.sock.settimeout(0.3)
    with pytest.raises(TimeoutError):
        conn.reader.readline()
    conn.sock.settimeout(5)
    conn.send(Shutdown())
    running.thread.join(timeout=3)
    assert not running.thread.is_alive()


def test_garbage_drops_only_that_client(sock_path):
    Running(FakeGatherer(), sock_path)
    good = Conn(sock_path)
    good.recv()
    bad = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bad.settimeout(5)
    bad.connect(str(sock_path))
    bad.sendall(b"nonsense\n")
    assert bad.recv(1) == b""  # closed by the server
    assert isinstance(good.recv(), Snapshot)


def test_the_server_exits_when_idle(sock_path):
    running = Running(FakeGatherer(), sock_path, idle_timeout=0.3)
    conn = Conn(sock_path)
    conn.close()
    running.thread.join(timeout=3)
    assert not running.thread.is_alive()


def test_run_service_exits_quietly_when_another_holds_the_lock(runtime_dir, mocker):
    import fcntl

    paths.ensure_runtime_dir()
    fd = os.open(paths.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    mocker.patch("jailbee.state_service.server.LOCK_WAIT_SECONDS", 0.1)
    serve = mocker.patch.object(StateServer, "serve")
    try:
        assert run_service(mocker.Mock()) == 0
    finally:
        os.close(fd)
    serve.assert_not_called()


def test_run_service_replaces_a_stale_socket(runtime_dir, mocker):
    paths.ensure_runtime_dir()
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(paths.socket_path()))  # bound, never listening: a dead server's leftover
    stale.close()
    bound: list[bool] = []

    async def fake_serve(self, path):
        bound.append(path.exists())

    mocker.patch.object(StateServer, "serve", fake_serve)
    mocker.patch("jailbee.dashboard.global_config_or_defaults", return_value=GlobalConfig())
    assert run_service(mocker.Mock()) == 0
    assert bound == [False]  # unlinked before serving
    assert not paths.socket_path().exists()
