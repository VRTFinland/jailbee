"""Quick settings at the browser boundary, including short e-ink menu pages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.flows import open_quick_settings, open_settings_overlay, reveal_setting
from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import open_transcript, record, wait_for_scroll_settled


@pytest.mark.parametrize("theme", ["light", "dark", "eink"])
def test_floating_trigger_has_opaque_surface_and_panel_tracks_its_target_size(
    logged_in: Page, theme: str
) -> None:
    page = logged_in
    page.evaluate(
        """theme => {
        localStorage.setItem('jailbee.showTitleBar', '0');
        localStorage.setItem('jailbee.theme', theme);
    }""",
        theme,
    )
    page.reload()
    trigger = page.get_by_role("button", name="Quick settings", exact=True, include_hidden=True)
    expect(trigger).to_be_visible()
    assert trigger.evaluate("el => getComputedStyle(el).backgroundColor") not in {
        "transparent",
        "rgba(0, 0, 0, 0)",
    }
    if theme == "eink":
        expect(trigger).to_have_css("background-color", "rgb(255, 255, 255)")
        expect(trigger).to_have_css("border-top-color", "rgb(0, 0, 0)")
        expect(trigger).to_have_css("border-top-style", "solid")
        expect(trigger).to_have_css("border-top-width", "1px")
    # The popover follows the primitive's actual hit-target token, even when enlarged.
    page.evaluate("document.documentElement.style.setProperty('--md-target-min', '64px')")
    trigger.click()
    panel = page.get_by_role("dialog", name="Quick settings")
    expect(panel).to_be_visible()
    button_box, panel_box = trigger.bounding_box(), panel.bounding_box()
    assert button_box and panel_box
    assert button_box["height"] == 64
    assert 0 <= panel_box["y"] - (button_box["y"] + button_box["height"]) <= 8


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
def test_menu_is_at_right_edge_and_does_not_overlap_title(logged_in: Page, width: int) -> None:
    page = logged_in
    page.set_viewport_size({"width": width, "height": 600})
    icon = page.get_by_role("button", name="Quick settings", exact=True, include_hidden=True)
    heading = page.get_by_role("heading", name="JailBee", exact=True)
    expect(icon).to_be_visible()
    icon_box = icon.bounding_box()
    heading_box = heading.bounding_box()
    assert icon_box is not None and heading_box is not None
    assert icon_box["x"] + icon_box["width"] <= width
    assert heading_box["x"] + heading_box["width"] <= icon_box["x"]
    bar_box = page.locator(".title-bar").bounding_box()
    assert bar_box is not None
    assert bar_box["height"] <= 44
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    open_quick_settings(page)
    panel = page.get_by_role("dialog", name="Quick settings")
    panel_box = panel.bounding_box()
    assert panel_box is not None
    assert panel_box["x"] + panel_box["width"] == icon_box["x"] + icon_box["width"]
    assert panel_box["x"] + panel_box["width"] <= width


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_menu_context_paging_and_live_preferences(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int, profile: str
) -> None:
    page.set_viewport_size({"width": width, "height": 600})
    server, _transcript = open_transcript(page, make_server, tmp_path)
    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))
    if profile == "eink":
        open_settings_overlay(page)
        page.get_by_role("radio", name="E-ink (page turns, no animation, slow refresh)").check()
        page.get_by_role("button", name="Close settings").click()
        expect(page.get_by_role("dialog", name="Settings")).to_have_count(0)
    icon = page.get_by_role("button", name="Quick settings", exact=True, include_hidden=True)
    open_quick_settings(page)
    panel = page.get_by_role("dialog", name="Quick settings")
    expect(panel.get_by_role("button", name="All", exact=True)).to_have_count(0)
    panel.get_by_role("button", name="Increase body font size").click()
    expect(page.locator("html")).to_have_css("font-size", "17px")
    reveal_setting(page, "switch", "Show thinking")
    panel.get_by_role("switch", name="Show thinking").check()
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
    open_settings_overlay(page)
    expect(page.get_by_role("slider", name="Body:")).to_have_value("17")
    expect(page.get_by_role("switch", name="Show thinking")).to_be_checked()
    expect(page.get_by_role("combobox", name="Agent context")).to_have_value("collapsed")
    page.get_by_role("radio", name="Dark", exact=True).check()
    page.get_by_role("button", name="Close settings").click()
    expect(page.get_by_role("dialog", name="Settings")).to_have_count(0)
    open_quick_settings(page)
    expect(panel.get_by_role("button", name="Theme: Dark", exact=True)).to_be_visible()
    # Outside click dismisses and returns focus without moving the document.
    page.locator(".ui-dialog-scrim").click(position={"x": 1, "y": 200})
    expect(icon).to_be_focused()
    page.goto(f"{server.url}/#/containers/work-main")
    expect(page.get_by_role("heading", name="work-main", exact=True)).to_be_visible()
    open_quick_settings(page)
    expect(panel.locator(".view-heading")).to_have_count(0)
    expect(panel.get_by_role("switch")).to_have_count(0)
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


@pytest.mark.parametrize("height", [320, 440])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_large_font_menu_pages_fit_above_the_bottom_guard(
    logged_in: Page, height: int, profile: str
) -> None:
    page = logged_in
    page.set_viewport_size({"width": 390, "height": height})
    open_settings_overlay(page)
    size = page.get_by_role("slider", name="Body:")
    size.focus()
    size.press("End")
    if profile == "eink":
        page.get_by_role("radio", name="E-ink (page turns, no animation, slow refresh)").check()
    page.get_by_role("button", name="Close settings").click()
    expect(page.get_by_role("dialog", name="Settings")).to_have_count(0)
    open_quick_settings(page)
    panel = page.get_by_role("dialog", name="Quick settings")
    while True:
        assert panel.evaluate("el => el.getBoundingClientRect().bottom <= innerHeight - 32")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        next_page = panel.get_by_role("button", name="Next settings page")
        if not next_page.is_visible() or next_page.is_disabled():
            break
        next_page.click()


@pytest.mark.parametrize("page_context", ["containers", "session"])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_small_viewport_pages_show_every_context_row_in_order(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    page_context: str,
    profile: str,
) -> None:
    page.set_viewport_size({"width": 390, "height": 320})
    server, _transcript = open_transcript(page, make_server, tmp_path)
    if page_context == "containers":
        page.goto(f"{server.url}/#/")
        expect(page.get_by_role("heading", name="JailBee")).to_be_visible()
    if profile == "eink":
        page.evaluate("localStorage.setItem('jailbee.displayProfile', 'eink')")
        page.reload()
    open_quick_settings(page)
    panel = page.get_by_role("dialog", name="Quick settings")
    assert panel.evaluate("el => el.getBoundingClientRect().bottom <= innerHeight - 32")

    expected_rows = ["Body: 16px", "Code: 14px", "Theme: Auto", "View"]
    expected_rows += (
        ["All", "Running", "Active"]
        if page_context == "containers"
        else ["Jump to…", "Follow new messages", "Show thinking", "Agent context"]
    )
    expected_rows.append("Settings")
    seen: list[str] = []
    previous = panel.get_by_role("button", name="Previous settings page")
    while previous.is_visible() and previous.is_enabled():
        previous.click()
    while True:
        seen.extend(
            panel.locator(".menu-row:visible").evaluate_all(
                "rows => rows.map(row => (row.classList.contains('font-row') "
                "? row.querySelector('span').innerText : row.classList.contains('context-row') "
                "? row.querySelector('label').textContent : row.innerText)"
                ".replace(/\\s+/g, ' ').trim())"
            )
        )
        next_page = panel.get_by_role("button", name="Next settings page")
        if not next_page.is_visible() or next_page.is_disabled():
            break
        next_page.click()
        assert panel.evaluate("el => el.getBoundingClientRect().bottom <= innerHeight - 32")
    if page_context == "session":
        assert seen[0].startswith("Session")
        assert "claude" in seen[0]
        assert "test-model" in seen[0]
        assert "33333333-3333-4333-8333-333333333333" in seen[0]
        seen = seen[1:]
    assert seen == expected_rows
    assert panel.evaluate("el => el.scrollHeight === el.clientHeight")


@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_resizing_menu_clamps_pages_and_keeps_all_rows_reachable(
    logged_in: Page, profile: str
) -> None:
    page = logged_in
    page.set_viewport_size({"width": 390, "height": 320})
    if profile == "eink":
        page.evaluate("localStorage.setItem('jailbee.displayProfile', 'eink')")
        page.reload()
    open_quick_settings(page)
    panel = page.get_by_role("dialog", name="Quick settings")
    next_page = panel.get_by_role("button", name="Next settings page")
    while next_page.is_visible() and next_page.is_enabled():
        next_page.click()
    page.set_viewport_size({"width": 600, "height": 600})
    page.wait_for_function("""() => {
        const status = document.querySelector('.menu-pages [role=status]');
        return status && status.textContent === '1 / 1';
    }""")
    assert panel.evaluate("el => el.getBoundingClientRect().bottom <= innerHeight - 32")
    rows = panel.locator(".menu-row:visible").evaluate_all(
        "rows => rows.map(row => (row.classList.contains('font-row') "
        "? row.querySelector('span').innerText : row.innerText).replace(/\\s+/g, ' ').trim())"
    )
    assert rows == [
        "Body: 16px",
        "Code: 14px",
        "Theme: Auto",
        "View",
        "All",
        "Running",
        "Active",
        "Settings",
    ]
    page.set_viewport_size({"width": 600, "height": 320})
    expect(next_page).to_be_visible()
    expect(next_page).to_be_enabled()
    seen: list[str] = []
    while True:
        seen.extend(
            panel.locator(".menu-row:visible").evaluate_all(
                "rows => rows.map(row => (row.classList.contains('font-row') "
                "? row.querySelector('span').innerText : row.classList.contains('context-row') "
                "? row.querySelector('label').textContent : row.innerText)"
                ".replace(/\\s+/g, ' ').trim())"
            )
        )
        assert panel.evaluate("el => el.getBoundingClientRect().bottom <= innerHeight - 32")
        if not next_page.is_visible() or next_page.is_disabled():
            break
        next_page.click()
    assert seen == rows


@pytest.mark.parametrize("public_page", ["login", "setup"])
def test_public_page_content_has_no_quick_settings_overlap(
    page: Page, make_server: Callable[..., E2EServer], public_page: str
) -> None:
    server = make_server(setup=public_page == "setup")
    page.set_viewport_size({"width": 320, "height": 320})
    page.add_init_script(
        "localStorage.setItem('jailbee.bodyFontSize', '24'); "
        "localStorage.setItem('jailbee.displayProfile', 'eink')"
    )
    page.goto(f"{server.url}/#/{public_page}")
    # Simulate a larger touch target and verify that content tracks its actual size.
    page.evaluate("""() => {
        document.documentElement.style.setProperty('--quick-trigger-size', '6rem');
    }""")
    heading = "JailBee" if public_page == "login" else "Register a security key"
    expect(page.get_by_role("heading", name=heading)).to_be_visible()
    expect(page.locator("main input").first).to_be_visible()
    page.evaluate("window.scrollTo(0, 0)")

    expect(page.locator(".quick-trigger")).to_have_count(0)
    overlaps = page.evaluate("""() => {
        const trigger = document.querySelector('.quick-trigger');
        if (!trigger) return false;
        const icon = trigger.getBoundingClientRect();
        return [...document.querySelectorAll('main h1, main input, main button')].some(element => {
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0
                && icon.left < rect.right && icon.right > rect.left
                && icon.top < rect.bottom && icon.bottom > rect.top;
        });
    }""")
    assert not overlaps


def test_escape_after_font_stepper_disables_closes_menu(logged_in: Page) -> None:
    page = logged_in
    open_quick_settings(page)
    panel = page.get_by_role("dialog", name="Quick settings")
    increase = panel.get_by_role("button", name="Increase body font size")
    while increase.is_enabled():
        increase.click()
    # Browsers may leave focus on a disabled element or move it to the body.
    page.locator("body").focus()
    page.evaluate("document.activeElement.blur()")
    page.keyboard.press("Escape")
    expect(panel).to_have_count(0)
    expect(page.get_by_role("button", name="Quick settings", exact=True, include_hidden=True)).to_be_focused()


def test_escape_on_trigger_closes_open_menu(logged_in: Page) -> None:
    page = logged_in
    open_quick_settings(page)
    icon = page.get_by_role("button", name="Quick settings", exact=True, include_hidden=True)
    icon.focus()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="Quick settings")).to_have_count(0)
    expect(icon).to_be_focused()


def test_escape_on_closed_select_closes_menu_and_restores_focus(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    open_transcript(page, make_server, tmp_path)
    reveal_setting(page, "combobox", "Agent context")
    panel = page.get_by_role("dialog", name="Quick settings")
    panel.get_by_role("combobox", name="Agent context").focus()
    page.keyboard.press("Escape")
    expect(panel).to_have_count(0)
    expect(page.get_by_role("button", name="Quick settings", exact=True, include_hidden=True)).to_be_focused()


def test_menu_follow_opt_out_keeps_live_entries_from_scrolling(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    server, transcript = open_transcript(page, make_server, tmp_path)
    page.get_by_role("button", name="Latest answer").click()
    page.get_by_role("button", name="Scroll to bottom").click()
    wait_for_scroll_settled(page)
    reveal_setting(page, "switch", "Follow new messages")
    page.get_by_role("switch", name="Follow new messages", exact=True).uncheck()
    page.keyboard.press("Escape")
    start = page.evaluate("scrollY")
    with transcript.open("a") as stream:
        stream.write(record(90, "assistant", "Opted out reply.\n\n" + "More text.\n\n" * 30))
    server.watches.open[-1].fire()
    expect(page.get_by_text("Opted out reply.", exact=True)).to_have_count(1)
    wait_for_scroll_settled(page)
    assert page.evaluate("scrollY") == start
    expect(page.get_by_role("button", name="New messages", exact=False)).to_be_visible()
