"""Automatic session names and renaming (#152 ticket 25).

Name derivation is a pure function; the login and rename flows go through the
HTTP seam like `test_web_auth.py`.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from jailbee.db import get_engine
from jailbee.db.models import WebAuthEvent
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.device_name import derive_device_name
from tests.web_fakes import FakeClock, FakeSourceFactory, pin_sequence

FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
CHROME_ANDROID = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
)
EDGE_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
)
CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
SAFARI_IPAD = (
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
CHROME_CROS = (
    "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADLESS_CHROME_LINUX = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) HeadlessChrome/126.0.0.0 Safari/537.36"
)


@pytest.mark.parametrize(
    ("agent", "name"),
    [
        (FIREFOX_LINUX, "Firefox on Linux"),
        (SAFARI_IPHONE, "Safari on iPhone"),
        (SAFARI_IPAD, "Safari on iPad"),
        (CHROME_ANDROID, "Chrome on Android"),
        (EDGE_WINDOWS, "Edge on Windows"),
        (CHROME_MAC, "Chrome on macOS"),
        (CHROME_CROS, "Chrome on ChromeOS"),
        (HEADLESS_CHROME_LINUX, "Chrome on Linux"),
        ("", "Browser"),
        (None, "Browser"),
        ("curl/8.5.0", "Browser"),
    ],
)
def test_the_name_is_derived_from_the_user_agent(agent: str | None, name: str) -> None:
    assert derive_device_name(agent) == name


def test_an_unknown_system_still_names_the_browser() -> None:
    assert derive_device_name("Mozilla/5.0 (PlayStation 5) Firefox/130.0") == "Firefox"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def pin() -> PinHolder:
    return PinHolder(generate=pin_sequence(), on_rotate=lambda new, why: None)


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


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def pin_login(client: TestClient, pin: PinHolder, agent: str, **extra: str) -> str:
    response = client.post(
        "/api/auth",
        json={"method": "pin", "pin": pin.current, **extra},
        headers={"User-Agent": agent},
    )
    assert response.status_code == 200, response.text
    token: str = response.json()["token"]
    return token


def test_login_without_a_device_name_names_the_session_from_the_user_agent(
    client: TestClient, pin: PinHolder
) -> None:
    token = pin_login(client, pin, FIREFOX_LINUX)

    assert client.get("/api/session", headers=bearer(token)).json()["device_name"] == (
        "Firefox on Linux"
    )


def test_an_old_client_that_sends_a_device_name_keeps_it(
    client: TestClient, pin: PinHolder
) -> None:
    token = pin_login(client, pin, FIREFOX_LINUX, device_name="kitchen tablet")

    assert client.get("/api/session", headers=bearer(token)).json()["device_name"] == (
        "kitchen tablet"
    )


def test_the_login_audit_row_carries_the_derived_name(
    client: TestClient, pin: PinHolder
) -> None:
    pin_login(client, pin, CHROME_ANDROID)

    with Session(get_engine()) as s:
        rows = s.exec(select(WebAuthEvent).where(WebAuthEvent.event == "login")).all()
    assert [r.device_name for r in rows] == ["Chrome on Android"]


def test_a_session_can_be_renamed(client: TestClient, pin: PinHolder) -> None:
    token = pin_login(client, pin, FIREFOX_LINUX)

    response = client.put(
        "/api/session", json={"device_name": "  hall tablet "}, headers=bearer(token)
    )

    assert response.status_code == 200
    assert response.json()["device_name"] == "hall tablet"
    assert client.get("/api/session", headers=bearer(token)).json()["device_name"] == "hall tablet"


def test_a_rename_is_audited(client: TestClient, pin: PinHolder) -> None:
    token = pin_login(client, pin, FIREFOX_LINUX)

    client.put("/api/session", json={"device_name": "hall tablet"}, headers=bearer(token))

    with Session(get_engine()) as s:
        rows = s.exec(select(WebAuthEvent).where(WebAuthEvent.event == "rename")).all()
    assert [r.device_name for r in rows] == ["hall tablet"]


def test_renaming_needs_a_session(client: TestClient) -> None:
    response = client.put("/api/session", json={"device_name": "x"})

    assert response.status_code == 401


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_a_rename_needs_a_sensible_name(client: TestClient, pin: PinHolder, name: str) -> None:
    token = pin_login(client, pin, FIREFOX_LINUX)

    response = client.put("/api/session", json={"device_name": name}, headers=bearer(token))

    assert response.status_code == 422
    assert client.get("/api/session", headers=bearer(token)).json()["device_name"] == (
        "Firefox on Linux"
    )


def test_a_rename_touches_only_that_session(client: TestClient, pin: PinHolder) -> None:
    first = pin_login(client, pin, FIREFOX_LINUX)
    second = pin_login(client, pin, CHROME_ANDROID)

    client.put("/api/session", json={"device_name": "renamed"}, headers=bearer(first))

    assert client.get("/api/session", headers=bearer(second)).json()["device_name"] == (
        "Chrome on Android"
    )
