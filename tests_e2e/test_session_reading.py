"""Read a long, live transcript with its sticky context on desktop and phone."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, WebSocketRoute, expect

from tests.web_fakes import wait_until
from tests_e2e.flows import log_in_with_pin, open_settings
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


def wait_for_scroll_settled(page: Page) -> None:
    page.evaluate("""async () => {
        let previous = window.scrollY;
        let stable = 0;
        while (stable < 4) {
            await new Promise(resolve => requestAnimationFrame(resolve));
            stable = previous === window.scrollY ? stable + 1 : 0;
            previous = window.scrollY;
        }
    }""")


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
    page.mouse.wheel(0, -180)
    page.wait_for_function(
        "() => document.querySelector('.entry.assistant:last-child')"
        ".getBoundingClientRect().top > 150"
    )
    expect(latest).to_be_hidden()
    position = page.evaluate("window.scrollY")
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "The live answer below the fold."))
    server.watches.open[0].fire()
    new_messages = page.locator("button.new-messages")
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
        open_settings(page)
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

    page.mouse.wheel(0, -100000)
    expect(page.get_by_role("button", name="Latest answer")).to_be_visible()
    position = page.evaluate("window.scrollY")
    # A non-answer first unseen entry makes this jump go straight to the end.
    with transcript.open("a") as stream:
        stream.write(record(26, "user", "New prompt while reading earlier replies."))
        stream.write(record(27, "assistant", "Follow is restored."))
    server.watches.open[-1].fire()
    page.locator("button.new-messages").wait_for()
    assert page.evaluate("window.scrollY") == position
    page.locator("button.new-messages").click()
    expect(page.get_by_text("Follow is restored.", exact=True)).to_be_in_viewport()
    expect(page.locator(".jump-latest")).to_have_count(0)
    with transcript.open("a") as stream:
        stream.write(record(28, "assistant", "Still following."))
    server.watches.open[-1].fire()
    expect(page.get_by_text("Still following.", exact=True)).to_be_in_viewport()
    expect(page.locator(".jump-latest")).to_have_count(0)


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
def test_manual_wheel_to_transcript_end_arms_follow_without_a_jump(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    server, transcript = open_transcript(page, make_server, tmp_path)
    distance = page.locator(".transcript").evaluate(
        "el => Math.ceil(el.getBoundingClientRect().bottom - "
        "document.querySelector('.bottom-guard').getBoundingClientRect().top)"
    )
    page.mouse.wheel(0, distance)
    page.wait_for_function("""() =>
        document.querySelector('.transcript').getBoundingClientRect().bottom <=
        document.querySelector('.bottom-guard').getBoundingClientRect().top + 1
    """)
    wait_for_scroll_settled(page)
    # The trailing answer spacer is not part of the transcript end.
    assert page.evaluate(
        "window.scrollY + window.innerHeight < document.documentElement.scrollHeight - 100"
    )
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "Followed from the content end.\n\n" * 16))
    server.watches.open[-1].fire()
    page.wait_for_function("""() => {
        const entries = document.querySelectorAll('.entry.assistant');
        return entries.length === 25 && entries[24].getBoundingClientRect().bottom <=
            document.querySelector('.bottom-guard').getBoundingClientRect().top + 1;
    }""")


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
def test_keyboard_momentum_leaves_follow_off_after_end(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    server, transcript = open_transcript(page, make_server, tmp_path)
    # No jump button: manually reaching an old session's end arms live follow.
    page.keyboard.press("End")
    expect(page.locator(".jump-latest")).to_have_count(0)
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "Followed after manual End."))
    server.watches.open[-1].fire()
    expect(page.get_by_text("Followed after manual End.", exact=True)).to_be_in_viewport()
    expect(page.locator(".jump-latest")).to_have_count(0)

    # Move into the answer-jump spacer. One animated PageUp starts with the
    # transcript end still visible, then leaves it without another key event.
    wait_for_scroll_settled(page)
    before = page.evaluate("window.scrollY")
    page.mouse.wheel(0, 200)
    page.wait_for_function("previous => window.scrollY > previous + 150", arg=before)
    wait_for_scroll_settled(page)
    page.evaluate("""() => {
        window.scrollFrames = [];
        window.addEventListener('scroll', () => window.scrollFrames.push({
            y: window.scrollY,
            end: document.querySelector('.transcript').getBoundingClientRect().bottom <=
                document.querySelector('.bottom-guard').getBoundingClientRect().top + 1,
        }));
    }""")
    page.keyboard.press("PageUp")
    expect(page.locator(".jump-latest")).to_be_visible()
    page.wait_for_function("""() => {
        const frames = window.scrollFrames;
        return frames.some(frame => frame.end) && frames.some(frame => !frame.end);
    }""")
    wait_for_scroll_settled(page)
    position = page.evaluate("window.scrollY")
    with transcript.open("a") as stream:
        stream.write(record(26, "assistant", "Do not pull the reader back."))
    server.watches.open[-1].fire()
    expect(page.locator("button.new-messages")).to_be_visible()
    assert page.evaluate("window.scrollY") == position


@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_long_answer_jump_does_not_follow_away_from_its_start(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, profile: str
) -> None:
    server, transcript = open_transcript(page, make_server, tmp_path)
    if profile == "eink":
        page.evaluate(
            "localStorage.setItem('jailbee.displayProfile', 'eink'); "
            "localStorage.setItem('jailbee.einkFollow', 'true')"
        )
        page.reload()
        expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    page.get_by_role("button", name="Latest answer").click()
    assert_latest_starts_below_header(page)
    position = page.evaluate("window.scrollY")
    with transcript.open("a") as stream:
        stream.write(record(25, "assistant", "Arrived while reading the long answer."))
    server.watches.open[-1].fire()
    expect(page.locator("button.new-messages")).to_be_visible()
    assert page.evaluate("window.scrollY") == position


def test_eink_default_holds_new_entries_even_at_the_end(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    server, transcript = open_transcript(page, make_server, tmp_path)
    open_settings(page)
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
    expect(page.locator("button.new-messages")).to_be_visible()
    expect(page.get_by_text("Held even at the end.", exact=True)).to_have_count(0)
    assert page.evaluate("window.scrollY") == position
    page.locator("button.new-messages").click()
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
