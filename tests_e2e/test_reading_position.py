"""Remember transcript entries across reloads and typography changes."""

import json
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Page, expect

from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import (
    SESSION,
    TITLE,
    open_transcript,
    record,
    wait_for_scroll_settled,
)


def assert_at_transcript_end(page: Page) -> None:
    page.wait_for_function("""() => {
        const bottom = document.querySelector('.transcript')?.getBoundingClientRect().bottom;
        const boundary = (document.querySelector('.page-nav[data-mode="bar"]') ??
            document.querySelector('.bottom-guard')).getBoundingClientRect().top;
        return bottom !== undefined && Math.abs(bottom - boundary) <= 2;
    }""")


def test_restores_tool_call_below_title_bar(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    server, transcript = open_transcript(page, make_server, tmp_path)
    tool = {
        "uuid": "remembered-tool",
        "sessionId": SESSION,
        "type": "assistant",
        "timestamp": "2026-11-02T10:00:00.000Z",
        "message": {
            "role": "assistant",
            "model": "test-model",
            "content": [
                {
                    "type": "tool_use",
                    "id": "read-position",
                    "name": "Read",
                    "input": {"file_path": "/work/repo/file.py"},
                }
            ],
        },
    }
    with transcript.open("a") as stream:
        stream.write(json.dumps(tool) + "\n")
        stream.write(record(26, "assistant", "After the tool.\n\n" * 100))
    server.watches.open[-1].fire()
    call = page.locator(".tool-call[id]")
    expect(call).to_have_count(1)
    call_id = call.get_attribute("id")
    # Position the tool as the first entry at the readable top, independently
    # of the scroll-margin rule used by restoration.
    call.evaluate("""el => {
        const top = document.querySelector('.title-bar').getBoundingClientRect().bottom;
        window.scrollTo(0, window.scrollY + el.getBoundingClientRect().top - top);
    }""")
    wait_for_scroll_settled(page)
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    page.wait_for_function(
        """id => {
        const call = document.getElementById(id);
        const bar = document.querySelector('.title-bar');
        return call && bar && Math.abs(call.getBoundingClientRect().top -
            bar.getBoundingClientRect().bottom) <= 2;
    }""",
        arg=call_id,
    )


def test_reloads_at_actual_transcript_end_with_follow_enabled(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    server, transcript = open_transcript(page, make_server, tmp_path)
    page.keyboard.press("j")
    page.get_by_role("dialog", name="Jump to").get_by_role("button", name="End", exact=True).click()
    assert_at_transcript_end(page)
    wait_for_scroll_settled(page)
    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    assert_at_transcript_end(page)
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "Following after restored end.\n\n" * 30))
    server.watches.open[-1].fire()
    expect(page.get_by_text("Following after restored end.", exact=True).first).to_be_attached()
    assert_at_transcript_end(page)


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
