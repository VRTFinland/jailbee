"""The first end-to-end path: PIN login, then the front page shows the fake state."""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.web_fakes import container, repo_group, snapshot_of
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
