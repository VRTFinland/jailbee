"""TOTP login, the PIN switch, revocation and the login audit (#109 ticket 11).

The HTTP seam (`TestClient` over `create_app`) with a fake clock and the
per-test state database. Keys and sessions are managed through
`jailbee.web.auth_admin`, the module the CLI drives, since the server itself
exposes no management endpoint.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pyotp")

import pyotp
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jailbee.db import get_engine
from jailbee.web import auth_admin
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.protocol import CLOSE_UNAUTHORIZED, PROTOCOL_VERSION
from tests.web_fakes import START, FakeClock, FakeSourceFactory, pin_sequence, snapshot


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def pin() -> PinHolder:
    return PinHolder(generate=pin_sequence())


@pytest.fixture
def client(clock: FakeClock, pin: PinHolder) -> TestClient:
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
    )
    return TestClient(create_app(deps))


def add_key(clock: FakeClock, name: str = "phone") -> str:
    return auth_admin.add_totp_key(get_engine(), name, clock=clock).secret


def code(secret: str, clock: FakeClock) -> str:
    return str(pyotp.TOTP(secret).at(clock.now))


def wrong_code(secret: str, clock: FakeClock) -> str:
    """A code no step around ``clock`` accepts."""
    totp = pyotp.TOTP(secret)
    near = {str(totp.at(clock.now + timedelta(seconds=d))) for d in (-30, 0, 30)}
    return next(c for c in ("000000", "111111", "222222", "333333") if c not in near)


def totp_login(client: TestClient, totp_code: str, device: str = "tablet") -> int:
    response = client.post(
        "/api/auth", json={"method": "totp", "code": totp_code, "device_name": device}
    )
    return response.status_code


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_a_current_totp_code_logs_in(client: TestClient, clock: FakeClock) -> None:
    secret = add_key(clock)

    response = client.post(
        "/api/auth", json={"method": "totp", "code": code(secret, clock), "device_name": "tablet"}
    )

    assert response.status_code == 200, response.text
    session = client.get("/api/session", headers=bearer(response.json()["token"]))
    assert session.json()["device_name"] == "tablet"


def test_a_totp_code_opens_only_one_session(client: TestClient, clock: FakeClock) -> None:
    secret = add_key(clock)
    used = code(secret, clock)
    assert totp_login(client, used) == 200

    clock.advance(timedelta(seconds=5))

    assert totp_login(client, used) == 401


def test_a_code_from_minutes_ago_is_refused(client: TestClient, clock: FakeClock) -> None:
    secret = add_key(clock)
    stale = code(secret, clock)

    clock.advance(timedelta(minutes=2))

    assert totp_login(client, stale) == 401


def test_any_stored_key_logs_in(client: TestClient, clock: FakeClock) -> None:
    add_key(clock, "phone")
    second = add_key(clock, "spare")

    assert totp_login(client, code(second, clock)) == 200


def test_login_view_offers_totp_once_a_key_exists(client: TestClient, clock: FakeClock) -> None:
    assert client.get("/api/auth/methods").json() == {"methods": ["pin"], "auth_disabled": False}

    add_key(clock)

    assert client.get("/api/auth/methods").json() == {
        "methods": ["pin", "totp"],
        "auth_disabled": False,
    }


def test_with_the_pin_off_even_the_right_pin_is_refused(
    client: TestClient, clock: FakeClock, pin: PinHolder
) -> None:
    add_key(clock)
    auth_admin.set_pin_enabled(get_engine(), False)

    response = client.post(
        "/api/auth", json={"method": "pin", "pin": pin.current, "device_name": "tablet"}
    )

    assert response.status_code == 401
    assert client.get("/api/auth/methods").json() == {"methods": ["totp"], "auth_disabled": False}


def test_the_pin_cannot_be_turned_off_without_a_totp_key() -> None:
    with pytest.raises(auth_admin.AuthAdminError, match="no TOTP key"):
        auth_admin.set_pin_enabled(get_engine(), False)

    assert auth_admin.pin_enabled(get_engine())


def test_the_last_totp_key_stays_while_the_pin_is_off(clock: FakeClock) -> None:
    engine = get_engine()
    key = auth_admin.add_totp_key(engine, "phone", clock=clock).key
    auth_admin.set_pin_enabled(engine, False)

    with pytest.raises(auth_admin.AuthAdminError, match="last TOTP key"):
        auth_admin.remove_totp_key(engine, key.id)

    assert auth_admin.list_totp_keys(engine) == [key]


def test_a_revoked_session_is_refused_on_its_next_request(
    client: TestClient, clock: FakeClock, pin: PinHolder
) -> None:
    response = client.post(
        "/api/auth", json={"method": "pin", "pin": pin.current, "device_name": "lost tablet"}
    )
    token = response.json()["token"]
    [session] = auth_admin.list_sessions(get_engine())

    auth_admin.revoke_session(get_engine(), session.id, clock=clock)

    assert client.get("/api/session", headers=bearer(token)).status_code == 401


def test_logins_logouts_and_revocations_are_audited(
    client: TestClient, clock: FakeClock, pin: PinHolder
) -> None:
    secret = add_key(clock)
    assert totp_login(client, wrong_code(secret, clock), device="thief") == 401
    clock.advance(timedelta(seconds=1))
    response = client.post(
        "/api/auth", json={"method": "pin", "pin": pin.current, "device_name": "boox"}
    )
    clock.advance(timedelta(seconds=1))
    client.post("/api/logout", headers=bearer(response.json()["token"]))
    clock.advance(timedelta(seconds=1))
    assert totp_login(client, code(secret, clock), device="phone") == 200
    [phone] = auth_admin.list_sessions(get_engine())
    clock.advance(timedelta(seconds=1))
    auth_admin.revoke_session(get_engine(), phone.id, clock=clock)

    events = [
        (e.event, e.method, e.device_name, int((e.at - START).total_seconds()))
        for e in auth_admin.recent_events(get_engine())
    ]
    assert events == [
        ("login_failed", "totp", "thief", 0),
        ("login", "pin", "boox", 1),
        ("logout", "", "boox", 2),
        ("login", "totp", "phone", 3),
        ("revoke", "cli", "phone", 4),
    ]


def test_an_open_socket_of_a_revoked_session_gets_no_more_pages(
    clock: FakeClock, pin: PinHolder
) -> None:
    factory = FakeSourceFactory()
    deps = WebDeps(
        snapshot_source=factory,
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
    )
    with TestClient(create_app(deps)) as client:
        body = {"method": "pin", "pin": pin.current, "device_name": "lost tablet"}
        token = client.post("/api/auth", json=body).json()["token"]
        with client.websocket_connect("/api/ws") as ws:
            ws.send_text(json.dumps({"type": "auth", "v": PROTOCOL_VERSION, "token": token}))
            assert ws.receive_json()["type"] == "hello"
            [session] = auth_admin.list_sessions(get_engine())

            auth_admin.revoke_session(get_engine(), session.id, clock=clock)
            factory.only.push(snapshot("alpha-one", seq=1))

            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
    assert closed.value.code == CLOSE_UNAUTHORIZED


def bad_pins(client: TestClient, pin: PinHolder, count: int = 5) -> list[int]:
    def guess() -> int:
        wrong = "000000" if pin.current != "000000" else "999999"
        body = {"method": "pin", "pin": wrong, "device_name": "x"}
        return client.post("/api/auth", json=body).status_code

    return [guess() for _ in range(count)]


def test_pin_guessing_does_not_lock_out_totp(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    secret = add_key(clock)
    assert bad_pins(client, pin, 6) == [401] * 5 + [429]

    assert totp_login(client, code(secret, clock)) == 200


def test_pin_guessing_does_not_lock_out_a_login_link(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    from jailbee.web import login_link

    assert bad_pins(client, pin, 6)[-1] == 429
    token = login_link.mint(get_engine(), clock=clock)

    body = {"method": "link", "token": token, "device_name": "laptop"}
    assert client.post("/api/auth", json=body).status_code == 200


def test_totp_guessing_is_limited_on_its_own(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    secret = add_key(clock)
    wrong = wrong_code(secret, clock)

    assert [totp_login(client, wrong) for _ in range(6)] == [401] * 5 + [429]
    body = {"method": "pin", "pin": pin.current, "device_name": "x"}
    assert client.post("/api/auth", json=body).status_code == 200
