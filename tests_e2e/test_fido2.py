"""Security-key setup and login through Chrome's real WebAuthn implementation."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Page, Route, expect
from sqlmodel import Session, select

from jailbee.db.models import WebFido2Key
from jailbee.web import auth_admin
from tests.web_fakes import snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.flows import DERIVED_DEVICE, log_in_with_pin
from tests_e2e.server import E2EServer

if TYPE_CHECKING:
    from typing import Any

    from playwright.sync_api import CDPSession


@dataclass(frozen=True)
class VirtualKey:
    cdp: CDPSession
    authenticator_id: str

    def credential(self) -> dict[str, Any]:
        (credential,) = self.cdp.send(
            "WebAuthn.getCredentials", {"authenticatorId": self.authenticator_id}
        )["credentials"]
        return dict(credential)

    def set_sign_count(self, count: int) -> None:
        credential = self.credential()
        self.cdp.send(
            "WebAuthn.removeCredential",
            {
                "authenticatorId": self.authenticator_id,
                "credentialId": credential["credentialId"],
            },
        )
        self.cdp.send(
            "WebAuthn.addCredential",
            {
                "authenticatorId": self.authenticator_id,
                "credential": {**credential, "signCount": count},
            },
        )


@pytest.fixture
def virtual_key(page: Page) -> Iterator[VirtualKey]:
    cdp = page.context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    authenticator = cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "usb",
                "hasResidentKey": False,
                "hasUserVerification": False,
                "automaticPresenceSimulation": True,
            }
        },
    )["authenticatorId"]
    try:
        yield VirtualKey(cdp, authenticator)
    finally:
        cdp.send("WebAuthn.removeVirtualAuthenticator", {"authenticatorId": authenticator})
        cdp.detach()


def register_key(page: Page, server: E2EServer) -> str:
    # WebAuthn needs a domain RP ID, not the harness's default IP address.
    url = f"http://localhost:{server.port}"
    page.goto(url)
    expect(page).to_have_url(f"{url}/index.html#/setup")
    page.get_by_label("Key name").fill("Desk security key")
    page.get_by_label("Setup PIN").fill(server.pin.current)
    page.get_by_role("button", name="Register key", exact=True).click()
    expect(page.get_by_text("Registered Desk security key.", exact=True)).to_be_visible()
    (key,) = auth_admin.list_fido2_keys(server.engine)
    assert key.rp_id == "localhost"
    assert key.last_used_at is None
    return url


@pytest.mark.parametrize(
    ("path", "stale_token"),
    [
        ("/index.html#/login", False),
        ("/index.html#/", True),
        ("/#/login", False),
        ("/#/", True),
    ],
)
def test_setup_mode_routes_old_dashboard_urls_to_key_registration(
    make_server: Callable[..., E2EServer],
    page: Page,
    virtual_key: VirtualKey,
    path: str,
    stale_token: bool,
) -> None:
    server = make_server(setup=True)
    url = f"http://localhost:{server.port}"
    if stale_token:
        page.add_init_script("localStorage.setItem('jailbee.token', 'expired-session')")
    page.goto(f"{url}{path}")

    expect(page.get_by_label("Setup PIN")).to_be_visible(timeout=2_000)
    page.get_by_label("Key name").fill("Recovered tab key")
    page.get_by_label("Setup PIN").fill(server.pin.current)
    page.get_by_role("button", name="Register key", exact=True).click()
    expect(page.get_by_text("Registered Recovered tab key.", exact=True)).to_be_visible()


@pytest.mark.parametrize("signed_in", [False, True], ids=["login", "dashboard"])
def test_open_tab_switches_to_setup_without_a_reload(
    make_server: Callable[..., E2EServer], page: Page, virtual_key: VirtualKey, signed_in: bool
) -> None:
    server = make_server()
    server.push(snapshot_of())
    url = f"http://localhost:{server.port}"
    if signed_in:
        log_in_with_pin(page, url, server.pin.current)
    else:
        page.goto(f"{url}/#/login")
        expect(page.get_by_label("PIN")).to_be_visible()

    server.stop()
    server.start(setup=True)

    expect(page.get_by_label("Setup PIN")).to_be_visible()
    page.get_by_label("Key name").fill("Open tab key")
    page.get_by_label("Setup PIN").fill(server.pin.current)
    page.get_by_role("button", name="Register key", exact=True).click()
    expect(page.get_by_text("Registered Open tab key.", exact=True)).to_be_visible()


def test_setup_tab_leaves_for_login_when_the_dashboard_returns_without_a_reload(
    make_server: Callable[..., E2EServer], page: Page, virtual_key: VirtualKey
) -> None:
    server = make_server(setup=True)
    server.push(snapshot_of())
    url = register_key(page, server)
    page.evaluate("window.__sameDocument = true")

    # The CLI stops setup mode and starts the dashboard at the very same origin.
    server.stop()
    server.start()

    # The setup view polls the mode; the marker proves no document reload.
    expect(page.get_by_role("button", name="Use security key", exact=True)).to_be_visible()
    expect(page).to_have_url(f"{url}/index.html#/login")
    assert page.evaluate("window.__sameDocument") is True


def test_signed_in_tab_returns_to_its_page_after_setup_ends_without_a_new_login(
    make_server: Callable[..., E2EServer], page: Page, virtual_key: VirtualKey
) -> None:
    server = make_server()
    server.push(snapshot_of())
    url = f"http://localhost:{server.port}"
    log_in_with_pin(page, url, server.pin.current)
    page.goto(f"{url}/#/settings")
    page.evaluate("window.__sameDocument = true")

    server.stop()
    server.start(setup=True)
    expect(page.get_by_label("Setup PIN")).to_be_visible()

    # Device sessions persist in the state DB, so the stored token stays valid.
    # The setup view's poll may land in the gap between the two servers; answer
    # it as the setup server would, so Chrome logs no refused connection.
    page.route("**/api/mode", lambda route: route.fulfill(json={"mode": "setup"}))
    server.stop()
    server.start()
    page.unroute("**/api/mode")

    expect(page).to_have_url(re.compile(r"#/settings$"))
    expect(page.get_by_role("heading", name="Settings", level=1)).to_be_visible()
    assert page.evaluate("window.__sameDocument") is True


@pytest.mark.parametrize("counter_rollback", [1, 2], ids=["equal-counter", "lower-counter"])
def test_registered_security_key_updates_its_counter_and_refuses_a_clone(
    make_server: Callable[..., E2EServer],
    page: Page,
    virtual_key: VirtualKey,
    browser_errors: BrowserErrors,
    counter_rollback: int,
) -> None:
    server = make_server(setup=True)
    server.push(snapshot_of())
    url = register_key(page, server)

    # The CLI stops setup mode and starts the dashboard at the very same origin.
    server.stop()
    server.start()
    page.goto(f"{url}/#/login")
    previous_count = virtual_key.credential()["signCount"]
    previous_used = None
    for _ in range(2):
        page.get_by_role("button", name="Use security key", exact=True).click()

        expect(page.get_by_role("button", name="Log out")).to_be_visible()
        expect(page.locator(".bar")).to_contain_text(DERIVED_DEVICE)
        expect(page.get_by_text("No repositories.", exact=True)).to_be_visible()
        (key,) = auth_admin.list_fido2_keys(server.engine)
        assert key.last_used_at is not None
        if previous_used is not None:
            assert key.last_used_at > previous_used
        previous_used = key.last_used_at
        current_count = virtual_key.credential()["signCount"]
        assert current_count > previous_count
        with Session(server.engine) as session:
            (stored_key,) = session.exec(select(WebFido2Key)).all()
            assert stored_key.sign_count == current_count
        previous_count = current_count

        page.get_by_role("button", name="Log out").click()
        expect(page).to_have_url(f"{url}/#/login")

    # A cloned key can still sign a fresh challenge but has an outdated counter.
    # Chrome increments before signing, so roll back one extra step for equality.
    virtual_key.set_sign_count(previous_count - counter_rollback)
    browser_errors.expect(
        "console",
        "Failed to load resource: the server responded with a status of 401 (Unauthorized)",
    )
    with page.expect_response(f"{url}/api/auth") as refused:
        page.get_by_role("button", name="Use security key", exact=True).click()
    assert refused.value.status == 401
    expect(page.get_by_role("alert")).to_have_text("The security key was not accepted.")
    expect(page).to_have_url(f"{url}/#/login")
    assert virtual_key.credential()["signCount"] == previous_count - counter_rollback + 1
    assert auth_admin.list_sessions(server.engine) == []
    event = auth_admin.recent_events(server.engine)[-1]
    assert (event.event, event.method) == ("login_failed", "fido2")
    assert event.device_name.startswith(DERIVED_DEVICE)
    with Session(server.engine) as session:
        (stored_key,) = session.exec(select(WebFido2Key)).all()
        assert stored_key.sign_count == previous_count


def test_a_removed_security_key_is_refused_visibly(
    make_server: Callable[..., E2EServer],
    page: Page,
    virtual_key: VirtualKey,
    browser_errors: BrowserErrors,
) -> None:
    server = make_server(setup=True)
    url = register_key(page, server)
    server.stop()
    server.start()
    page.goto(f"{url}/#/login")

    def revoke_before_delivery(route: Route) -> None:
        # Pause the real signed request, not the ceremony or the server response.
        # This models the host revoking the key while its login is in flight.
        try:
            (key,) = auth_admin.list_fido2_keys(server.engine)
            auth_admin.remove_fido2_key(server.engine, key.id)
        finally:
            route.continue_()

    page.route(f"{url}/api/auth", revoke_before_delivery, times=1)
    browser_errors.expect(
        "console",
        "Failed to load resource: the server responded with a status of 401 (Unauthorized)",
    )
    page.get_by_role("button", name="Use security key", exact=True).click()

    expect(page.get_by_role("alert")).to_have_text("The security key was not accepted.")
    expect(page).to_have_url(f"{url}/#/login")
    expect(page.get_by_role("button", name="Use security key", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Log out")).not_to_be_visible()
    assert auth_admin.list_sessions(server.engine) == []
    # FIDO2 is deliberately exempt from rate limits (UNLIMITED_METHODS): each
    # attempt signs a fresh challenge, so a failure is audited, not backed off.
    events = auth_admin.recent_events(server.engine)
    assert [(event.event, event.method) for event in events] == [
        ("key_added", "fido2"),
        ("login_failed", "fido2"),
    ]
    assert events[0].device_name == "Desk security key"
    assert events[1].device_name.startswith(DERIVED_DEVICE)
