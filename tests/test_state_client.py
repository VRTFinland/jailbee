"""StateClient against a real StateServer on a real socket."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from jailbee.dashboard import RepoGroup
from jailbee.lifecycle import ContainerInfo
from jailbee.state_service import StateServiceUnavailable, paths
from jailbee.state_service.client import DISCONNECTED, StateClient
from jailbee.state_service.protocol import Snapshot
from jailbee.state_service.server import StateServer
from tests.test_state_server import T0, FakeGatherer


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


def test_client_reconnects_after_the_server_goes_away(runtime_dir):
    spawner = Spawner()
    client = _client(spawner)
    try:
        client.wait_first_snapshot(3)
        spawner.servers[0]._stopping = True  # what a crash looks like from outside
        _until(lambda: client.status() == DISCONNECTED or len(spawner.servers) == 2)
        _until(lambda: len(spawner.servers) == 2 and client.status() is None)
    finally:
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
