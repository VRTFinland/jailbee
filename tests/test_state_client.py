"""StateClient against a real StateServer on a real socket."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import socket
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest

from jailbee.dashboard import RepoGroup
from jailbee.lifecycle import ContainerInfo
from jailbee.state_service import StateServiceUnavailable, paths
from jailbee.state_service import client as client_module
from jailbee.state_service.client import DISCONNECTED, StateClient
from jailbee.state_service.protocol import PROTOCOL, Hello, Snapshot, encode
from jailbee.state_service.server import StateServer
from tests.test_state_server import T0, FakeGatherer, reap_servers, track_server  # noqa: F401


class Spawner:
    """Starts a StateServer in-process, the way `jailbee _state-service` would."""

    def __init__(self, gatherer_factory=FakeGatherer, version="v1"):
        self.gatherer_factory = gatherer_factory
        self.version = version
        self.servers: list[StateServer] = []
        self.threads: list[threading.Thread] = []

    def __call__(self):
        paths.ensure_runtime_dir()
        server = StateServer(self.gatherer_factory(), version=self.version, idle_timeout=0.5)
        self.servers.append(server)
        thread = threading.Thread(
            target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
        )
        thread.start()
        self.threads.append(thread)
        track_server(server, thread)


def _client(spawner, **kw):
    kw.setdefault("version", spawner.version)
    kw.setdefault("backoff", (0.05,))
    kw.setdefault("spawn", spawner)
    client = StateClient(kw.pop("cwd_root", None), **kw)
    client.start()
    return client


def _until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_the_first_client_spawns_the_server(runtime_dir):
    spawner = Spawner()
    client = _client(spawner)
    try:
        snap = client.wait_first_snapshot(3)
        assert snap.seq >= 1
        assert len(spawner.servers) == 1
        assert client.status() is None
    finally:
        client.close()


def test_two_clients_share_one_server(runtime_dir):
    spawner = Spawner()
    a, b = _client(spawner), _client(spawner)
    try:
        a.wait_first_snapshot(3)
        b.wait_first_snapshot(3)
        assert len(spawner.servers) == 1
    finally:
        a.close()
        b.close()


def test_racing_clients_spawn_once(runtime_dir):
    spawner = Spawner()
    clients = [_client(spawner) for _ in range(4)]
    try:
        for c in clients:
            c.wait_first_snapshot(3)
        assert len(spawner.servers) == 1
    finally:
        for c in clients:
            c.close()


def test_on_update_fires_for_snapshots(runtime_dir):
    spawner = Spawner()
    seen = threading.Event()
    client = _client(spawner, on_update=seen.set)
    try:
        assert seen.wait(3)
    finally:
        client.close()


def test_no_server_is_unavailable_with_the_log_path(runtime_dir):
    client = _client(Spawner(), spawn=lambda: None, connect_timeout=0.2)
    try:
        with pytest.raises(StateServiceUnavailable, match=r"state-service\.log"):
            client.wait_first_snapshot(0.5)
    finally:
        client.close()


def test_a_gather_error_becomes_the_status_and_keeps_the_snapshot(runtime_dir):
    spawner = Spawner(lambda: FakeGatherer(fail_on=2))
    client = _client(spawner)
    try:
        first = client.wait_first_snapshot(3)
        client.refresh()
        _until(lambda: client.status() == "refresh failed: boom")
        assert client.latest().seq >= first.seq
    finally:
        client.close()


def test_client_survives_a_server_killed_with_a_stale_socket_left_behind(runtime_dir, monkeypatch):
    spawner = Spawner()
    gate = threading.Event()
    attempts: list[int] = []
    real_ensure = client_module.ensure_runtime_dir

    def gated_ensure():
        # Every connect attempt after the first is held until the stale
        # socket is in place, so the client is certain to meet it.
        attempts.append(1)
        if len(attempts) > 1:
            gate.wait(5)
        return real_ensure()

    monkeypatch.setattr(client_module, "ensure_runtime_dir", gated_ensure)

    def spawn():
        # What `run_service` does: the lifetime-lock holder removes a stale socket.
        paths.socket_path().unlink(missing_ok=True)
        spawner()

    client = _client(spawner, spawn=spawn)
    try:
        first = client.wait_first_snapshot(3)
        spawner.servers[0]._stopping = True  # the server is gone...
        spawner.threads[0].join(timeout=3)
        assert not spawner.threads[0].is_alive()
        # ...and, as after a SIGKILL, its socket file is still there, unanswered.
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(paths.socket_path()))
        stale.close()
        assert paths.socket_path().exists()
        _until(lambda: client.status() == DISCONNECTED)
        assert client.latest() is not None
        assert client.latest().seq >= first.seq
        assert len(spawner.servers) == 1
        gate.set()
        _until(lambda: len(spawner.servers) == 2 and client.status() is None, timeout=5)
    finally:
        gate.set()
        client.close()


def test_client_replaces_a_server_of_another_version(runtime_dir):
    old = Spawner(version="v0")
    old()
    _until(paths.socket_path().exists)
    new = Spawner(version="v1")
    client = _client(new)
    try:
        client.wait_first_snapshot(5)
        assert len(new.servers) == 1
        old.threads[0].join(timeout=3)
        assert not old.threads[0].is_alive()
    finally:
        client.close()


def test_a_stale_client_leaves_a_newer_server_alone_and_stops(runtime_dir, monkeypatch):
    newer = Spawner(version="1.7.0")
    newer()
    _until(paths.socket_path().exists)
    connects: list[int] = []
    real_ensure = client_module.ensure_runtime_dir

    def counting_ensure():
        connects.append(1)
        return real_ensure()

    monkeypatch.setattr(client_module, "ensure_runtime_dir", counting_ensure)
    client = _client(Spawner(version="1.6.0"))
    try:
        _until(lambda: not client._thread.is_alive())
        assert "1.7.0" in client.status()
        assert "restart this dashboard" in client.status()
        assert newer.threads[0].is_alive()
        assert not newer.servers[0]._stopping
        assert len(connects) == 1
        time.sleep(0.4)  # several backoff periods: no reconnect
        assert len(connects) == 1
        with pytest.raises(StateServiceUnavailable, match="restart this dashboard"):
            client.wait_first_snapshot(5)
    finally:
        client.close()
        newer.servers[0]._stopping = True


def test_client_replaces_an_older_server_even_with_parseable_versions(runtime_dir):
    old = Spawner(version="1.6.0")
    old()
    _until(paths.socket_path().exists)
    new = Spawner(version="1.7.0")
    client = _client(new)
    try:
        client.wait_first_snapshot(5)
        old.threads[0].join(timeout=3)
        assert not old.threads[0].is_alive()
    finally:
        client.close()


def test_set_active_reaches_the_server_and_survives_a_reconnect(runtime_dir):
    spawner = Spawner()
    client = _client(spawner)
    try:
        client.wait_first_snapshot(3)
        client.set_active(False)
        _until(lambda: not any(c.active for c in spawner.servers[0]._clients))
        spawner.servers[0]._stopping = True
        _until(lambda: len(spawner.servers) == 2 and spawner.servers[1]._clients)
        _until(lambda: not any(c.active for c in spawner.servers[1]._clients))
    finally:
        client.close()


def test_client_receives_a_snapshot_over_64_kib(runtime_dir):
    big = [
        RepoGroup(
            "alpha",
            "/a",
            None,
            [
                ContainerInfo(
                    f"alpha-{i:04d}", "Running", "strict", "10.0.0.1", "4GiB", repo="alpha"
                )
                for i in range(800)
            ],
        )
    ]

    class BigGatherer(FakeGatherer):
        def tick(self, *, active, refresh, roots):
            if not (active or refresh):
                return None
            self.seq += 1
            return Snapshot(self.seq, T0, True, big)

    client = _client(Spawner(BigGatherer))
    try:
        assert len(client.wait_first_snapshot(5).groups[0].containers) == 800
    finally:
        client.close()


def test_the_cwd_root_is_sent_in_hello(runtime_dir):
    spawner = Spawner()
    client = _client(spawner, cwd_root=Path("/repos/a"))
    try:
        client.wait_first_snapshot(3)
        assert [c.cwd_root for c in spawner.servers[0]._clients] == [Path("/repos/a")]
    finally:
        client.close()


def test_close_during_a_connection_attempt_leaves_no_connected_client(runtime_dir):
    spawner = Spawner()
    entered, release = threading.Event(), threading.Event()

    def slow_spawn():
        entered.set()
        release.wait(5)
        spawner()

    client = _client(spawner, spawn=slow_spawn)
    closer = threading.Thread(target=client.close)
    try:
        assert entered.wait(3)
        closer.start()
        _until(client._closed.is_set)
        release.set()
        closer.join(timeout=5)
        client._thread.join(timeout=3)
        assert not client._thread.is_alive()
        _until(lambda: not spawner.servers[0]._clients)
    finally:
        release.set()
        client.close()


def test_a_failing_on_update_does_not_stop_the_reader(runtime_dir):
    calls: list[int] = []

    def on_update():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("callback bug")

    client = _client(Spawner(), on_update=on_update)
    try:
        first = client.wait_first_snapshot(3)
        client.refresh()
        _until(lambda: client.latest().seq > first.seq)
        assert client._thread.is_alive()
    finally:
        client.close()


class _HookedLock:
    """A lock that runs ``hook`` once, right after the first release that follows a connect."""

    def __init__(self, client_ref, hook):
        self._lock = threading.Lock()
        self._client_ref = client_ref
        self._hook = hook

    def __enter__(self):
        self._lock.acquire()

    def __exit__(self, *exc):
        self._lock.release()
        hook = self._hook
        if hook is not None and self._client_ref[0]._sock is not None:
            self._hook = None
            hook()


def test_a_set_active_between_reading_and_replaying_the_flag_is_not_overwritten(runtime_dir):
    spawner = Spawner()
    ref: list[StateClient] = []
    fired = threading.Event()

    def hook():
        ref[0].set_active(True)  # the user's newer choice, racing the replay
        fired.set()

    client = StateClient(None, spawn=spawner, version="v1", backoff=(0.05,))
    ref.append(client)
    client._lock = _HookedLock(ref, hook)  # type: ignore[assignment]  # test double
    client.set_active(False)
    client.start()
    try:
        client.wait_first_snapshot(3)
        assert fired.is_set()
        client.refresh()
        gatherer = spawner.servers[0]._gatherer
        # The refresh is processed after every Active message sent before it.
        _until(lambda: any(call[1] for call in gatherer.calls))
        assert any(c.active for c in spawner.servers[0]._clients)
    finally:
        client.close()


def test_a_server_that_handshakes_then_drops_us_is_retried_with_backoff(runtime_dir):
    paths.ensure_runtime_dir()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(paths.socket_path()))
    listener.listen()
    accepted: list[float] = []

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            accepted.append(time.monotonic())
            with conn, conn.makefile("rb") as reader:
                reader.readline()
                conn.sendall(encode(Hello(PROTOCOL, "v1")) + b"this is not a message\n")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = _client(Spawner(), spawn=lambda: None, backoff=(0.2,))
    try:
        _until(lambda: len(accepted) >= 3, timeout=5)
        gaps = [b - a for a, b in itertools.pairwise(accepted)]
        assert min(gaps) >= 0.15, gaps
    finally:
        client.close()
        with contextlib.suppress(OSError):
            listener.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept
        listener.close()
        thread.join(timeout=2)


def test_spawn_server_logs_to_the_state_dir_and_detaches(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    popens: list[tuple[list[str], dict]] = []
    files: list = []

    def fake_popen(argv, **kw):
        popens.append((argv, kw))
        files.append(kw["stdout"])
        return None

    monkeypatch.setattr(
        client_module,
        "subprocess",
        types.SimpleNamespace(Popen=fake_popen, DEVNULL=subprocess.DEVNULL),
    )
    client_module.spawn_server()
    client_module.spawn_server()

    log = tmp_path / "state" / "jailbee" / "state-service.log"
    assert log.parent.is_dir()
    assert len(popens) == 2
    argv, kw = popens[0]
    assert argv[1:] == ["-m", "jailbee", "_state-service"]
    assert kw["start_new_session"] is True
    assert kw["cwd"] == "/"
    assert kw["stdin"] == subprocess.DEVNULL
    assert kw["stdout"] is kw["stderr"]
    assert files[0].mode == "ab"
    assert all(f.closed for f in files)  # the parent keeps no handle
    assert log.exists()
