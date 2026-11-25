"""Non-security-key login paths through the real server and browser app."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta

import pyotp
import pytest
from playwright.sync_api import Page, expect

from jailbee.web.auth_admin import add_totp_key, list_sessions, revoke_session
from jailbee.web.login_link import mint
from tests.web_fakes import FakeClock, snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.flows import DERIVED_DEVICE, log_in_with_pin, open_settings
from tests_e2e.server import E2EServer


def test_wrong_pin_shows_an_error_and_rate_limit_shows_the_remaining_wait(
    make_server: Callable[..., E2EServer], page: Page, browser_errors: BrowserErrors
) -> None:
    clock = FakeClock()
    server = make_server(clock=clock, monotonic=clock.monotonic)
    server.push(snapshot_of())
    page.goto(server.url)
    browser_errors.expect(
        "console", "Failed to load resource: the server responded with a status of 401", times=5
    )
    browser_errors.expect(
        "console", "Failed to load resource: the server responded with a status of 429", times=2
    )

    for _ in range(5):
        page.get_by_label("PIN").fill("000000")
        with page.expect_response(server.url + "/api/auth") as attempt:
            page.get_by_role("button", name="Log in", exact=True).click()
        assert attempt.value.status == 401
        expect(page.get_by_role("alert")).to_have_text(
            "Wrong or used PIN. The server printed a new one."
        )
        expect(page.get_by_label("PIN")).to_have_value("")

    for remaining in (60, 59):
        page.get_by_label("PIN").fill(server.pin.current)
        with page.expect_response(server.url + "/api/auth") as limited:
            page.get_by_role("button", name="Log in", exact=True).click()
        assert limited.value.status == 429
        assert limited.value.headers["retry-after"] == str(remaining)
        wait = "1 minute" if remaining == 60 else "59 seconds"
        expect(page.get_by_role("alert")).to_have_text(
            f"Too many attempts. Wait {wait} and try again."
        )
        expect(page.get_by_label("PIN")).to_have_value("")
        clock.advance(timedelta(seconds=1))

    clock.advance(timedelta(seconds=58))
    page.get_by_label("PIN").fill(server.pin.current)
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.get_by_role("button", name="Log out")).to_be_visible()


def test_authenticator_code_signs_in(make_server: Callable[..., E2EServer], page: Page) -> None:
    clock = FakeClock()
    server = make_server(clock=clock)
    server.push(snapshot_of())
    key = add_totp_key(server.engine, "test authenticator", clock=clock)

    page.goto(server.url)
    page.get_by_role("tab", name="Authenticator code").click()
    page.get_by_label("Code", exact=True).fill(pyotp.TOTP(key.secret).at(clock()))
    page.get_by_role("button", name="Log in", exact=True).click()

    expect(page.get_by_role("button", name="Log out")).to_be_visible()
    expect(page.locator(".bar")).to_contain_text(DERIVED_DEVICE)
    expect(page.get_by_text("No repositories.")).to_be_visible()


@pytest.mark.parametrize("link_state", ["fresh", "used", "expired"])
def test_one_time_link_signs_in_only_while_fresh(
    make_server: Callable[..., E2EServer],
    page: Page,
    browser_errors: BrowserErrors,
    link_state: str,
) -> None:
    clock = FakeClock()
    server = make_server(clock=clock)
    server.push(snapshot_of())
    token = mint(server.engine, clock=clock)
    url = server.url + "/#/login?link=" + token

    if link_state == "used":
        page.goto(url)
        expect(page.get_by_role("button", name="Log out")).to_be_visible()
        page.get_by_role("button", name="Log out").click()
        expect(page.get_by_role("button", name="Log in", exact=True)).to_be_visible()
    elif link_state == "expired":
        clock.advance(timedelta(seconds=61))

    if link_state != "fresh":
        browser_errors.expect(
            "console", "Failed to load resource: the server responded with a status of 401"
        )
    page.goto(url)

    if link_state == "fresh":
        expect(page.get_by_role("button", name="Log out")).to_be_visible()
        expect(page.locator(".bar")).to_contain_text(DERIVED_DEVICE)
        expect(page.get_by_text("No repositories.")).to_be_visible()
        expect(page).to_have_url(server.url + "/#/")
    else:
        expect(page.get_by_role("alert")).to_have_text(
            "This login link was already used or has expired. Run `jailbee remote web open` again."
        )
        expect(page.get_by_role("button", name="Log in", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="Log out")).to_have_count(0)
        expect(page).to_have_url(server.url + "/#/login")


def test_a_fresh_link_signs_in_when_the_login_page_is_already_open(
    server: E2EServer, page: Page
) -> None:
    page.goto(server.url)
    expect(page.get_by_role("button", name="Log in", exact=True)).to_be_visible()
    token = mint(server.engine)
    document_started = page.evaluate("performance.timeOrigin")

    page.evaluate("token => { location.hash = '/login?link=' + token }", token)

    expect(page.get_by_role("button", name="Log out")).to_be_visible()
    expect(page.get_by_text("No repositories.")).to_be_visible()
    expect(page).to_have_url(server.url + "/#/")
    assert page.evaluate("performance.timeOrigin") == document_started


def test_login_asks_for_no_name_and_the_header_shows_the_derived_one(
    server: E2EServer, page: Page
) -> None:
    page.goto(server.url)
    expect(page.get_by_label("This device")).to_have_count(0)
    expect(page.get_by_label("PIN")).to_be_visible()

    log_in_with_pin(page, server.url, server.pin.current)

    expect(page.locator(".bar")).to_contain_text(DERIVED_DEVICE)


def test_renaming_the_session_in_settings_updates_the_header_and_the_listing(
    server: E2EServer, page: Page
) -> None:
    log_in_with_pin(page, server.url, server.pin.current)

    open_settings(page)
    name = page.get_by_label("Name shown in the header")
    expect(name).to_have_value(re.compile(r"^Chrome on "))
    name.fill("reading room tablet")
    page.get_by_role("button", name="Save name").click()

    expect(page.get_by_text("Saved.")).to_be_visible()
    page.get_by_role("link", name="JailBee").click()
    expect(page.locator(".bar")).to_contain_text("reading room tablet")
    (session,) = list_sessions(server.engine)
    assert session.device_name == "reading room tablet"


def test_logout_returns_to_login_and_invalidates_the_old_token(
    server: E2EServer, page: Page
) -> None:
    with page.expect_response(server.url + "/api/auth") as login:
        log_in_with_pin(page, server.url, server.pin.current)
    token = login.value.json()["token"]
    headers = {"Authorization": "Bearer " + token}
    assert page.request.get(server.url + "/api/session", headers=headers).status == 200

    page.get_by_role("button", name="Log out").click()

    expect(page).to_have_url(server.url + "/#/login")
    expect(page.get_by_role("button", name="Log in", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Log out")).to_have_count(0)
    assert page.request.get(server.url + "/api/session", headers=headers).status == 401
    page.goto(server.url + "/#/settings")
    expect(page).to_have_url(server.url + "/#/login")


def test_admin_revocation_closes_the_live_socket_and_returns_to_login(
    server: E2EServer, page: Page
) -> None:
    # Observe the browser's public WebSocket close event, not app/store state.
    page.add_init_script("""
        window.__e2eSocketCloseCodes = [];
        window.WebSocket = class extends WebSocket {
            constructor(...args) {
                super(...args);
                this.addEventListener('close', event => {
                    window.__e2eSocketCloseCodes.push(event.code);
                });
            }
        };
    """)
    log_in_with_pin(page, server.url, server.pin.current)
    expect(page.locator(".bar")).to_contain_text(DERIVED_DEVICE)
    (session,) = list_sessions(server.engine)
    document_started = page.evaluate("performance.timeOrigin")

    revoke_session(server.engine, session.id)
    server.push(snapshot_of())

    page.wait_for_function("() => window.__e2eSocketCloseCodes.includes(4001)")
    expect(page).to_have_url(server.url + "/#/login")
    expect(page.get_by_role("button", name="Log in", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Log out")).to_have_count(0)
    assert page.evaluate("performance.timeOrigin") == document_started


def test_auth_disabled_opens_the_dashboard_without_a_login(
    make_server: Callable[..., E2EServer], page: Page
) -> None:
    server = make_server(auth_enabled=False)
    server.push(snapshot_of())

    page.goto(server.url)

    expect(page.get_by_text("No repositories.")).to_be_visible()
    expect(page).to_have_url(server.url + "/#/")
    expect(page.get_by_role("button", name="Log in", exact=True)).to_have_count(0)
    expect(page.get_by_label("PIN", exact=True)).to_have_count(0)


def test_protected_route_without_a_session_redirects_to_login(
    server: E2EServer, page: Page
) -> None:
    page.goto(server.url + "/#/settings")

    expect(page).to_have_url(server.url + "/#/login")
    expect(page.get_by_role("button", name="Log in", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_have_count(0)
