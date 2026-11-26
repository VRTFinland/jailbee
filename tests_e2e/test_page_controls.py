"""E-ink controls trade chrome for readable text without changing page turns."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import TITLE, open_transcript, record, wait_for_scroll_settled


def choose_mode(page: Page, mode: str) -> None:
    page.evaluate(
        """mode => {
            localStorage.setItem('jailbee.displayProfile', 'eink');
            localStorage.setItem('jailbee.pageControls', mode);
        }""",
        mode,
    )
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()


@pytest.mark.parametrize("mode", ["indicator", "side", "bar"])
@pytest.mark.parametrize("height", [800, 320], ids=["portrait", "short-landscape"])
def test_page_controls_preserve_readable_height_keys_taps_and_jump_clearance(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    mode: str,
    height: int,
) -> None:
    page.set_viewport_size({"width": 640, "height": height})
    server, transcript = open_transcript(page, make_server, tmp_path)
    choose_mode(page, mode)
    nav = page.get_by_role("navigation", name="Pages", exact=True)
    expect(nav).to_have_attribute("data-mode", mode)
    # Reading-position restore can anchor the first prompt below its margin.
    # Start this page-grid test at the document origin, not that restored anchor.
    page.evaluate("window.scrollTo(0, 0)")
    wait_for_scroll_settled(page)
    # Include a nonzero device inset, independently of control mode.
    page.evaluate("document.documentElement.style.setProperty('--guard-bottom', '60px')")
    wait_for_scroll_settled(page)
    guard = page.locator(".bottom-guard").bounding_box()
    controls = nav.bounding_box()
    assert guard is not None and controls is not None
    assert guard["height"] == 60
    assert abs(controls["y"] + controls["height"] - guard["y"]) <= 1
    if mode == "bar":
        assert controls["height"] == 26
        assert controls["width"] == 640
        expect(nav).to_contain_text("%")
    elif mode == "side":
        assert controls["height"] == 132
        for button in nav.locator(".prev, .next").all():
            box = button.bounding_box()
            assert box is not None and box["height"] == 44 and box["width"] >= 44
    else:
        assert controls["height"] <= 32
        expect(nav.locator(".prev, .next")).to_have_count(0)

    for button in nav.locator("button").all():
        box = button.bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 640

    # Compact chrome retains a full 44px target extending upward, not into
    # the bottom guard. Check actual hit testing, not only pseudo-element CSS.
    for button in nav.locator("button:not(:disabled)").all():
        assert button.evaluate("""el => {
            const r = el.getBoundingClientRect();
            const target = Number.parseFloat(getComputedStyle(el).getPropertyValue('--md-target-min'));
            return el.contains(document.elementFromPoint(r.x + r.width / 2, r.bottom - target + 1));
        }""")

    top = page.locator(".title-bar").evaluate("el => el.getBoundingClientRect().bottom")
    readable = guard["y"] - top - (26 if mode == "bar" else 0)
    assert readable > 0
    for key in ["m", "PageDown", "m", "PageDown"]:
        before = page.evaluate("window.scrollY")
        page.keyboard.press(key)
        wait_for_scroll_settled(page)
        moved = page.evaluate("window.scrollY") - before
        assert abs(moved - int(readable)) <= 1
    for key in ["n", "PageUp"]:
        before = page.evaluate("window.scrollY")
        page.keyboard.press(key)
        wait_for_scroll_settled(page)
        assert abs(before - page.evaluate("window.scrollY") - int(readable)) <= 1
    before = page.evaluate("window.scrollY")
    page.locator(".tap-zone.forward").click(position={"x": 5, "y": 100})
    wait_for_scroll_settled(page)
    assert abs(page.evaluate("window.scrollY") - before - int(readable)) <= 1
    page.locator(".tap-zone.back").click(position={"x": 5, "y": 100})
    wait_for_scroll_settled(page)
    assert abs(page.evaluate("window.scrollY") - before) <= 1
    if mode != "indicator":
        nav.locator(".next").click()
        wait_for_scroll_settled(page)
        assert abs(page.evaluate("window.scrollY") - before - int(readable)) <= 1
        nav.locator(".prev").click()
        wait_for_scroll_settled(page)
        assert abs(page.evaluate("window.scrollY") - before) <= 1

    def assert_jump_clear(selector: str) -> None:
        button = page.locator(selector)
        expect(button).to_be_visible()
        assert button.evaluate(
            """el => {
                const a = el.getBoundingClientRect();
                const b = document.querySelector('.page-nav').getBoundingClientRect();
                const guard = document.querySelector('.bottom-guard').getBoundingClientRect();
                return a.left >= 0 && a.right <= innerWidth && a.bottom <= guard.top &&
                    (a.right <= b.left || a.left >= b.right ||
                        a.bottom <= b.top || a.top >= b.bottom);
            }"""
        )

    assert_jump_clear(".jump-latest")
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "Held while reading with compact controls."))
    server.watches.open[-1].fire()
    assert_jump_clear("button.new-messages")


def test_page_control_choice_is_available_in_settings_and_survives_reload(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    server, _ = open_transcript(page, make_server, tmp_path)
    page.goto(f"{server.url}/#/settings")
    expect(page.get_by_label("Page controls", exact=True)).to_have_count(0)
    page.get_by_role("radio", name="E-ink (page turns").check()
    control = page.get_by_label("Page controls", exact=True)
    expect(control).to_have_value("indicator")
    for value in ["side", "bar", "indicator"]:
        control.select_option(value)
        page.reload()
        expect(control).to_have_value(value)


def test_normal_profile_does_not_render_page_controls(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    open_transcript(page, make_server, tmp_path)
    expect(page.get_by_role("navigation", name="Pages", exact=True)).to_have_count(0)
    expect(page.locator(".tap-zone")).to_have_count(0)
    expect(page.locator(".bottom-guard")).to_have_css("height", "32px")
