"""Security-key setup and login through Chrome's real WebAuthn implementation."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from playwright.sync_api import Page, expect

from jailbee.web import auth_admin
from tests.web_fakes import snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.server import E2EServer


@pytest.fixture
def virtual_key(page: Page) -> Iterator[None]:
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
        yield
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
    return url


def test_registered_security_key_logs_in_to_the_dashboard(
    make_server: Callable[..., E2EServer], page: Page, virtual_key: None
) -> None:
    server = make_server(setup=True)
    server.push(snapshot_of())
    url = register_key(page, server)

    # The CLI stops setup mode and starts the dashboard at the very same origin.
    server.stop()
    server.start()
    page.goto(f"{url}/#/login")
    page.get_by_label("This device").fill("security-key tablet")
    page.get_by_role("button", name="Use security key", exact=True).click()

    expect(page.get_by_role("button", name="Log out")).to_be_visible()
    expect(page.get_by_role("banner")).to_contain_text("security-key tablet")
    expect(page.get_by_text("No repositories.", exact=True)).to_be_visible()


def test_a_removed_security_key_is_refused_visibly(
    make_server: Callable[..., E2EServer],
    page: Page,
    virtual_key: None,
    browser_errors: BrowserErrors,
) -> None:
    server = make_server(setup=True)
    url = register_key(page, server)
    server.stop()
    server.start()
    page.goto(f"{url}/#/login")
    page.get_by_label("This device").fill("revoked-key tablet")

    # As on a host, remove the key after the login page offered that method.
    expect(page.get_by_role("button", name="Use security key", exact=True)).to_be_visible()
    key = auth_admin.list_fido2_keys(server.engine)[0]
    auth_admin.remove_fido2_key(server.engine, key.id)
    browser_errors.expect(
        "console", "Failed to load resource: the server responded with a status of 404 (Not Found)"
    )
    page.get_by_role("button", name="Use security key", exact=True).click()

    expect(page.get_by_role("alert")).to_have_text(
        "No security key is registered for this address."
    )
    expect(page).to_have_url(f"{url}/#/login")
    expect(page.get_by_role("button", name="Use security key", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Log out")).not_to_be_visible()
