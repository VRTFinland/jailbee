"""Action hosting over a real state service connection."""

import asyncio
import os
import threading

from jailbee.state_service.client import StateClient
from jailbee.state_service.protocol import ActionFinish, ActionOutput, RunAction
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
        events = []

        def finished():
            events.extend(client.drain_action_events())
            return any(isinstance(e, ActionFinish) for e in events)

        _until(finished)
        assert any(isinstance(e, ActionOutput) and e.chunk == "hello\n" for e in events)
        assert next(e for e in events if isinstance(e, ActionFinish)).status == 7
        assert client.action_entries()[123].output == "hello\n"
    finally:
        client.close()


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


def test_reconnect_replays_only_origin_entries_and_rejects_duplicate(runtime_dir, tmp_path):
    from jailbee.state_service import paths
    from jailbee.state_service.protocol import ActionRejected

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

    def connect(identity):
        client = StateClient(
            None, dashboard_id=identity, version="v1", spawn=lambda: None, backoff=(0.05,)
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
    other = connect("other")
    again = connect("one")
    try:
        _until(lambda: 55 in again.action_entries())
        assert 55 not in other.action_entries()
        assert again.action_entries()[55].finished
        again.drain_action_events()
        again.run_action(request)
        events = []

        def rejected():
            events.extend(again.drain_action_events())
            return any(isinstance(event, ActionRejected) for event in events)

        _until(rejected)
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
