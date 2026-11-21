"""`/api/ws`: auth handshake, snapshot delivery and the state-service lifecycle.

Through the WebSocket seam (`TestClient.websocket_connect`) with a fake
snapshot source standing in for `StateClient`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jailbee.db import get_engine
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.protocol import PROTOCOL_VERSION
from tests.web_fakes import FakeClock, FakeSourceFactory, pin_sequence, snapshot, wait_until


def make_client(factory: FakeSourceFactory, *, grace: float = 60.0) -> tuple[TestClient, str]:
    """A started client (one event loop for the whole test) and a session token."""
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    deps = WebDeps(
        snapshot_source=factory,
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        grace_seconds=grace,
    )
    client = TestClient(create_app(deps))
    client.__enter__()
    body = {"method": "pin", "pin": pin.current, "device_name": "boox"}
    token: str = client.post("/api/auth", json=body).json()["token"]
    return client, token


@pytest.fixture
def factory() -> FakeSourceFactory:
    return FakeSourceFactory()


@pytest.fixture
def started(factory: FakeSourceFactory) -> Iterator[tuple[TestClient, str]]:
    client, token = make_client(factory)
    yield client, token
    client.__exit__(None, None, None)


def auth(token: str, v: int = PROTOCOL_VERSION) -> str:
    return json.dumps({"type": "auth", "v": v, "token": token})


def test_hello_names_the_device(started: tuple[TestClient, str]) -> None:
    client, token = started
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(auth(token))
        assert ws.receive_json() == {"type": "hello", "v": PROTOCOL_VERSION, "device_name": "boox"}


@pytest.mark.parametrize(
    ("first", "code"),
    [
        (auth("not-a-token"), 4001),
        ("not json", 4400),
        (json.dumps({"type": "subscribe", "v": PROTOCOL_VERSION}), 4400),
    ],
)
def test_socket_without_a_valid_login_is_closed(
    started: tuple[TestClient, str], factory: FakeSourceFactory, first: str, code: int
) -> None:
    client, _ = started
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(first)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == code
    assert factory.made == [], "an unauthenticated socket never opens the state service"


def test_an_older_browser_app_is_told_to_reload(started: tuple[TestClient, str]) -> None:
    client, token = started
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(auth(token, v=PROTOCOL_VERSION + 1))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4002


def test_front_page_arrives_on_every_snapshot(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(auth(token))
        ws.receive_json()  # hello

        factory.only.push(snapshot("alpha-one", "alpha-two", seq=1))
        first = ws.receive_json()
        factory.only.push(snapshot("alpha-two", seq=2))
        second = ws.receive_json()

    assert first["type"] == "front_page"
    assert first["v"] == PROTOCOL_VERSION
    assert [c["name"] for c in first["view"]["containers"]] == ["alpha-one", "alpha-two"]
    assert [c["name"] for c in second["view"]["containers"]] == ["alpha-two"]


def test_a_late_browser_gets_the_current_page_at_once() -> None:
    factory = FakeSourceFactory(initial=snapshot("alpha-one", seq=7))
    client, token = make_client(factory)
    try:
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(auth(token))
            ws.receive_json()  # hello
            page = ws.receive_json()
        assert page["view"]["seq"] == 7
    finally:
        client.__exit__(None, None, None)


def test_state_service_trouble_is_reported(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(auth(token))
        ws.receive_json()  # hello
        source = factory.only
        source._status = "state service unavailable: boom"
        source.push(snapshot("alpha-one"))
        status = ws.receive_json()

    assert status == {
        "type": "status",
        "v": PROTOCOL_VERSION,
        "message": "state service unavailable: boom",
    }


def test_state_service_opens_with_the_first_browser_only(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    assert factory.made == [], "no browser, no state-service connection"

    with client.websocket_connect("/api/ws") as one:
        one.send_text(auth(token))
        one.receive_json()
        assert factory.only.started
        with client.websocket_connect("/api/ws") as two:
            two.send_text(auth(token))
            two.receive_json()
        # The first browser is still there.
        assert not factory.only.closed

    assert len(factory.made) == 1


def test_state_service_closes_after_the_grace_period() -> None:
    factory = FakeSourceFactory()
    client, token = make_client(factory, grace=0.05)
    try:
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(auth(token))
            ws.receive_json()
        wait_until(lambda: factory.only.closed)

        # The next browser opens a fresh connection.
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(auth(token))
            ws.receive_json()
        assert len(factory.made) == 2
        assert factory.made[1].started and not factory.made[1].closed
    finally:
        client.__exit__(None, None, None)


def test_a_reload_within_the_grace_period_keeps_the_connection(
    started: tuple[TestClient, str], factory: FakeSourceFactory
) -> None:
    client, token = started
    for _ in range(2):
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(auth(token))
            ws.receive_json()

    assert len(factory.made) == 1
    assert not factory.only.closed


def test_server_shutdown_closes_the_connection(factory: FakeSourceFactory) -> None:
    client, token = make_client(factory)
    with client.websocket_connect("/api/ws") as ws:
        ws.send_text(auth(token))
        ws.receive_json()
    client.__exit__(None, None, None)

    assert factory.only.closed
