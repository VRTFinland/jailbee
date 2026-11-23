"""Read hand-written agent sessions through the real server in Chrome."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from jailbee.agent_status import AgentSummary
from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.server import E2EServer
from tests_e2e.sessions import (
    OTHER_TITLE,
    SESSION_ID,
    SESSION_TITLE,
    append_entry,
    open_session,
    session_path,
)

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "transcripts" / "claude"


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    home = tmp_path / "claude"
    shutil.copytree(FIXTURES, home)
    return home


@pytest.fixture
def server(make_server: Callable[..., E2EServer], agent_home: Path) -> E2EServer:
    server = make_server(agent_homes={"work": [("claude", agent_home)]}.get)
    live = AgentSummary("claude", "busy", None, None, 1, session_ids=(SESSION_ID,))
    server.push(
        snapshot_of(repo_group("work", container("work-main", "work", agent_status=(live,))))
    )
    return server


@pytest.fixture(params=[{"width": 1280, "height": 800}, {"width": 390, "height": 844}],
                ids=["desktop", "phone"])
def session_page(logged_in: Page, request: pytest.FixtureRequest) -> Page:
    logged_in.set_viewport_size(request.param)
    return logged_in


def assert_no_horizontal_scroll(page: Page) -> None:
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_repo_and_container_list_sessions_and_open_their_transcript(session_page: Page) -> None:
    page = session_page
    page.get_by_role("link", name="work", exact=True).click()
    expect(page.get_by_role("link", name=SESSION_TITLE)).to_be_visible()
    expect(page.get_by_role("link", name=OTHER_TITLE)).to_be_visible()
    assert_no_horizontal_scroll(page)
    open_session(page)
    expect(page.get_by_text("I'll read the file.", exact=True)).to_be_visible()
    expect(page.get_by_text("Done.", exact=True)).to_be_visible()
    assert_no_horizontal_scroll(page)

    page.get_by_role("link", name="← work", exact=True).click()
    page.get_by_role("link", name="main", exact=True).click()
    expect(page.get_by_role("heading", name="work-main", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name=OTHER_TITLE)).to_have_count(0)
    expect(page.get_by_role("link", name=SESSION_TITLE)).to_contain_text("live")
    assert_no_horizontal_scroll(page)
    open_session(page)
    expect(page.get_by_text("Done.", exact=True)).to_be_visible()
    assert_no_horizontal_scroll(page)


def test_markdown_code_folded_tools_and_subagent_transcript(
    session_page: Page, server: E2EServer, agent_home: Path
) -> None:
    page = session_page
    append_entry(server, session_path(agent_home), "## Result\n\nA **working** fix.\n\n```python\ndef verified():\n    return True\n```")
    page.get_by_role("link", name="work", exact=True).click()
    open_session(page)
    expect(page.get_by_role("heading", name="Result", exact=True)).to_be_visible()
    expect(page.locator("strong", has_text="working")).to_be_visible()
    expect(page.locator("pre code").filter(has_text="def verified():")).to_be_visible()
    read = page.get_by_role("button", name="Read /work/repo/auth.py", exact=True)
    expect(read).to_have_attribute("aria-expanded", "false")
    expect(page.get_by_text("def login():", exact=False)).to_have_count(0)
    read.click()
    expect(read).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_text("def login():", exact=False)).to_be_visible()
    assert_no_horizontal_scroll(page)
    page.get_by_role("link", name="Subagent: Review auth", exact=True).click()
    expect(page.get_by_text("Review it.", exact=True)).to_be_visible()
    expect(page.get_by_text("Looks fine.", exact=True)).to_be_visible()
    assert_no_horizontal_scroll(page)
    page.get_by_role("link", name="Parent session").click()
    expect(page.get_by_text("Done.", exact=True)).to_be_visible()


def test_live_append_and_latest_answer_jump(
    session_page: Page, server: E2EServer, agent_home: Path
) -> None:
    page = session_page
    path = session_path(agent_home)
    # A long prompt makes the newest answer genuinely outside the viewport.
    append_entry(server, path, "\n\n".join(f"Context paragraph {n}." for n in range(50)), role="user")
    page.get_by_role("link", name="work", exact=True).click()
    open_session(page)
    page.get_by_role("heading", name=SESSION_TITLE, exact=True).scroll_into_view_if_needed()
    append_entry(server, path, "An initial live reply.")
    expect(page.get_by_text("An initial live reply.", exact=True)).to_have_count(1)
    append_entry(server, path, "The newest live reply.")
    reply = page.get_by_text("The newest live reply.", exact=True)
    expect(reply).to_have_count(1)
    expect(reply).not_to_be_in_viewport()
    page.get_by_role("button", name="Latest answer").click()
    expect(reply).to_be_in_viewport()
    expect(page.get_by_role("button", name="Latest answer")).to_have_count(0)
    expect(page.get_by_text("Done.", exact=True)).not_to_be_in_viewport()
    assert_no_horizontal_scroll(page)


def test_thinking_is_hidden_until_enabled_in_settings(session_page: Page) -> None:
    page = session_page
    page.get_by_role("link", name="work", exact=True).click()
    open_session(page)
    expect(page.get_by_text("Read auth.py first.", exact=True)).to_have_count(0)
    page.get_by_role("link", name="Settings", exact=True).click()
    page.get_by_role("checkbox", name="Show thinking").check()
    page.go_back()
    expect(page.get_by_text("Read auth.py first.", exact=True)).to_be_visible()
    expect(page.get_by_text("Done.", exact=True)).to_be_visible()
    assert_no_horizontal_scroll(page)


def test_follow_resumes_after_restart_without_losing_or_duplicating_entries(
    session_page: Page, server: E2EServer, agent_home: Path, browser_errors: BrowserErrors
) -> None:
    page = session_page
    path = session_path(agent_home)
    page.get_by_role("link", name="work", exact=True).click()
    open_session(page)
    append_entry(server, path, "Reply before restart.")
    expect(page.get_by_text("Reply before restart.", exact=True)).to_have_count(1)

    refused = re.compile(
        rf"WebSocket connection to 'ws://127\.0\.0\.1:{server.port}/api/ws' failed: "
        r"Error in connection establishment: net::ERR_CONNECTION_REFUSED"
    )
    browser_errors.expect("console", refused, at_least=1)
    try:
        with page.expect_console_message(
            predicate=lambda message: refused.fullmatch(message.text) is not None
        ):
            server.stop()
        expect(page.get_by_text("Connecting…", exact=True)).to_be_visible()
        append_entry(server, path, "First reply while offline.")
        append_entry(server, path, "Second reply while offline.")
    finally:
        server.start()

    expect(page.get_by_text("Second reply while offline.", exact=True)).to_have_count(1)
    expect(page.get_by_text("Connecting…", exact=True)).to_have_count(0)
    append_entry(server, path, "Reply after restart.")
    replies = page.locator("article.transcript .entry.assistant .prose")
    expect(replies).to_have_text([
        "I'll read the file.",
        "Done.",
        "Reply before restart.",
        "First reply while offline.",
        "Second reply while offline.",
        "Reply after restart.",
    ])
    page.get_by_text("Reply after restart.", exact=True).scroll_into_view_if_needed()
    expect(page.get_by_text("Reply after restart.", exact=True)).to_be_in_viewport()
    assert_no_horizontal_scroll(page)
