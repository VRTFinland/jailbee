"""Settings preserve the live reader beneath a responsive overlay."""

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.flows import open_settings_overlay
from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import open_transcript, record, wait_for_scroll_settled


@pytest.mark.parametrize("width", [390, 1280])
def test_settings_preserves_reader_and_live_updates(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    server, transcript = open_transcript(page, make_server, tmp_path)
    page.evaluate("window.scrollTo(0, 600)")
    wait_for_scroll_settled(page)
    position = page.evaluate("window.scrollY")
    url = page.url
    open_settings_overlay(page)
    dialog = page.get_by_role("dialog", name="Settings", exact=True)
    box = dialog.bounding_box()
    assert box is not None
    if width == 390:
        assert box["y"] > 0 and box["width"] == width
    else:
        assert box["y"] == 0 and box["x"] > 0
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "Overlay live update"))
    server.watches.open[0].fire()
    expect(page.locator(".entry").get_by_text("Overlay live update")).to_be_attached()
    page.go_back()
    expect(dialog).to_have_count(0)
    assert page.url == url
    assert page.evaluate("window.scrollY") == position
    expect(page.get_by_role("button", name="Quick settings", exact=True)).to_be_focused()
    open_settings_overlay(page)
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert page.evaluate("window.scrollY") == position
    open_settings_overlay(page)
    page.get_by_role("button", name="Close settings").click()
    expect(dialog).to_have_count(0)
    assert page.evaluate("window.scrollY") == position


def test_eink_overlay_has_no_tint_or_motion(logged_in: Page) -> None:
    page = logged_in
    open_settings_overlay(page)
    page.get_by_role("radio", name="E-ink (page turns, no animation, slow refresh)").check()
    expect(page.locator(".settings-backdrop")).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_have_css("transition-duration", "0s")
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_have_css("box-shadow", "none")
