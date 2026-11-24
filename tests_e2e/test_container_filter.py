"""Container filters through shared-core snapshots and the real browser controls."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from jailbee.agent_status import AgentSummary
from jailbee.orchestration.view import ChildView
from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.server import E2EServer


@pytest.mark.parametrize("width", [1280, 390])
def test_filters_keep_dimmed_parent_persist_and_follow_snapshots(
    server: E2EServer, logged_in: Page, width: int
) -> None:
    page = logged_in
    page.set_viewport_size({"width": width, "height": 800})
    parent = container("work-orch", repo="work", state="Stopped")
    child = container(
        "work-task", repo="work",
        agent_status=(AgentSummary("claude", "busy", None, None, 1),),
        child=ChildView(parent.name, "working", (), False, ()),
    )
    idle = container("work-idle", repo="work")
    stopped = container("other-stopped", repo="other", state="Stopped")
    server.push(snapshot_of(repo_group("work", parent, child, idle), repo_group("other", stopped)))
    rows = page.locator("li.row")
    expect(rows).to_have_count(4)
    running = page.get_by_role("button", name="Running", exact=True)
    running.click()
    expect(running).to_have_attribute("aria-pressed", "true")
    expect(rows).to_have_count(3)
    expect(page.locator('[data-repo="other"]')).to_have_count(0)
    expect(page.get_by_role("status")).to_have_text("1 hidden by filter")
    active = page.get_by_role("button", name="Active", exact=True)
    expect(active).to_have_attribute("title", "Active")
    active.click()
    expect(rows).to_have_count(2)
    expect(rows.first).to_have_attribute("data-container", parent.name)
    expect(rows.first).to_have_class(re.compile(r"\bfilter-context\b"))
    expect(rows.first).to_have_css("opacity", "0.5")
    expect(rows.last).to_have_class(re.compile(r"\bnested\b"))
    expect(rows.last.locator(".indent")).to_have_text("└ ")
    expect(page.get_by_role("status")).to_have_text("2 hidden by filter")
    page.reload()
    expect(active).to_have_attribute("aria-pressed", "true")
    expect(rows).to_have_count(2)
    page.locator(".repo-heading a").click()
    expect(active).to_have_attribute("aria-pressed", "true")
    expect(rows).to_have_count(2)
    child.agent_status = ()
    server.push(snapshot_of(repo_group("work", parent, child, idle), repo_group("other", stopped)))
    expect(rows).to_have_count(0)
    expect(page.get_by_role("status")).to_have_text("3 hidden by filter")
    expect(page.get_by_text("No matching containers.")).to_be_visible()
    page.get_by_role("button", name="All", exact=True).click()
    expect(rows).to_have_count(3)
    expect(page.locator(".filter-context")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
