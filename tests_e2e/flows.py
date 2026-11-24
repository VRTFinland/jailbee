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


def open_quick_settings(page: Page) -> None:
    """Open the menu unless it is already open."""
    trigger = page.get_by_role("button", name="Quick settings", exact=True)
    if trigger.get_attribute("aria-expanded") != "true":
        trigger.click()
    expect(page.get_by_role("dialog", name="Quick settings")).to_be_visible()


def reveal_setting(page: Page, role: str, name: str) -> None:
    """Page through the menu, including on a short e-ink screen."""
    open_quick_settings(page)
    panel = page.get_by_role("dialog", name="Quick settings")
    previous = panel.get_by_role("button", name="Previous settings page")
    while previous.is_visible() and previous.is_enabled():
        previous.click()
    target = panel.get_by_role(role, name=name, exact=True)  # type: ignore[arg-type]  # Shared role-based step.
    while not target.is_visible():
        next_page = panel.get_by_role("button", name="Next settings page")
        expect(next_page).to_be_enabled()
        next_page.click()
    expect(target).to_be_visible()


def open_settings(page: Page) -> None:
    """Reach the full settings page through quick settings."""
    reveal_setting(page, "link", "Settings")
    page.get_by_role("link", name="Settings", exact=True).click()
