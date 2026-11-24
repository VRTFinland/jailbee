"""Read a long, live transcript with its sticky context on desktop and phone."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, WebSocketRoute, expect

from tests.web_fakes import wait_until
from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer

SESSION = "33333333-3333-4333-8333-333333333333"
TITLE = "Investigate the long session transcript and keep all of this title accessible on a phone"


def record(number: int, kind: str, text: str) -> str:
    return (
        json.dumps(
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
        )
        + "\n"
    )


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
    log_in_with_pin(page, server.url, server.pin.current)
    page.goto(f"{server.url}/#/repos/work/sessions/claude/{SESSION}")
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    wait_until(lambda: len(server.watches.open) == 1)
    return server, transcript


def assert_latest_starts_below_header(page: Page) -> None:
    page.wait_for_function("""() => {
        const replies = document.querySelectorAll('.entry.assistant');
        const reply = replies[replies.length - 1].getBoundingClientRect();
        const header = document.querySelector('.session-header').getBoundingClientRect();
        return Math.abs(reply.top - header.bottom) <= 2;
    }""")


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_latest_answer_and_sticky_title_follow_the_readable_area(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int, profile: str
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    server, transcript = open_transcript(page, make_server, tmp_path)
    if profile == "eink":
        page.evaluate("localStorage.setItem('jailbee.displayProfile', 'eink')")
        page.reload()
        expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    latest = page.get_by_role("button", name="Latest answer")
    title = page.get_by_role("heading", name=TITLE, exact=True)
    expect(title).to_have_attribute("title", TITLE)
    expect(latest).to_be_visible()

    latest.click()
    expect(page.get_by_role("button", name="Scroll to bottom")).to_be_visible()
    expect(page.get_by_text("Reply 24", exact=True)).to_be_in_viewport()
    assert_latest_starts_below_header(page)
    expect(title).to_be_in_viewport()
    assert page.locator(".session-header").evaluate("el => el.getBoundingClientRect().top") == 0

    page.evaluate("window.scrollTo(0, 600)")
    expect(latest).to_be_visible()
    expect(title).to_be_in_viewport()
    latest.click()
    expect(page.get_by_role("button", name="Scroll to bottom")).to_be_visible()

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
    new_messages = page.get_by_role("button", name="New messages")
    expect(new_messages).to_be_visible()
    assert page.evaluate("window.scrollY") == position
    expect(title).to_be_in_viewport()
    new_messages.click()
    expect(new_messages).to_be_hidden()
    expect(page.get_by_text("The live answer below the fold.", exact=True)).to_be_in_viewport()
    assert_latest_starts_below_header(page)


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_end_follow_and_new_messages_restore_follow(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int, profile: str
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    server, transcript = open_transcript(page, make_server, tmp_path)
    if profile == "eink":
        page.get_by_role("link", name="Settings", exact=True).click()
        page.get_by_role("radio", name="E-ink (page turns").check()
        page.get_by_role("checkbox", name="Follow new messages on e-ink").check()
        page.go_back()
        expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    page.get_by_role("button", name="Latest answer").click()
    page.get_by_role("button", name="Scroll to bottom").click()
    expect(page.locator(".jump-latest")).to_have_count(0)
    if profile == "eink":
        expect(page.get_by_role("button", name="Next", exact=False).last).to_be_disabled()

    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "Automatically followed answer."))
    server.watches.open[-1].fire()
    reply = page.get_by_text("Automatically followed answer.", exact=True)
    expect(reply).to_be_in_viewport()
    expect(page.locator(".jump-latest")).to_have_count(0)
    if profile == "eink":
        expect(page.get_by_role("button", name="Next", exact=False).last).to_be_disabled()

    page.evaluate("window.scrollTo(0, 600)")
    expect(page.get_by_role("button", name="Latest answer")).to_be_visible()
    position = page.evaluate("window.scrollY")
    # A non-answer first unseen entry makes this jump go straight to the end.
    with transcript.open("a") as stream:
        stream.write(record(26, "user", "New prompt while reading earlier replies."))
        stream.write(record(27, "assistant", "Follow is restored."))
    server.watches.open[-1].fire()
    page.get_by_role("button", name="New messages").wait_for()
    assert page.evaluate("window.scrollY") == position
    page.get_by_role("button", name="New messages").click()
    expect(page.get_by_text("Follow is restored.", exact=True)).to_be_in_viewport()
    expect(page.locator(".jump-latest")).to_have_count(0)
    with transcript.open("a") as stream:
        stream.write(record(28, "assistant", "Still following."))
    server.watches.open[-1].fire()
    expect(page.get_by_text("Still following.", exact=True)).to_be_in_viewport()
    expect(page.locator(".jump-latest")).to_have_count(0)


def test_eink_default_holds_new_entries_even_at_the_end(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    server, transcript = open_transcript(page, make_server, tmp_path)
    page.get_by_role("link", name="Settings", exact=True).click()
    page.get_by_role("radio", name="E-ink (page turns").check()
    expect(page.get_by_role("checkbox", name="Follow new messages on e-ink")).not_to_be_checked()
    page.go_back()
    page.get_by_role("button", name="Latest answer").click()
    page.get_by_role("button", name="Scroll to bottom").click()
    expect(page.locator(".jump-latest")).to_have_count(0)
    position = page.evaluate("window.scrollY")
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "Held even at the end."))
    server.watches.open[-1].fire()
    expect(page.get_by_role("button", name="New messages")).to_be_visible()
    expect(page.get_by_text("Held even at the end.", exact=True)).to_have_count(0)
    assert page.evaluate("window.scrollY") == position
    page.get_by_role("button", name="New messages").click()
    expect(page.get_by_text("Held even at the end.", exact=True)).to_be_in_viewport()


def test_partial_keyboard_scroll_keeps_the_current_eink_page(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    open_transcript(page, make_server, tmp_path)
    page.evaluate("localStorage.setItem('jailbee.displayProfile', 'eink')")
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    expect(page.locator(".page-indicator")).to_contain_text("Page 1 /")
    readable = page.locator(".page-nav").evaluate(
        "el => el.getBoundingClientRect().top - "
        "document.querySelector('.session-header').getBoundingClientRect().bottom"
    )
    # Sync point: the scroll has landed and its handler has run (scroll events
    # fire before the next animation frame's callbacks).
    page.evaluate(
        """async height => {
            const target = Math.floor(height * 0.6);
            window.scrollTo(0, target);
            const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
            while (Math.abs(window.scrollY - target) > 1) await frame();
            await frame();
        }""",
        readable,
    )
    expect(page.locator(".page-indicator")).to_contain_text("Page 1 /")
    readable = page.locator(".page-nav").evaluate(
        "el => Math.floor(el.getBoundingClientRect().top - "
        "document.querySelector('.session-header').getBoundingClientRect().bottom)"
    )
    page.get_by_role("button", name="Next", exact=False).last.click()
    page.wait_for_function("height => Math.abs(window.scrollY - height) <= 1", arg=readable)


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


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_out_of_date_banner_leaves_the_session_title_bar_visible(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int, profile: str
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    open_transcript(page, make_server, tmp_path)
    if profile == "eink":
        # Applied by the reload below.
        page.evaluate("localStorage.setItem('jailbee.displayProfile', 'eink')")
    stale = False
    links: list[tuple[WebSocketRoute, WebSocketRoute]] = []
    hellos: list[str] = []

    def connect(route: WebSocketRoute) -> None:
        upstream = route.connect_to_server()
        links.append((route, upstream))

        def stale_auth(message: str | bytes) -> None:
            payload = json.loads(message)
            if stale and payload["type"] == "auth":
                payload["v"] = -1
            upstream.send(json.dumps(payload))

        def from_server(message: str | bytes) -> None:
            if json.loads(message)["type"] == "hello":
                hellos.append("hello")
            route.send(message)

        route.on_message(stale_auth)
        upstream.on_message(from_server)
        upstream.on_close(lambda code, reason: route.close(code=code, reason=reason))

    # Routed only now: Playwright forwards routed frames only while it is
    # called, which open_transcript's plain polling would starve.
    page.route_web_socket("**/api/ws", connect)
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    for _ in range(100):
        if hellos:
            break
        page.wait_for_timeout(100)
    assert hellos == ["hello"]
    page.evaluate("window.scrollTo(0, 3000)")
    page.wait_for_function("window.scrollY > 2000")

    # The server is upgraded while the reader is mid-transcript; this tab just
    # reloaded for an upgrade, so it shows the banner instead of reloading.
    page.evaluate("sessionStorage.setItem('jailbee.protocolReload', String(Date.now()))")
    stale = True
    route, upstream = links[-1]
    upstream.close(code=1012, reason="restart")
    route.close(code=1012, reason="restart")
    expect(page.get_by_role("alert")).to_have_text("This page is out of date; reload.")

    title_bar_clear = """() => {
        const banner = document.querySelector('.outdated').getBoundingClientRect();
        const header = document.querySelector('.session-header').getBoundingClientRect();
        return header.top >= banner.bottom - 0.5;
    }"""
    page.wait_for_function(title_bar_clear)
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_in_viewport()
    # The readable area now starts below banner and header together.
    page.get_by_role("button", name="Latest answer").click()
    assert_latest_starts_below_header(page)
    page.wait_for_function(title_bar_clear)
    if profile == "eink":
        # A page turn back moves by at most the area between header and bar.
        before = page.evaluate("window.scrollY")
        readable = page.locator(".page-nav").evaluate(
            "el => el.getBoundingClientRect().top - "
            "document.querySelector('.session-header').getBoundingClientRect().bottom"
        )
        page.get_by_role("button", name="Previous", exact=False).last.click()
        page.wait_for_function("previous => window.scrollY < previous", arg=before)
        assert 0 < before - page.evaluate("window.scrollY") <= readable + 1
        page.wait_for_function(title_bar_clear)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_function(title_bar_clear)
