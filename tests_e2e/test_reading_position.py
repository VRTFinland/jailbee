"""The reader opens at the latest content; explicit message anchors take precedence."""

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import TITLE, open_transcript, record, wait_for_scroll_settled


def assert_at_transcript_end(page: Page) -> None:
    page.wait_for_function("""() => {
        const bottom = document.querySelector('.transcript')?.getBoundingClientRect().bottom;
        const composer = document.querySelector('.agent-composer');
        const nav = document.querySelector('.page-nav');
        const boundary = Math.min(composer?.getBoundingClientRect().top ?? innerHeight,
            nav?.getBoundingClientRect().top ?? innerHeight);
        return bottom !== undefined && Math.abs(bottom - boundary) <= 2;
    }""")


@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_opens_and_reloads_at_end_even_after_reading_earlier_content(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, profile: str
) -> None:
    page.set_viewport_size({"width": 1280, "height": 800})
    open_transcript(page, make_server, tmp_path)
    assert_at_transcript_end(page)
    page.evaluate(
        """profile => {
            localStorage.setItem('jailbee.displayProfile', profile);
            localStorage.setItem('jailbee.followNewMessages', '0');
            localStorage.setItem('jailbee.einkFollow', '0');
        }""", profile,
    )
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    assert_at_transcript_end(page)
    if profile == "eink":
        page.wait_for_function("""() => {
            const [current, total] = document.querySelector('.page-indicator').textContent.split('/').map(Number);
            return current > 1 && current === total;
        }""")
    page.evaluate("window.scrollTo(0, 500)")
    wait_for_scroll_settled(page)
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    assert_at_transcript_end(page)


@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_explicit_message_anchor_wins_over_initial_end(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, profile: str
) -> None:
    open_transcript(page, make_server, tmp_path)
    page.evaluate("profile => localStorage.setItem('jailbee.displayProfile', profile)", profile)
    target = page.locator('.entry.assistant').nth(8)
    anchor = target.get_attribute("id")
    assert anchor is not None
    page.goto(page.url + "#" + anchor)
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    page.wait_for_function("""id => {
        const entry = document.getElementById(id);
        const top = document.querySelector('.title-bar').getBoundingClientRect().bottom;
        return entry && Math.abs(entry.getBoundingClientRect().top - top) <= 2;
    }""", arg=anchor)


def test_end_follows_but_reading_earlier_content_does_not_jump(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    server, transcript = open_transcript(page, make_server, tmp_path)
    assert_at_transcript_end(page)
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "Following initial end.\n\n" * 30))
    server.watches.open[-1].fire()
    expect(page.get_by_text("Following initial end.", exact=True).first).to_be_attached()
    assert_at_transcript_end(page)
    page.mouse.move(400, 300)
    page.mouse.wheel(0, -1500)
    page.wait_for_function("() => document.querySelector('.transcript').getBoundingClientRect().bottom > innerHeight + 500")
    wait_for_scroll_settled(page)
    position = page.evaluate("window.scrollY")
    with transcript.open("a") as stream:
        stream.write(record(26, "assistant", "Do not jump while reading.\n\n" * 30))
    server.watches.open[-1].fire()
    expect(page.get_by_text("Do not jump while reading.", exact=True).first).to_be_attached()
    assert page.evaluate("window.scrollY") == position
