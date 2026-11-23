"""The first end-to-end path: PIN login, then the front page shows the fake state."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer


def test_pin_login_lands_on_a_front_page_listing_the_repo_and_container(
    server: E2EServer, page: Page
) -> None:
    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))

    log_in_with_pin(page, server.url, server.pin.current, "kitchen tablet")

    repo = page.locator("section.repo", has=page.get_by_role("heading", name="work"))
    expect(repo).to_be_visible()
    expect(repo.get_by_role("link", name="main", exact=True)).to_be_visible()
    expect(page.get_by_role("banner")).to_contain_text("kitchen tablet")


def test_a_logged_in_page_sees_state_pushed_after_it_opened(
    server: E2EServer, logged_in: Page
) -> None:
    expect(logged_in.get_by_text("No repositories.")).to_be_visible()

    server.push(snapshot_of(repo_group("beta", container("beta-feature", "beta"))))

    expect(logged_in.get_by_role("link", name="feature", exact=True)).to_be_visible()


def test_the_dashboard_comes_back_after_a_server_restart(
    server: E2EServer, logged_in: Page, browser_errors: BrowserErrors
) -> None:
    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))
    expect(logged_in.get_by_role("link", name="main", exact=True)).to_be_visible()

    refused = re.compile(
        rf"WebSocket connection to 'ws://127\.0\.0\.1:{server.port}/api/ws' failed: "
        r"Error in connection establishment: net::ERR_CONNECTION_REFUSED"
    )
    browser_errors.expect("console", refused, at_least=1)
    # Exercise a real reconnect gap, rather than racing its exponential backoff.
    with logged_in.expect_console_message(
        predicate=lambda message: refused.fullmatch(message.text) is not None
    ):
        server.stop()
    server.start()
    server.push(snapshot_of(repo_group("work", container("work-next", "work"))))

    expect(logged_in.get_by_role("link", name="next", exact=True)).to_be_visible()
