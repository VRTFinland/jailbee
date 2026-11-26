"""Action hosting over a real state service connection."""

import asyncio
import os
import threading

from jailbee.state_service.client import StateClient
from jailbee.state_service.protocol import RunAction
from jailbee.state_service.server import StateServer
from tests.test_state_client import _until
from tests.test_state_server import FakeGatherer, reap_servers, track_server  # noqa: F401


def test_action_streams_output_and_result(runtime_dir, tmp_path):
    class Child:
        def __init__(self, prompt_sock):
            prompt_sock.close()
            read, write = os.pipe()
            os.write(write, b"hello\n")
            os.close(write)
            self.stdout = os.fdopen(read, "rb")

        def wait(self):
            return 7

        def kill(self):
            pass

    from jailbee.state_service import paths

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
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                123,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: bool(client.action_entries().get(123) and client.action_entries()[123].finished))
        assert client.action_entries()[123].status == 7
        assert client.action_entries()[123].output == "hello\n"
    finally:
        client.close()


def test_action_events_are_broadcast_to_same_scope_clients(runtime_dir, tmp_path):
    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    from jailbee.state_service import paths

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

    def connect(identity):
        client = StateClient(
            tmp_path, dashboard_id=identity, version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        return client

    first, second = connect("one"), connect("two")
    try:
        first.run_action(
            RunAction(
                123,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "one",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: 123 in first.action_entries() and 123 in second.action_entries())
        _until(
            lambda: first.action_entries()[123].finished and second.action_entries()[123].finished
        )
        assert first.action_entries()[123].output == second.action_entries()[123].output
    finally:
        first.close()
        second.close()


def test_late_client_gets_finished_action_and_can_dismiss_for_all(runtime_dir, tmp_path):
    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 0

        def kill(self):
            pass

    from jailbee.state_service import paths

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

    def connect(identity):
        client = StateClient(
            tmp_path, dashboard_id=identity, version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        return client

    first = connect("one")
    first.run_action(
        RunAction(321, ["ls"], str(tmp_path), "target", False, "one", "none", str(tmp_path), "repo")
    )
    _until(
        lambda: first.action_entries().get(321) is not None and first.action_entries()[321].finished
    )
    late = connect("late")
    try:
        _until(lambda: 321 in late.action_entries() and late.action_entries()[321].finished)
        assert late.action_entries()[321].output == first.action_entries()[321].output
        late.dismiss_action(321)
        _until(lambda: 321 not in first.action_entries() and 321 not in late.action_entries())
    finally:
        first.close()
        late.close()


def test_wire_action_messages_round_trip():
    from jailbee.state_service.protocol import (
        ActionEntry,
        ActionFinish,
        ActionOutput,
        ActionRejected,
        decode,
        encode,
    )

    messages = [
        RunAction(1, ["ls"], "/repo", "a", True, "dashboard", "none", "/repo", "r"),
        ActionEntry(1, ["ls"], "/repo", "a", True, "dashboard", "none", "/repo", "r"),
        ActionOutput(1, "text"),
        ActionFinish(1, 0),
        ActionRejected(1, "duplicate"),
    ]
    for message in messages:
        assert decode(encode(message)) == message


def test_question_is_cancelled_over_real_prompt_channel(runtime_dir, tmp_path):
    from jailbee.prompt_channel import Cancel, CommandEnd, Prompt
    from jailbee.state_service import paths

    answers = []

    class Child:
        stdout = None

        def __init__(self, sock):
            def ask():
                command = CommandEnd(sock)
                answers.append(command.ask(lambda n: Prompt(n, "confirm", "Proceed?")))
                command.close()

            self.thread = threading.Thread(target=ask)
            self.thread.start()

        def wait(self):
            self.thread.join(timeout=3)
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
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                17,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: bool(answers))
        assert isinstance(answers[0], Cancel)
        _until(
            lambda: (
                client.action_entries().get(17) is not None and client.action_entries()[17].finished
            )
        )
        assert client.action_entries()[17].cancelled
    finally:
        client.close()


def test_reconnect_replays_entries_in_repository_scope_and_rejects_duplicate(runtime_dir, tmp_path):
    from jailbee.state_service import paths

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

    def connect(identity, cwd_root=None):
        client = StateClient(
            cwd_root, dashboard_id=identity, version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        return client

    first = connect("one")
    request = RunAction(
        55, ["ls"], str(tmp_path), "target", False, "one", "none", str(tmp_path), "repo"
    )
    first.run_action(request)
    _until(
        lambda: first.action_entries().get(55) is not None and first.action_entries()[55].finished
    )
    first.close()
    other = connect("other", tmp_path / "another-repo")
    again = connect("one")
    try:
        _until(lambda: 55 in again.action_entries())
        assert 55 not in other.action_entries()
        assert again.action_entries()[55].finished
        again.run_action(request)
        _until(lambda: again.action_rejection(55) is not None)
        assert again.action_entries()[55].status == 0
    finally:
        other.close()
        again.close()


def test_large_escaped_output_is_bounded_and_replayed(runtime_dir, tmp_path):
    from jailbee.dashboard.runner import OUTPUT_DROPPED_MARKER, OUTPUT_KEEP_CHARS
    from jailbee.state_service import paths

    text = ('\x01\n\\"' * (OUTPUT_KEEP_CHARS // 2)) + "THE END"
    output_file = tmp_path / "output"
    output_file.write_bytes(text.encode())

    class Child:
        def __init__(self, sock):
            sock.close()
            self.stdout = output_file.open("rb")

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

    def connect():
        client = StateClient(
            None, dashboard_id="large", version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        return client

    client = connect()
    try:
        client.run_action(
            RunAction(
                99,
                ["ls", "\x01" * 12000],
                str(tmp_path),
                "target",
                False,
                "large",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(
            lambda: (
                client.action_entries().get(99) is not None and client.action_entries()[99].finished
            )
        )
        expected = OUTPUT_DROPPED_MARKER + text[-OUTPUT_KEEP_CHARS:]
        assert client.action_entries()[99].output == expected
    finally:
        client.close()
    again = connect()
    try:
        _until(lambda: 99 in again.action_entries())
        assert again.action_entries()[99].output == expected
    finally:
        again.close()


def test_slow_action_reader_is_dropped_without_blocking_other_client(
    runtime_dir, tmp_path, monkeypatch
):
    import socket

    from jailbee.state_service import paths
    from jailbee.state_service import server as server_module
    from jailbee.state_service.protocol import PROTOCOL, Hello, encode

    monkeypatch.setattr(server_module, "MAX_ACTION_BUFFER_BYTES", 32768, raising=False)
    source = tmp_path / "large-output"
    source.write_bytes(b"a" * (8 * 1024 * 1024))

    class Child:
        def __init__(self, sock):
            sock.close()
            self.stdout = source.open("rb")

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
    slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    slow.settimeout(5)
    slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    slow.connect(str(paths.socket_path()))
    slow.sendall(encode(Hello(PROTOCOL, "v1", None, "slow")))
    reader = slow.makefile("rb")
    assert reader.readline()
    healthy = StateClient(
        None, dashboard_id="healthy", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    healthy.start()
    try:
        healthy.wait_first_snapshot(3)
        slow.sendall(
            encode(
                RunAction(
                    441,
                    ["ls"],
                    str(tmp_path),
                    "target",
                    False,
                    "slow",
                    "none",
                    str(tmp_path),
                    "repo",
                )
            )
        )
        # Wait until output has saturated the receiver without consuming it.
        _until(lambda: server._actions.get(441) is not None and server._actions[441].finished)
        healthy.refresh()
        assert healthy.latest() is not None
        total = 0
        while chunk := reader.read(65536):
            total += len(chunk)
            assert total < 2 * 1024 * 1024
    finally:
        healthy.close()
        reader.close()
        slow.close()


def test_finished_action_survives_server_restart(runtime_dir, tmp_path):
    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema
    from jailbee.state_service import paths

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            return 7

        def kill(self):
            pass

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    paths.ensure_runtime_dir()

    def start_server():
        server = StateServer(
            FakeGatherer(),
            version="v1",
            engine=engine,
            action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
        )
        thread = threading.Thread(
            target=lambda: asyncio.run(server.serve(paths.socket_path())), daemon=True
        )
        thread.start()
        track_server(server, thread)
        _until(paths.socket_path().exists)
        return server, thread

    def start_client():
        client = StateClient(
            None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        return client

    server, thread = start_server()
    client = start_client()
    client.run_action(
        RunAction(
            123, ["ls"], str(tmp_path), "target", True, "dashboard", "none", str(tmp_path), "repo"
        )
    )
    _until(lambda: 123 in client.action_entries() and client.action_entries()[123].finished)
    client.close()
    server._stopping = True
    thread.join(timeout=5)
    assert not thread.is_alive()
    paths.socket_path().unlink(missing_ok=True)
    server, thread = start_server()
    client = start_client()
    try:
        _until(lambda: 123 in client.action_entries())
        assert client.action_entries()[123].status == 7
        client.dismiss_action(123)
        _until(lambda: 123 not in client.action_entries())
    finally:
        client.close()
    server._stopping = True
    thread.join(timeout=5)
    assert not thread.is_alive()
    paths.socket_path().unlink(missing_ok=True)
    start_server()
    client = start_client()
    try:
        assert 123 not in client.action_entries()
    finally:
        client.close()


def test_corrupt_persisted_action_does_not_prevent_service_start(tmp_path, caplog):
    from sqlalchemy import create_engine
    from sqlmodel import Session

    from jailbee.db import _ensure_schema
    from jailbee.db.models import ActionEntryRow

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    with Session(engine) as session:
        session.add(ActionEntryRow(entry_id=123, state="done", payload="not json"))
        session.commit()

    StateServer(FakeGatherer(), version="v1", engine=engine)
    assert "123" in caplog.text


def test_running_action_is_interrupted_after_restart(runtime_dir, tmp_path):
    from sqlalchemy import create_engine

    from jailbee.db import _ensure_schema
    from jailbee.state_service import paths

    release = threading.Event()

    class Child:
        stdout = None

        def __init__(self, sock):
            sock.close()

        def wait(self):
            release.wait(5)
            return 0

        def kill(self):
            release.set()

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    _ensure_schema(engine)
    paths.ensure_runtime_dir()
    server = StateServer(
        FakeGatherer(),
        version="v1",
        engine=engine,
        action_spawn=lambda *a, **kw: Child(kw["prompt_sock"]),
    )
    stopped = threading.Event()

    def run_server():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve(paths.socket_path()))
            stopped.set()
            release.wait(5)
            loop.run_until_complete(asyncio.gather(*server._action_tasks))
        finally:
            loop.close()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    track_server(server, thread)
    _until(paths.socket_path().exists)
    client = StateClient(
        None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
    )
    client.start()
    try:
        client.wait_first_snapshot(3)
        client.run_action(
            RunAction(
                345,
                ["ls"],
                str(tmp_path),
                "target",
                False,
                "dashboard",
                "none",
                str(tmp_path),
                "repo",
            )
        )
        _until(lambda: 345 in client.action_entries())
        client.close()
        server._stopping = True
        assert stopped.wait(5), "original server did not stop"
        paths.socket_path().unlink(missing_ok=True)
        replacement = StateServer(FakeGatherer(), version="v1", engine=engine)
        replacement_thread = threading.Thread(
            target=lambda: asyncio.run(replacement.serve(paths.socket_path())), daemon=True
        )
        replacement_thread.start()
        track_server(replacement, replacement_thread)
        _until(paths.socket_path().exists)
        client = StateClient(
            None, dashboard_id="dashboard", version="v1", spawn=lambda: None, backoff=(0.05,)
        )
        client.start()
        client.wait_first_snapshot(3)
        _until(lambda: 345 in client.action_entries())
        entry = client.action_entries()[345]
        assert entry.state == "interrupted"
        assert entry.finished
    finally:
        release.set()
        client.close()
