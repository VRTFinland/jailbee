"""Read a long, live transcript with its sticky context on desktop and phone."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.web_fakes import wait_until
from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer

SESSION = "33333333-3333-4333-8333-333333333333"
TITLE = "Investigate the long session transcript and keep all of this title accessible on a phone"


def record(number: int, kind: str, text: str) -> str:
    return json.dumps(
        {
            "uuid": f"entry-{number}",
            "sessionId": SESSION,
            "timestamp": "2026-11-02T10:00:00.000Z",
            "type": kind,
            "message": {
                "role": kind,
                "content": [{"type": "text", "text": text}] if kind == "assistant" else text,
                "model": "test-model",
            },
        }
    ) + "\n"


def open_transcript(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> tuple[E2EServer, Path]:
    home = tmp_path / "agent-home"
    project = home / "projects" / "-work-repo"
    project.mkdir(parents=True)
    transcript = project / f"{SESSION}.jsonl"
    transcript.write_text(
        record(0, "user", TITLE)
        + "".join(
            record(i, "assistant", f"Reply {i}\n\n" + "A readable paragraph.\n\n" * 16)
            for i in range(1, 25)
        )
    )
    homes = {"work": [("claude", home)]}
    server = make_server(agent_homes=homes.get)
    log_in_with_pin(page, server.url, server.pin.current, "session reader")
    page.goto(f"{server.url}/#/repos/work/sessions/claude/{SESSION}")
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    wait_until(lambda: len(server.watches.open) == 1)
    return server, transcript


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
def test_latest_answer_and_sticky_title_follow_the_readable_area(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    server, transcript = open_transcript(page, make_server, tmp_path)
    latest = page.get_by_role("button", name="Latest answer")
    title = page.get_by_role("heading", name=TITLE, exact=True)
    expect(title).to_have_attribute("title", TITLE)
    expect(latest).to_be_visible()

    latest.click()
    expect(latest).to_be_hidden()
    expect(page.get_by_text("Reply 24", exact=True)).to_be_in_viewport()
    expect(title).to_be_in_viewport()
    assert page.locator(".session-header").evaluate("el => el.getBoundingClientRect().top") == 0

    page.evaluate("window.scrollTo(0, 600)")
    expect(latest).to_be_visible()
    expect(title).to_be_in_viewport()
    latest.click()
    expect(latest).to_be_hidden()

    # Stay on a visible answer, but not at the end: appended answers must not
    # scroll the reader away, and must bring the jump button back.
    page.locator(".entry.assistant").last.evaluate(
        "el => window.scrollTo(0, window.scrollY + el.getBoundingClientRect().top - 300)"
    )
    expect(latest).to_be_hidden()
    position = page.evaluate("window.scrollY")
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "The live answer below the fold."))
    server.watches.open[0].fire()
    expect(page.get_by_text("The live answer below the fold.", exact=True)).to_be_attached()
    expect(latest).to_be_visible()
    assert page.evaluate("window.scrollY") == position
    expect(title).to_be_in_viewport()
    latest.click()
    expect(latest).to_be_hidden()
    expect(page.get_by_text("The live answer below the fold.", exact=True)).to_be_in_viewport()


def test_short_landscape_page_turns_do_not_skip_text(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    page.set_viewport_size({"width": 640, "height": 320})
    open_transcript(page, make_server, tmp_path)
    page.evaluate("localStorage.setItem('jailbee.displayProfile', 'eink')")
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    # A larger guard stands in for a device's nonzero safe-area inset.
    page.evaluate("document.documentElement.style.setProperty('--guard-bottom', '60px')")
    next_page = page.get_by_role("button", name="Next", exact=False).last
    for _ in range(4):
        before = page.evaluate("window.scrollY")
        readable = page.locator(".page-nav").evaluate(
            "el => el.getBoundingClientRect().top - "
            "document.querySelector('.session-header').getBoundingClientRect().bottom"
        )
        assert 0 < readable < 100
        next_page.click()
        expect(page.locator(".page-indicator")).not_to_have_text("Page 1 / 1")
        page.wait_for_function("previous => window.scrollY > previous", arg=before)
        moved = page.evaluate("window.scrollY") - before
        assert 0 < moved <= readable + 1
