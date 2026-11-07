"""Container clients retry the connect for a while (a broker restart is routine)."""

import pytest

from jailbee.inbox import socket_cli, transport
from jailbee.inbox.transport import BrokerUnreachable, Client
from jailbee.orchestration import wire
from tests.test_inbox_transport import FakeServer, path, sockdir  # noqa: F401


def test_connect_retries_until_the_broker_is_back(path):  # noqa: F811
    now = [0.0]
    started: list[FakeServer] = []

    def sleep(seconds: float) -> None:
        now[0] += seconds
        if now[0] >= 3 and not started:
            started.append(FakeServer(path, [lambda r: wire.ok({"ok": True})]))

    try:
        with Client(path, retry_for=10, clock=lambda: now[0], sleep=sleep) as c:
            assert c.call("list", {}, timeout=5) == {"ok": True}
        assert 3 <= now[0] < 10
    finally:
        for srv in started:
            srv.close()


def test_connect_gives_up_after_the_retry_window(path):  # noqa: F811
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with (
        Client(path, retry_for=10, clock=lambda: now[0], sleep=sleep) as c,
        pytest.raises(BrokerUnreachable),
    ):
        c.call("list", {}, timeout=1)
    assert 10 <= now[0] < 16  # waited the window, not much more


def test_missing_socket_keeps_its_own_message_after_retrying(path):  # noqa: F811
    now = [0.0]
    client = Client(
        path,
        retry_for=2,
        clock=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
        missing_message="socket is missing",
    )
    with client, pytest.raises(BrokerUnreachable, match="socket is missing"):
        client.call("list", {}, timeout=1)
    assert now[0] >= 2


def test_container_commands_retry_ten_seconds(monkeypatch, path):  # noqa: F811
    monkeypatch.setenv(transport.SOCKET_ENV, str(path))
    client = socket_cli._factory()
    try:
        assert client.retry_for == 10.0
    finally:
        client.close()
