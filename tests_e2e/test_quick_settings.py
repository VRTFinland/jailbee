"""Quick settings at the browser boundary, including short e-ink menu pages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.flows import open_quick_settings, open_settings, reveal_setting
from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import open_transcript, record, wait_for_scroll_settled


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_menu_context_paging_and_live_preferences(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int, profile: str
) -> None:
    page.set_viewport_size({"width": width, "height": 600})
    server, transcript = open_transcript(page, make_server, tmp_path)
    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))
    if profile == "eink":
        open_settings(page)
        page.get_by_role("radio", name="E-ink (page turns, no animation, slow refresh)").check()
        page.go_back()
    icon = page.get_by_role("button", name="Quick settings", exact=True)
    open_quick_settings(page)
    panel = page.get_by_role("dialog", name="Quick settings")
    expect(panel.get_by_role("button", name="All", exact=True)).to_have_count(0)
    panel.get_by_role("button", name="Increase body font size").click()
    expect(page.locator("html")).to_have_css("font-size", "17px")
    reveal_setting(page, "checkbox", "Show thinking")
    panel.get_by_role("checkbox", name="Show thinking").check()
    reveal_setting(page, "combobox", "Agent context")
    panel.get_by_role("combobox", name="Agent context").select_option("collapsed")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert panel.evaluate("el => el.getBoundingClientRect().bottom <= innerHeight - 32")
    if profile == "eink":
        expect(panel).to_have_css("box-shadow", "none")
        # Every item is reachable without scrolling the popover.
        seen: set[str] = set()
        previous = panel.get_by_role("button", name="Previous settings page")
        while previous.is_visible() and previous.is_enabled():
            previous.click()
        while True:
            seen.update(panel.locator(".menu-row:visible").all_text_contents())
            next_page = panel.get_by_role("button", name="Next settings page")
            if not next_page.is_visible() or next_page.is_disabled():
                break
            next_page.click()
        assert any("Body:" in text for text in seen)
        assert any("Follow new messages" in text for text in seen)
        assert any("Agent context" in text for text in seen)
        assert "Settings" in seen
        assert panel.evaluate("el => el.scrollHeight === el.clientHeight")
    page.keyboard.press("Escape")
    expect(icon).to_be_focused()
    open_settings(page)
    expect(page.get_by_role("slider", name="Body:")).to_have_value("17")
    expect(page.get_by_role("checkbox", name="Show thinking")).to_be_checked()
    expect(page.get_by_role("combobox", name="Agent context")).to_have_value("collapsed")
    page.get_by_role("radio", name="Dark", exact=True).check()
    open_quick_settings(page)
    expect(panel.get_by_role("button", name="Theme: Dark", exact=True)).to_be_visible()
    # Outside click dismisses and returns focus without moving the document.
    page.locator(".quick-backdrop").click(position={"x": 5, "y": 200})
    expect(icon).to_be_focused()
    page.goto(f"{server.url}/#/containers/work-main")
    expect(page.get_by_role("heading", name="work-main", exact=True)).to_be_visible()
    open_quick_settings(page)
    expect(panel.locator(".view-heading")).to_have_count(0)
    expect(panel.get_by_role("checkbox")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.keyboard.press("Escape")
    page.goto(f"{server.url}/#/")
    reveal_setting(page, "button", "Running")
    panel.get_by_role("button", name="Running", exact=True).click()
    page.keyboard.press("Escape")
    expect(page.locator(".filter-marker")).to_be_visible()
    expect(page.locator(".filter-notice")).to_have_text("Showing: Running · show all")
    page.get_by_role("button", name="show all").click()
    expect(page.locator(".filter-marker")).to_have_count(0)
    expect(page.locator(".filter-notice")).to_have_count(0)


def test_menu_follow_opt_out_keeps_live_entries_from_scrolling(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    server, transcript = open_transcript(page, make_server, tmp_path)
    page.get_by_role("button", name="Latest answer").click()
    page.get_by_role("button", name="Scroll to bottom").click()
    wait_for_scroll_settled(page)
    reveal_setting(page, "checkbox", "Follow new messages")
    page.get_by_role("checkbox", name="Follow new messages", exact=True).uncheck()
    page.keyboard.press("Escape")
    start = page.evaluate("scrollY")
    with transcript.open("a") as stream:
        stream.write(record(90, "assistant", "Opted out reply.\n\n" + "More text.\n\n" * 30))
    server.watches.open[-1].fire()
    expect(page.get_by_text("Opted out reply.", exact=True)).to_have_count(1)
    wait_for_scroll_settled(page)
    assert page.evaluate("scrollY") == start
    expect(page.get_by_role("button", name="New messages", exact=False)).to_be_visible()
