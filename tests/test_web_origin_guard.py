"""Cross-origin defences: the WebSocket `Origin` check and the auth-off `Host` check.

HTTP and WebSocket seams (`TestClient` over `create_app`). A browser always
sends `Origin` on a WebSocket handshake, so a page on another site that opens
`/api/ws` is refused whatever the auth mode (cross-site WebSocket hijacking).
With login off, a request whose `Host` is not loopback or the listen address
is refused too (DNS rebinding), unless the user opted in to unauthenticated
remote access.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jailbee.db import get_engine
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.protocol import PROTOCOL_VERSION
from tests.web_fakes import FakeClock, FakeSourceFactory, pin_sequence


def make_client(
    base_url: str = "http://testserver", pin: PinHolder | None = None, **extra: object
) -> TestClient:
    clock = FakeClock()
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin or PinHolder(generate=pin_sequence()),
        **extra,  # type: ignore[arg-type]  # test helper forwards optional WebDeps fields
    )
    return TestClient(create_app(deps), base_url=base_url)


def hello(token: str = "anonymous") -> str:
    return json.dumps({"type": "auth", "v": PROTOCOL_VERSION, "token": token})


def ws_headers(client: TestClient, headers: dict[str, str]) -> dict[str, str]:
    """``headers`` plus the client's own `Host`: Starlette's test WebSocket
    always sends ``testserver`` otherwise, whatever ``base_url`` says."""
    return {"host": client.base_url.netloc.decode(), **headers}


def ws_greeting(client: TestClient, headers: dict[str, str], token: str = "anonymous") -> str:
    """The first server message after an auth hello: ``hello`` or a close."""
    with client.websocket_connect("/api/ws", headers=ws_headers(client, headers)) as ws:
        ws.send_text(hello(token))
        return str(ws.receive_json()["type"])


def test_cross_origin_http_post_is_refused_with_auth_off() -> None:
    client = make_client("http://localhost:8910", auth_enabled=False)
    path = "/api/repos/alpha/sessions/claude/example/messages"
    response = client.post(
        path, content='{"text":"hello"}', headers={"origin": "https://evil.example"}
    )
    assert response.status_code == 403
    response = client.post(
        path, content='{"text":"hello"}', headers={"origin": "http://localhost:8910"}
    )
    assert response.status_code != 403


# --- WebSocket Origin (always) ----------------------------------------------


def test_a_cross_site_websocket_is_refused_with_login_off() -> None:
    client = make_client("http://localhost:8910", auth_enabled=False)

    with pytest.raises(WebSocketDisconnect):
        ws_greeting(client, {"origin": "https://evil.example"})


def test_a_cross_site_websocket_is_refused_even_with_a_valid_session() -> None:
    pin = PinHolder(generate=pin_sequence())
    client = make_client("https://jb.example.com", pin=pin)
    body = {"method": "pin", "pin": pin.current, "device_name": "laptop"}
    token = client.post("/api/auth", json=body).json()["token"]
    assert ws_greeting(client, {"origin": "https://jb.example.com"}, token) == "hello"

    with pytest.raises(WebSocketDisconnect):
        ws_greeting(client, {"origin": "https://evil.example"}, token)


def test_a_same_origin_websocket_is_accepted() -> None:
    client = make_client("http://localhost:8910", auth_enabled=False)

    assert ws_greeting(client, {"origin": "http://localhost:8910"}) == "hello"


def test_a_websocket_without_origin_is_accepted() -> None:
    # Non-browser clients send no Origin; they carry no ambient credentials.
    client = make_client("http://localhost:8910", auth_enabled=False)

    assert ws_greeting(client, {}) == "hello"


def test_a_websocket_on_another_port_of_the_same_host_is_refused() -> None:
    client = make_client("http://localhost:8910", auth_enabled=False)

    with pytest.raises(WebSocketDisconnect):
        ws_greeting(client, {"origin": "http://localhost:3000"})


def test_behind_a_proxy_at_the_domain_root_the_origin_matches_the_forwarded_host() -> None:
    # The proxy passes the public Host and Origin (docs/web-dashboard.md); the
    # default HTTPS port is implicit in both.
    client = make_client("https://jb.example.com")

    headers = ws_headers(client, {"origin": "https://jb.example.com"})
    with client.websocket_connect("/api/ws", headers=headers) as ws:
        ws.send_text(hello("not-a-session"))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 4001, "the origin passed; only the bad token is refused"


# --- Host with login off (DNS rebinding) ------------------------------------


@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:8910", "http://127.0.0.1:8910", "http://127.1.2.3", "http://[::1]:8910"],
)
def test_auth_off_serves_loopback_hosts(base_url: str) -> None:
    client = make_client(base_url, auth_enabled=False)

    assert client.get("/api/session").status_code == 200


def test_auth_off_refuses_a_rebound_host_on_rest_and_websocket() -> None:
    client = make_client("http://attacker.example:8910", auth_enabled=False)

    assert client.get("/api/session").status_code == 403
    assert client.get("/api/auth/methods").status_code == 403
    with pytest.raises(WebSocketDisconnect):
        ws_greeting(client, {"origin": "http://attacker.example:8910"})


def test_auth_off_serves_the_configured_listen_address() -> None:
    client = make_client("http://192.168.1.5:8910", auth_enabled=False, listen="192.168.1.5")

    assert client.get("/api/session").status_code == 200


def test_auth_off_with_the_remote_opt_in_serves_any_host() -> None:
    client = make_client(
        "http://dash.lan:8910", auth_enabled=False, allow_unauthenticated_remote=True
    )

    assert client.get("/api/session").status_code == 200


def test_auth_on_serves_any_host() -> None:
    client = make_client("https://jb.example.com")

    assert client.get("/api/auth/methods").status_code == 200
