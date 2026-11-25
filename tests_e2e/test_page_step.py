"""Overlapping e-ink page turns, readable geometry and the previous-page guide."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypedDict, cast

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import TITLE, open_transcript, wait_for_scroll_settled


class ReadingArea(TypedDict):
    top: float
    bottom: float
    y: float


def reading_area(page: Page) -> ReadingArea:
    return cast(ReadingArea, page.evaluate("""() => ({
        top: Number.parseFloat(document.querySelector('.page').style.getPropertyValue('--session-readable-top')),
        bottom: (document.querySelector('.page-nav[data-mode="bar"]') ??
            document.querySelector('.bottom-guard')).getBoundingClientRect().top,
        y: window.scrollY,
    })"""))


def open_paged_transcript(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, step: int
) -> None:
    open_transcript(page, make_server, tmp_path)
    page.evaluate(
        """step => {
            localStorage.setItem('jailbee.displayProfile', 'eink');
            localStorage.setItem('jailbee.theme', 'eink');
            localStorage.setItem('jailbee.pageStep', String(step));
        }""",
        step,
    )
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    page.evaluate("window.scrollTo(0, 0)")
    wait_for_scroll_settled(page)


@pytest.mark.parametrize("step", [100, 90, 75, 66, 50])
@pytest.mark.parametrize("size", [(390, 800), (640, 320)], ids=["phone", "landscape"])
def test_each_step_overlaps_without_skipping_readable_text(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    step: int,
    size: tuple[int, int],
) -> None:
    page.set_viewport_size({"width": size[0], "height": size[1]})
    open_paged_transcript(page, make_server, tmp_path, step)
    # A large safe-area inset exercises the short landscape readable area too.
    page.evaluate("document.documentElement.style.setProperty('--guard-bottom', '60px')")
    wait_for_scroll_settled(page)
    for _ in range(4):
        before = reading_area(page)
        page.keyboard.press("m")
        page.wait_for_function("y => window.scrollY > y", arg=before["y"])
        wait_for_scroll_settled(page)
        after = reading_area(page)
        moved = after["y"] - before["y"]
        readable = before["bottom"] - before["top"]
        assert moved == pytest.approx(int(readable * step / 100), abs=1)
        assert after["y"] + after["top"] <= before["y"] + before["bottom"] + 1
        if step == 100:
            expect(page.locator(".page-guide")).to_have_count(0)


@pytest.mark.parametrize("step", [90, 75, 66, 50])
def test_guide_marks_previous_bottom_then_disappears_on_turn_or_scroll(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, step: int
) -> None:
    page.set_viewport_size({"width": 390, "height": 800})
    open_paged_transcript(page, make_server, tmp_path, step)
    before = reading_area(page)
    page.keyboard.press("m")
    wait_for_scroll_settled(page)
    guide = page.locator(".page-guide")
    expect(guide).to_be_visible()
    after = reading_area(page)
    assert guide.evaluate("el => el.getBoundingClientRect().top") == pytest.approx(
        before["y"] + before["bottom"] - after["y"], abs=1
    )
    expect(guide).to_have_css("height", "1px")
    expect(guide).to_have_css("background-color", "rgb(0, 0, 0)")
    expect(guide).to_have_css("pointer-events", "none")
    page.keyboard.press("m")
    wait_for_scroll_settled(page)
    expect(guide).to_have_count(0)
    page.keyboard.press("m")
    wait_for_scroll_settled(page)
    expect(guide).to_be_visible()
    page.mouse.wheel(0, 10)
    expect(guide).to_have_count(0)
    page.keyboard.press("m")
    wait_for_scroll_settled(page)
    expect(guide).to_be_visible()
    page.keyboard.press("n")
    expect(guide).to_have_count(0)
