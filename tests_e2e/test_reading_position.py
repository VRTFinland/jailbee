"""Remember transcript entries across reloads and typography changes."""

from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Page, expect

from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import TITLE, open_transcript, wait_for_scroll_settled


def test_restores_entry_after_reload_and_font_change(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    page.set_viewport_size({"width": 1280, "height": 800})
    open_transcript(page, make_server, tmp_path)
    entry = page.locator(".entry.assistant").nth(8)
    entry_id = entry.get_attribute("id")
    entry.evaluate("el => el.scrollIntoView({block: 'start'})")
    wait_for_scroll_settled(page)
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    page.evaluate("localStorage.setItem('jailbee.bodyFontSize', '24')")
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    page.wait_for_function(
        """id => {
        const entry = document.getElementById(id);
        const header = document.querySelector('.title-bar');
        if (!entry || !header) return false;
        const difference = entry.getBoundingClientRect().top -
            header.getBoundingClientRect().bottom;
        return Math.abs(difference) <= 2;
    }""",
        arg=entry_id,
    )
