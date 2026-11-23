"""FIDO2 security-key login and the setup mode that registers keys (#109 ticket 12).

The HTTP seam: `TestClient` over `create_setup_app` (registration) and
`create_app` (login), with a software authenticator standing in for the
browser and key. The relying party comes from the request's `Origin` header,
falling back to `Host`, so the tests vary those headers as a reverse proxy
or a browser would.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("fido2")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jailbee.db import get_engine
from jailbee.web import auth_admin
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.protocol import CLOSE_SETUP_MODE
from jailbee.web.setup_app import SetupDeps, create_setup_app
from tests.fido2_authenticator import SoftAuthenticator
from tests.web_fakes import FakeClock, FakeSourceFactory, pin_sequence

ORIGIN = "https://jb.example.com"
PROXY_HOST = "127.0.0.1:8910"
"""What a reverse proxy typically puts in `Host` when it forwards to the server."""


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def setup_pin() -> PinHolder:
    return PinHolder(generate=pin_sequence())


@pytest.fixture
def setup_client(clock: FakeClock, setup_pin: PinHolder) -> TestClient:
    deps = SetupDeps(engine=get_engine(), pin=setup_pin, clock=clock, monotonic=clock.monotonic)
    return TestClient(create_setup_app(deps))


@pytest.fixture
def client(clock: FakeClock) -> TestClient:
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=PinHolder(generate=pin_sequence()),
    )
    return TestClient(create_app(deps))


def proxied(origin: str = ORIGIN) -> dict[str, str]:
    return {"Origin": origin, "Host": PROXY_HOST}


def register(
    setup_client: TestClient,
    setup_pin: PinHolder,
    key: SoftAuthenticator,
    name: str = "yubikey",
    headers: dict[str, str] | None = None,
) -> Any:
    begin = setup_client.post(
        "/api/setup/fido2/begin", json={"pin": setup_pin.current}, headers=headers or proxied()
    )
    assert begin.status_code == 200, begin.text
    body = begin.json()
    credential = key.create(body["options"])
    return setup_client.post(
        "/api/setup/fido2/complete",
        json={"challenge_id": body["challenge_id"], "credential": credential, "name": name},
        headers=headers or proxied(),
    )


def test_setup_mode_registers_a_key_with_the_setup_pin(
    setup_client: TestClient, setup_pin: PinHolder
) -> None:
    response = register(setup_client, setup_pin, SoftAuthenticator(ORIGIN), name="yubikey")

    assert response.status_code == 200, response.text
    [key] = auth_admin.list_fido2_keys(get_engine())
    assert key.name == "yubikey"
    assert response.json() == {"id": key.id, "name": "yubikey"}


def login(
    client: TestClient,
    key: SoftAuthenticator,
    headers: dict[str, str] | None = None,
    device: str = "laptop",
    **get_kwargs: Any,
) -> Any:
    begin = client.post("/api/auth/fido2/begin", headers=headers or proxied())
    assert begin.status_code == 200, begin.text
    body = begin.json()
    return client.post(
        "/api/auth",
        json={
            "method": "fido2",
            "challenge_id": body["challenge_id"],
            "credential": key.get(body["options"], **get_kwargs),
            "device_name": device,
        },
        headers=headers or proxied(),
    )


def test_a_registered_key_logs_in_on_the_normal_server(
    setup_client: TestClient, setup_pin: PinHolder, client: TestClient
) -> None:
    key = SoftAuthenticator(ORIGIN)
    register(setup_client, setup_pin, key)

    response = login(client, key)

    assert response.status_code == 200, response.text
    token = response.json()["token"]
    session = client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
    assert session.json()["device_name"] == "laptop"


def test_the_relying_party_is_the_origin_host_not_the_proxied_host(
    setup_client: TestClient, setup_pin: PinHolder
) -> None:
    begin = setup_client.post(
        "/api/setup/fido2/begin", json={"pin": setup_pin.current}, headers=proxied()
    )

    assert begin.json()["options"]["rp"]["id"] == "jb.example.com"


def test_without_an_origin_header_the_host_header_names_the_relying_party(
    setup_client: TestClient, setup_pin: PinHolder, client: TestClient
) -> None:
    # `TestClient` speaks plain http, so the fallback origin is http://<Host>.
    key = SoftAuthenticator("http://localhost:8910")
    headers = {"Host": "localhost:8910"}

    registered = register(setup_client, setup_pin, key, headers=headers)
    begin = client.post("/api/auth/fido2/begin", headers=headers)

    assert registered.status_code == 200, registered.text
    assert begin.json()["options"]["rpId"] == "localhost"
    assert login(client, key, headers=headers).status_code == 200


def test_a_key_registered_for_one_domain_is_not_offered_at_another(
    setup_client: TestClient, setup_pin: PinHolder, client: TestClient
) -> None:
    register(setup_client, setup_pin, SoftAuthenticator(ORIGIN))

    begin = client.post("/api/auth/fido2/begin", headers=proxied("https://other.example.org"))

    assert begin.status_code == 404


def test_a_signature_from_another_origin_is_refused_and_audited(
    setup_client: TestClient, setup_pin: PinHolder, client: TestClient
) -> None:
    key = SoftAuthenticator(ORIGIN)
    register(setup_client, setup_pin, key)
    key.origin = "https://evil.example.net"  # a phishing page relaying our challenge

    response = login(client, key, device="phish")

    assert response.status_code == 401
    events = [(e.event, e.method, e.device_name) for e in auth_admin.recent_events(get_engine())]
    assert ("login_failed", "fido2", "phish") in events


def test_a_successful_key_login_is_audited(
    setup_client: TestClient, setup_pin: PinHolder, client: TestClient
) -> None:
    key = SoftAuthenticator(ORIGIN)
    register(setup_client, setup_pin, key, name="yubikey")

    login(client, key, device="laptop")

    events = [(e.event, e.method, e.device_name) for e in auth_admin.recent_events(get_engine())]
    assert ("key_added", "fido2", "yubikey") in events
    assert events[-1] == ("login", "fido2", "laptop")


def test_a_challenge_completes_only_once(
    setup_client: TestClient, setup_pin: PinHolder, client: TestClient
) -> None:
    key = SoftAuthenticator(ORIGIN)
    register(setup_client, setup_pin, key)
    begin = client.post("/api/auth/fido2/begin", headers=proxied()).json()
    body = {
        "method": "fido2",
        "challenge_id": begin["challenge_id"],
        # Counter 0, as many passkeys send: only the challenge store stops the replay.
        "credential": key.get(begin["options"], counter=0),
        "device_name": "laptop",
    }

    first = client.post("/api/auth", json=body, headers=proxied())
    replay = client.post("/api/auth", json=body, headers=proxied())

    assert (first.status_code, replay.status_code) == (200, 401)


def test_a_signature_counter_that_goes_backwards_is_refused_as_a_clone(
    setup_client: TestClient, setup_pin: PinHolder, client: TestClient
) -> None:
    key = SoftAuthenticator(ORIGIN)
    register(setup_client, setup_pin, key)

    assert login(client, key, counter=7).status_code == 200
    assert login(client, key, counter=7).status_code == 401
    assert login(client, key, counter=8).status_code == 200


def test_auth_methods_advertises_fido2_once_a_key_exists(
    setup_client: TestClient, setup_pin: PinHolder, client: TestClient
) -> None:
    assert "fido2" not in client.get("/api/auth/methods").json()["methods"]

    register(setup_client, setup_pin, SoftAuthenticator(ORIGIN))

    assert client.get("/api/auth/methods").json()["methods"] == ["pin", "fido2"]


def test_the_normal_server_cannot_register_keys(client: TestClient) -> None:
    begin = client.post("/api/setup/fido2/begin", json={"pin": "100000"}, headers=proxied())

    assert begin.status_code == 404


def test_a_wrong_setup_pin_starts_no_registration_and_spends_the_pin(
    setup_client: TestClient, setup_pin: PinHolder
) -> None:
    before = setup_pin.current

    begin = setup_client.post("/api/setup/fido2/begin", json={"pin": "999999"}, headers=proxied())

    assert begin.status_code == 401
    assert setup_pin.current != before


def test_setup_pin_guessing_is_limited_and_says_when_to_retry(
    setup_client: TestClient, clock: FakeClock
) -> None:
    def guess() -> tuple[int, str | None]:
        r = setup_client.post("/api/setup/fido2/begin", json={"pin": "999999"}, headers=proxied())
        return r.status_code, r.headers.get("Retry-After")

    assert [guess()[0] for _ in range(5)] == [401] * 5
    clock.advance(timedelta(seconds=15))

    assert guess() == (429, "45")


def test_dashboard_mode_is_public_and_not_cached(client: TestClient) -> None:
    response = client.get("/api/mode")
    assert response.status_code == 200
    assert response.json() == {"mode": "dashboard"}
    assert response.headers["cache-control"] == "no-store"


def test_setup_mode_lists_nothing_but_its_mode_and_registration(setup_client: TestClient) -> None:
    response = setup_client.get("/api/mode")
    assert response.json() == {"mode": "setup"}
    assert response.headers["cache-control"] == "no-store"
    assert setup_client.get("/api/auth/methods").status_code == 404
    assert setup_client.post("/api/auth", json={}).status_code == 404


def test_setup_mode_sends_the_browser_to_the_setup_view(
    tmp_path: Any, clock: FakeClock, setup_pin: PinHolder
) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>JailBee</title>")
    deps = SetupDeps(engine=get_engine(), pin=setup_pin, clock=clock, static_dir=tmp_path)
    browser = TestClient(create_setup_app(deps))

    root = browser.get("/", follow_redirects=False)
    page = browser.get("/index.html")

    assert root.headers["location"] == "index.html#/setup"
    assert "JailBee" in page.text


def test_pin_guessing_does_not_lock_out_a_security_key(
    setup_client: TestClient, setup_pin: PinHolder, client: TestClient
) -> None:
    key = SoftAuthenticator(ORIGIN)
    register(setup_client, setup_pin, key)
    for _ in range(6):
        last = client.post(
            "/api/auth", json={"method": "pin", "pin": "x", "device_name": "x"}, headers=proxied()
        )
    assert last.status_code == 429

    assert login(client, key).status_code == 200


def test_setup_mode_closes_the_dashboard_socket_with_the_setup_code(
    setup_client: TestClient,
) -> None:
    # An already-open signed-in tab reconnects its socket; the close code is
    # how it learns to switch to the setup view instead of retrying forever.
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        setup_client.websocket_connect("/api/ws") as ws,
    ):
        ws.receive_json()
    assert closed.value.code == CLOSE_SETUP_MODE == 4003
