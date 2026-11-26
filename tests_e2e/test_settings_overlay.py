"""Settings preserve the live reader beneath a responsive overlay."""

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.flows import open_settings_overlay
from tests_e2e.server import E2EServer
from tests_e2e.test_reading_position import assert_at_transcript_end
from tests_e2e.test_session_reading import open_transcript, record, wait_for_scroll_settled


def test_settings_scrim_covers_floating_trigger_even_if_trigger_is_later_in_dom(
    logged_in: Page,
) -> None:
    page = logged_in
    page.evaluate("localStorage.setItem('jailbee.showTitleBar', '0')")
    page.reload()
    open_settings_overlay(page)
    trigger = page.get_by_role("button", name="Quick settings", exact=True, include_hidden=True)
    page.evaluate("""() => {
        const floating = document.querySelector('.quick-settings');
        floating.style.pointerEvents = 'auto';
        document.body.append(floating);
    }""")
    assert trigger.evaluate("""el => {
        const trigger = el.getBoundingClientRect();
        const x = trigger.x + trigger.width / 2;
        const y = trigger.y + trigger.height / 2;
        const top = document.elementFromPoint(x, y);
        return !el.contains(top);
    }""")


def test_browser_forward_reopens_settings_and_close_leaves_reader(logged_in: Page) -> None:
    page = logged_in
    url = page.url
    open_settings_overlay(page)
    dialog = page.get_by_role("dialog", name="Settings", exact=True)
    page.go_back()
    expect(dialog).to_have_count(0)
    page.go_forward()
    expect(dialog).to_be_visible()
    page.get_by_role("button", name="Close settings").click()
    expect(dialog).to_have_count(0)
    open_settings_overlay(page)
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    assert page.url == url


def test_title_bar_toggle_restores_focus_to_visible_menu(logged_in: Page) -> None:
    page = logged_in
    open_settings_overlay(page)
    page.get_by_role("switch", name="Show title bar").uncheck()
    page.get_by_role("button", name="Close settings").click()
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_have_count(0)
    expect(
        page.get_by_role("button", name="Quick settings", exact=True, include_hidden=True)
    ).to_be_focused()
    expect(page.locator(".title-bar")).to_have_count(0)


def test_settings_keeps_active_follow_at_the_live_end(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    server, transcript = open_transcript(page, make_server, tmp_path)
    page.keyboard.press("j")
    page.get_by_role("dialog", name="Jump to").get_by_role("button", name="End", exact=True).click()
    assert_at_transcript_end(page)
    open_settings_overlay(page)
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "Live follow while settings are open.\n\n" * 40))
    server.watches.open[-1].fire()
    expect(
        page.get_by_text("Live follow while settings are open.", exact=True).first
    ).to_be_attached()
    assert_at_transcript_end(page)
    page.get_by_role("button", name="Close settings").click()
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_have_count(0)
    assert_at_transcript_end(page)


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
    expect(
        page.get_by_role("button", name="Quick settings", exact=True, include_hidden=True)
    ).to_be_focused()
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
    page.set_viewport_size({"width": 1280, "height": 800})
    open_settings_overlay(page)
    dialog = page.get_by_role("dialog", name="Settings", exact=True)
    expect(dialog).to_have_css("border-top-color", "rgba(0, 0, 0, 0)")
    page.get_by_role("radio", name="E-ink (page turns, no animation, slow refresh)").check()
    expect(dialog).to_have_css("border-top-width", "1px")
    expect(dialog).to_have_css("border-top-style", "solid")
    expect(page.locator(".ui-dialog-scrim")).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_have_css(
        "transition-duration", "0s"
    )
    expect(page.get_by_role("dialog", name="Settings", exact=True)).to_have_css(
        "box-shadow", "none"
    )
