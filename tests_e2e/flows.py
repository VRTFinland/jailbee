"""User-level steps shared by e2e tests: what a person does, not how the app does it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from playwright.sync_api import expect

if TYPE_CHECKING:
    from playwright.sync_api import Page


DERIVED_DEVICE = "Chrome on"
"""What the header shows for the e2e browser: the server names a session after its User-Agent."""


def log_in_with_pin(page: Page, url: str, pin: str) -> None:
    """Open the dashboard at ``url``, land on the login page and log in with ``pin``."""
    page.goto(url)
    page.get_by_label("PIN").fill(pin)
    page.get_by_role("button", name="Log in").click()
    expect(page.get_by_role("button", name="Log out")).to_be_visible()
