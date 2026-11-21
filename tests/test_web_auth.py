"""Web dashboard login: one-time PIN, sliding session tokens (#109 ticket 01).

Everything goes through the HTTP seam (`TestClient` over `create_app`) with a
fake clock, deterministic PINs and the per-test temporary state database.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from jailbee.db import get_engine
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from tests.web_fakes import FakeClock, FakeSourceFactory, pin_sequence


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def rotations() -> list[tuple[str, str]]:
    return []


@pytest.fixture
def pin(rotations: list[tuple[str, str]]) -> PinHolder:
    return PinHolder(
        generate=pin_sequence(), on_rotate=lambda new, why: rotations.append((new, why))
    )


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


def login(client: TestClient, pin: str, device: str = "boox") -> str:
    response = client.post("/api/auth", json={"method": "pin", "pin": pin, "device_name": device})
    assert response.status_code == 200, response.text
    token: str = response.json()["token"]
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_pin_login_gives_a_session_named_after_the_device(
    client: TestClient, pin: PinHolder
) -> None:
    token = login(client, pin.current, device="kitchen tablet")

    me = client.get("/api/session", headers=bearer(token))

    assert me.status_code == 200
    assert me.json()["device_name"] == "kitchen tablet"


def test_a_pin_works_once_and_a_new_one_is_announced(
    client: TestClient, pin: PinHolder, rotations: list[tuple[str, str]]
) -> None:
    used = pin.current
    login(client, used)

    assert pin.current != used
    assert rotations == [(pin.current, "login")]
    again = client.post("/api/auth", json={"method": "pin", "pin": used, "device_name": "x"})
    assert again.status_code == 401


def test_a_wrong_guess_is_rejected_and_rotates_the_pin(
    client: TestClient, pin: PinHolder, rotations: list[tuple[str, str]]
) -> None:
    before = pin.current
    wrong = "000000" if before != "000000" else "999999"

    response = client.post("/api/auth", json={"method": "pin", "pin": wrong, "device_name": "x"})

    assert response.status_code == 401
    assert "token" not in response.json()
    assert pin.current != before
    assert rotations == [(pin.current, "wrong guess")]
    # The PIN shown before the wrong guess is gone too.
    stale = client.post("/api/auth", json={"method": "pin", "pin": before, "device_name": "x"})
    assert stale.status_code == 401


@pytest.mark.parametrize("device", ["", "   ", "x" * 101])
def test_login_needs_a_sensible_device_name(
    client: TestClient, pin: PinHolder, device: str
) -> None:
    current = pin.current
    response = client.post(
        "/api/auth", json={"method": "pin", "pin": current, "device_name": device}
    )

    assert response.status_code == 422
    assert pin.current == current, "a malformed request is not a guess"


def test_unknown_auth_method_is_refused(client: TestClient) -> None:
    response = client.post("/api/auth", json={"method": "totp", "code": "1", "device_name": "x"})

    assert response.status_code == 422


def test_auth_methods_lists_pin(client: TestClient) -> None:
    assert client.get("/api/auth/methods").json() == {"methods": ["pin"]}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "x"}])
def test_api_without_a_valid_token_is_rejected(client: TestClient, headers: dict[str, str]) -> None:
    assert client.get("/api/session", headers=headers).status_code == 401


def test_a_session_expires_after_thirty_idle_days(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    token = login(client, pin.current)

    clock.advance(timedelta(days=30, seconds=1))

    assert client.get("/api/session", headers=bearer(token)).status_code == 401
    clock.advance(timedelta(seconds=-2))  # back inside the window: the row is gone for good
    assert client.get("/api/session", headers=bearer(token)).status_code == 401


def test_each_use_slides_the_expiry(client: TestClient, pin: PinHolder, clock: FakeClock) -> None:
    token = login(client, pin.current)

    for _ in range(3):
        clock.advance(timedelta(days=20))
        assert client.get("/api/session", headers=bearer(token)).status_code == 200

    me = client.get("/api/session", headers=bearer(token)).json()
    assert me["last_used_at"] > me["created_at"]


def test_logout_ends_the_session(client: TestClient, pin: PinHolder) -> None:
    token = login(client, pin.current)
    other = login(client, pin.current, device="phone")

    assert client.post("/api/logout", headers=bearer(token)).status_code == 200

    assert client.get("/api/session", headers=bearer(token)).status_code == 401
    assert client.get("/api/session", headers=bearer(other)).status_code == 200


def test_repeated_guessing_is_rate_limited(
    client: TestClient, pin: PinHolder, clock: FakeClock
) -> None:
    def guess() -> int:
        wrong = "000000" if pin.current != "000000" else "999999"
        body = {"method": "pin", "pin": wrong, "device_name": "x"}
        return client.post("/api/auth", json=body).status_code

    assert [guess() for _ in range(5)] == [401] * 5
    assert guess() == 429
    # Even the right PIN waits out the window.
    body = {"method": "pin", "pin": pin.current, "device_name": "x"}
    assert client.post("/api/auth", json=body).status_code == 429

    clock.advance(timedelta(seconds=61))
    assert client.post("/api/auth", json=body).status_code == 200


def test_static_frontend_is_served_at_the_root(tmp_path, clock: FakeClock, pin: PinHolder) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>JailBee</title>")
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        static_dir=tmp_path,
    )
    client = TestClient(create_app(deps))

    assert "JailBee" in client.get("/").text
    assert client.get("/api/session").status_code == 401, "static mount must not shadow /api"
