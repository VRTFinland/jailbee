"""Reconnect and bounded event history over the state service socket."""

import asyncio
import threading

from jailbee.state_service import paths
from jailbee.state_service.client import StateClient
from jailbee.state_service.protocol import ActionRejected, RunAction
from jailbee.state_service.server import StateServer
from tests.test_state_client import _until
from tests.test_state_server import FakeGatherer, reap_servers, track_server  # noqa: F401


def test_reconnect_replaces_entries_dismissed_while_offline(runtime_dir, tmp_path, monkeypatch):
    from jailbee.state_service import client as client_module

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(), version="v1", action_spawn=lambda *a, **kw: Child(kw["prompt_sock"])
    )
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
    )
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    gate = threading.Event()
    original = client_module._try_connect

    def connect(path):
        if threading.current_thread().name == "offline-client" and not gate.is_set():
            return None
        return original(path)

    first = StateClient(
        None,
        dashboard_id="one",
        version="v1",
        spawn=lambda: None,
        backoff=(0.05,),
        connect_timeout=0.1,
    )
    first.start()
    second = StateClient(
        None, dashboard_id="two", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    second.start()
    try:
        first.wait_first_snapshot(3)
        second.wait_first_snapshot(3)
        first.run_action(
            RunAction(55, ["ls"], str(tmp_path), "x", False, "one", "none", str(tmp_path), "r")
        )
        _until(
            lambda: (
                first.action_entries().get(55) is not None and first.action_entries()[55].finished
            )
        )
        _until(
            lambda: (
                second.action_entries().get(55) is not None and second.action_entries()[55].finished
            )
        )
        monkeypatch.setattr(client_module, "_try_connect", connect)
        first._thread.name = "offline-client"
        first._sock.shutdown(2)
        _until(lambda: first.status() is not None)
        second.dismiss_action(55)
        _until(lambda: 55 not in second.action_entries())
        gate.set()
        _until(lambda: first.status() is None and 55 not in first.action_entries())
    finally:
        gate.set()
        first.close()
        second.close()


def test_action_event_history_is_bounded(runtime_dir, tmp_path):
    from tests.test_state_client import Spawner

    spawner = Spawner()
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=spawner, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        for entry_id in range(400):
            client.run_action(
                RunAction(
                    entry_id, ["ls"], str(tmp_path), "x", False, "wrong", "none", str(tmp_path), "r"
                )
            )
        _until(lambda: client.action_rejection(399) is not None)
        events = client.drain_action_events()
        assert len(events) <= 256
        assert isinstance(events[-1], ActionRejected)
        assert events[-1].entry_id == 399
    finally:
        client.close()


def test_replay_many_large_persisted_entries(runtime_dir, tmp_path):
    from sqlalchemy import create_engine
    from jailbee.db import _ensure_schema
    from jailbee.db.actions import save
    from jailbee.state_service.protocol import ActionEntry

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    text = "a" * 64000
    for index in range(40):
        save(engine, ActionEntry(index + 1, ["ls"], str(tmp_path), "target", False,
                                 "origin", "none", str(tmp_path), "repo", output=text,
                                 finished=True, status=0, state="done"))
    paths.ensure_runtime_dir()
    server = StateServer(FakeGatherer(), version="v1", engine=engine)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True)
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(None, dashboard_id="late", version="v1", spawn=lambda: None, backoff=(0.05,))
    client.start()
    try:
        _until(lambda: len(client.action_entries()) == 40, timeout=8)
        assert all(entry.output == text and entry.finished for entry in client.action_entries().values())
    finally:
        client.close()
