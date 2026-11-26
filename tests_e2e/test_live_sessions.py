"""Live-only lists and the one-session shortcut through real browser navigation."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from sqlmodel import Session

from jailbee.agent_status import AgentSummary
from jailbee.db.models import ContainerSession
from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.flows import open_settings
from tests_e2e.server import E2EServer
from tests_e2e.sessions import OTHER_TITLE, SESSION_ID, SESSION_TITLE

OTHER_ID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def server(make_server: Callable[..., E2EServer], tmp_path: Path) -> E2EServer:
    home = tmp_path / "claude"
    fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/transcripts/claude"
    shutil.copytree(fixtures, home)
    server = make_server(agent_homes={"work": [("claude", home)]}.get)
    push_live(server, SESSION_ID)
    stamp = datetime(2026, 11, 2, tzinfo=UTC)
    with Session(server.engine) as db:
        db.add(ContainerSession(container_name="work-main", agent="claude", session_id=OTHER_ID,
                                first_seen=stamp, last_seen=stamp))
        db.commit()
    return server


def push_live(server: E2EServer, *ids: str) -> None:
    live = AgentSummary("claude", "busy", None, None, len(ids), session_ids=ids)
    server.push(snapshot_of(repo_group("work", container("work-main", "work", agent_status=(live,)))))


@pytest.mark.parametrize("entry", ["main", "work"])
def test_single_live_session_replaces_list_and_back_reaches_front_page(
    logged_in: Page, server: E2EServer, entry: str
) -> None:
    page = logged_in
    page.get_by_role("link", name=entry, exact=True).click()
    expect(page.get_by_role("heading", name=SESSION_TITLE, exact=True)).to_be_visible()
    page.go_back()
    expect(page.get_by_role("heading", name="JailBee", exact=True)).to_be_visible()
    page.get_by_role("link", name=entry, exact=True).click()
    expect(page.get_by_role("heading", name=SESSION_TITLE, exact=True)).to_be_visible()
    page.get_by_role("navigation", name="Breadcrumb").get_by_role("link", name="work", exact=True).click()
    expect(page.get_by_role("heading", name="work", exact=True)).to_be_visible()
    expect(page.get_by_role("switch", name="Live only", exact=True)).to_be_checked()
    expect(page.locator("li.session")).to_have_count(1)
    page.reload()
    expect(page.get_by_role("heading", name=SESSION_TITLE, exact=True)).to_be_visible()


@pytest.mark.parametrize("entry", ["main", "work"])
def test_two_live_sessions_keep_the_list_and_toggle_reveals_history(
    logged_in: Page, server: E2EServer, entry: str
) -> None:
    page = logged_in
    # A running session without a transcript still appears in the container live list.
    second = "33333333-3333-4333-8333-333333333333"
    push_live(server, SESSION_ID, second)
    page.get_by_role("link", name=entry, exact=True).click()
    expect(page.get_by_role("heading", name="work" if entry == "work" else "work-main", exact=True)).to_be_visible()
    expect(page.locator("li.session")).to_have_count(2)
    expect(page.get_by_role("searchbox", name="Search earlier sessions")).to_have_count(0)
    page.get_by_role("switch", name="Live only", exact=True).uncheck()
    expect(page.get_by_text("All sessions", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name=OTHER_TITLE)).to_be_visible()
    expect(page.locator("li.session")).to_have_count(3)
    if entry == "main":
        expect(page.get_by_role("searchbox", name="Search earlier sessions")).to_be_visible()


def test_session_preferences_survive_reload_and_no_live_has_one_tap_escape(
    logged_in: Page, server: E2EServer
) -> None:
    page = logged_in
    open_settings(page)
    page.get_by_role("switch", name="Open the only live session directly").uncheck()
    page.get_by_role("switch", name="Show only live sessions").uncheck()
    page.reload()
    expect(page.get_by_role("switch", name="Show only live sessions")).not_to_be_checked()
    expect(page.get_by_role("switch", name="Open the only live session directly")).not_to_be_checked()
    page.get_by_role("link", name="JailBee", exact=True).click()
    page.get_by_role("link", name="main", exact=True).click()
    expect(page.get_by_role("switch", name="Live only", exact=True)).not_to_be_checked()
    expect(page.get_by_role("link", name=OTHER_TITLE)).to_be_visible()
    page.get_by_role("switch", name="Live only", exact=True).check()
    push_live(server)
    page.reload()
    # The view toggle did not change the device preference.
    expect(page.get_by_role("switch", name="Live only", exact=True)).not_to_be_checked()
    page.get_by_role("switch", name="Live only", exact=True).check()
    expect(page.get_by_text("No live sessions.", exact=True)).to_be_visible()
    page.get_by_role("button", name="Show all sessions", exact=True).click()
    expect(page.get_by_role("link", name=OTHER_TITLE)).to_be_visible()
