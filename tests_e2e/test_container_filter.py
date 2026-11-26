"""Container filters through shared-core snapshots and the real browser controls."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import timedelta

import pytest
from playwright.sync_api import Page, expect

from jailbee.accounts.models import AgentActivity
from jailbee.agent_status import AgentSummary, SessionMark
from jailbee.orchestration.view import ChildView
from tests.web_fakes import START, FakeClock, container, repo_group, snapshot_of
from tests_e2e.flows import log_in_with_pin, open_quick_settings, reveal_setting
from tests_e2e.server import E2EServer


@pytest.mark.parametrize("width", [1280, 390])
def test_filters_keep_dimmed_parent_persist_and_follow_snapshots(
    server: E2EServer, logged_in: Page, width: int
) -> None:
    page = logged_in
    page.set_viewport_size({"width": width, "height": 800})
    parent = container("work-orch", repo="work", state="Stopped")
    child = container(
        "work-task",
        repo="work",
        agent_status=(AgentSummary("claude", "busy", None, None, 1),),
        child=ChildView(parent.name, "working", (), False, ()),
    )
    idle = container("work-idle", repo="work")
    stopped = container("other-stopped", repo="other", state="Stopped")
    server.push(snapshot_of(repo_group("work", parent, child, idle), repo_group("other", stopped)))
    rows = page.locator("li.row")
    expect(rows).to_have_count(4)
    open_quick_settings(page)
    running = page.get_by_role("button", name="Running", exact=True)
    running.click()
    expect(running).to_have_attribute("aria-pressed", "true")
    expect(rows).to_have_count(3)
    expect(page.locator('[data-repo="other"]')).to_have_count(0)
    expect(page.locator(".filter-notice")).to_have_text("Showing: Running · show all")
    active = page.get_by_role("button", name="Active", exact=True)
    expect(active).to_have_attribute("title", "Active")
    active.click()
    expect(rows).to_have_count(2)
    expect(rows.first).to_have_attribute("data-container", parent.name)
    expect(rows.first).to_have_class(re.compile(r"\bfilter-context\b"))
    expect(rows.first).to_have_css("opacity", "0.5")
    expect(rows.last).to_have_class(re.compile(r"\bnested\b"))
    expect(rows.last.locator(".indent")).to_have_text("└ ")
    expect(page.locator(".filter-notice")).to_have_text("Showing: Active · show all")
    page.keyboard.press("Escape")
    expect(page.locator(".filter-marker")).to_be_visible()
    page.reload()
    reveal_setting(page, "button", "Active")
    expect(active).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("Escape")
    expect(rows).to_have_count(2)
    page.locator(".repo-heading a").click()
    reveal_setting(page, "button", "Active")
    expect(active).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("Escape")
    expect(rows).to_have_count(2)
    child.agent_status = ()
    server.push(snapshot_of(repo_group("work", parent, child, idle), repo_group("other", stopped)))
    expect(rows).to_have_count(0)
    expect(page.locator(".filter-notice")).to_have_text("Showing: Active · show all")
    expect(page.get_by_text("No matching containers.")).to_be_visible()
    page.get_by_role("button", name="show all", exact=True).click()
    expect(page.locator(".filter-notice")).to_have_count(0)
    expect(page.locator(".filter-marker")).to_have_count(0)
    expect(rows).to_have_count(3)
    expect(page.locator(".filter-context")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_active_explains_recent_idle_in_a_crowded_session_cell(
    make_server: Callable[..., E2EServer], page: Page, profile: str
) -> None:
    clock = FakeClock()
    server = make_server(clock=clock, monotonic=clock.monotonic)
    parent = container("work-parent", repo="work", state="Stopped")
    children = [
        container(
            f"work-{suffix}", repo="work", child=ChildView(parent.name, "working", (), False, ())
        )
        for suffix in ("a", "b", "c")
    ]
    children[1].agent_status = (
        AgentSummary(
            "claude",
            "waiting",
            START,
            "permission",
            20,
            activity=AgentActivity(
                None, None, state="idle", last_event_at=START - timedelta(minutes=5)
            ),
            sessions=(
                SessionMark("waiting", START),
                SessionMark("idle", START - timedelta(hours=2)),
            ),
        ),
    )
    server.push(snapshot_of(repo_group("work", parent, *children)))
    log_in_with_pin(page, server.url, server.pin.current)
    page.set_viewport_size({"width": 390, "height": 844})
    if profile == "eink":
        page.evaluate("localStorage.setItem('jailbee.displayProfile', 'eink')")
        page.reload()
    reveal_setting(page, "button", "Active")
    page.get_by_role("button", name="Active", exact=True).click()
    page.keyboard.press("Escape")
    expect(page.locator("li.row")).to_have_count(2)
    row = page.locator('[data-container="work-b"]')
    expect(row.locator('[data-status="waiting"], [data-status="idle"]')).to_have_count(2)
    expect(row.locator('[data-status="waiting"], [data-status="idle"]').nth(0)).to_have_text(
        "waiting for you"
    )
    expect(row.locator('[data-status="waiting"], [data-status="idle"]').nth(1)).to_have_text(
        "idle 5m"
    )
    expect(row.locator('[data-status="waiting"], [data-status="idle"]').nth(1)).to_have_css(
        "font-weight", "700"
    )
    expect(row.locator(".indent")).to_have_text("└ ")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("link", name="work", exact=True).click()
    expect(row.locator('[data-status="waiting"], [data-status="idle"]')).to_have_count(2)
    expect(row.locator('[data-status="waiting"], [data-status="idle"]').nth(1)).to_have_text(
        "idle 5m"
    )
    expect(row.locator(".indent")).to_have_text("└ ")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
