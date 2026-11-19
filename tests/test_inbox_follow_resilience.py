"""Follower recovery through the inbox CLI and a local broker socket."""

import socket
import threading

import pytest
from typer.testing import CliRunner

from jailbee.inbox import container_cmd, socket_cli
from jailbee.orchestration import wire


class StopFollowing(Exception):
    pass


def test_missing_socket_sleeps_silently_and_caps_backoff(monkeypatch, tmp_path):
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setenv("JAILBEE_BROKER_RETRY_SECONDS", "0")
    pauses = []

    def sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 8:
            raise StopFollowing

    monkeypatch.setattr(socket_cli.time, "sleep", sleep)
    result = CliRunner().invoke(container_cmd.inbox_app, ["follow"])
    assert isinstance(result.exception, StopFollowing)
    assert result.output == ""
    assert pauses == [0.5, 1, 2, 4, 8, 16, 30, 30]


def test_follow_recovers_after_role_refusal_and_disconnect(monkeypatch, tmp_path):
    path = tmp_path / "broker.sock"
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_SOCKET", str(path))
    monkeypatch.setenv("JAILBEE_BROKER_RETRY_SECONDS", "0")
    pauses = []
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    item = {"id": "m1", "from": "kid", "kind": "note", "body": "hello"}

    def serve():
        with server:
            for response in (
                wire.fail(wire.NOT_ORCHESTRATED, "no role"),
                wire.ok({"kind": "message", "item": item}),
                wire.ok({"kind": "message", "item": item}),
            ):
                conn, _ = server.accept()
                with conn, conn.makefile("rb") as reader:
                    assert wire.decode_request(wire.read_line(reader)).op == "follow"
                    conn.sendall(wire.encode_response(response))

    thread = None

    def sleep(seconds):
        nonlocal thread
        pauses.append(seconds)
        if len(pauses) == 1:
            server.bind(str(path))
            server.listen()
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
        elif len(pauses) == 4:
            raise StopFollowing

    monkeypatch.setattr(socket_cli.time, "sleep", sleep)
    result = CliRunner().invoke(container_cmd.inbox_app, ["follow"])
    assert isinstance(result.exception, StopFollowing)
    assert result.output.count("JailBee inbox: m1 from kid (note): hello") == 2
    assert "no role" not in result.output
    assert pauses == [0.5, 1, 0.5, 0.5]
    assert thread is not None
    thread.join(timeout=1)
    assert not thread.is_alive()


@pytest.mark.parametrize("response", [wire.fail(wire.VERSION_ERROR, "version"), b"not json\n"])
def test_follow_retries_protocol_errors_silently(monkeypatch, tmp_path, response):
    path = tmp_path / "broker.sock"
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_SOCKET", str(path))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen()

    def serve():
        with server:
            conn, _ = server.accept()
            with conn, conn.makefile("rb") as reader:
                wire.read_line(reader)
                conn.sendall(response if isinstance(response, bytes) else wire.encode_response(response))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    def sleep(seconds):
        raise StopFollowing

    monkeypatch.setattr(socket_cli.time, "sleep", sleep)
    result = CliRunner().invoke(container_cmd.inbox_app, ["follow"])
    assert isinstance(result.exception, StopFollowing)
    assert result.output == ""
    thread.join(timeout=1)
    assert not thread.is_alive()
