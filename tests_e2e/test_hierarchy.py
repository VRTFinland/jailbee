"""Orchestration relationships through real snapshots, links and browser layout."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from jailbee.orchestration.view import ChildView, GrantView
from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.flows import open_settings
from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import wait_for_scroll_settled


@pytest.mark.parametrize("width", [1280, 390])
def test_hierarchy_links_counts_and_live_renesting(
    server: E2EServer, logged_in: Page, width: int
) -> None:
    page = logged_in
    page.set_viewport_size({"width": width, "height": 800})
    parent = container("work-orch", repo="work", grant=GrantView(4, (), (), False, ()))
    first = container(
        "work-first",
        repo="work",
        child=ChildView(parent.name, "working", (), False, (), "codex/opus"),
    )
    second = container(
        "work-second", repo="work", child=ChildView(parent.name, "imported", (), False, ())
    )
    server.push(snapshot_of(repo_group("work", first, parent, second)))
    rows = page.locator("li.row")
    expect(rows).to_have_count(3)
    expect(rows.nth(0)).to_have_attribute("data-container", parent.name)
    expect(rows.nth(0)).to_contain_text("Orchestrator · 2 children")
    expect(rows.nth(1)).to_have_class(re.compile(r"\bnested\b"))
    expect(rows.nth(2)).to_have_class(re.compile(r"\bnested\b"))
    parent_box = rows.nth(0).bounding_box()
    child_box = rows.nth(1).bounding_box()
    assert parent_box is not None and child_box is not None
    assert child_box["x"] > parent_box["x"]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    rows.nth(1).locator(".name").click()
    expect(page.get_by_role("heading", name=first.name, exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.locator(".parent").get_by_role("link", name="orch", exact=True).click()
    expect(page.get_by_role("heading", name="Children (2)")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    child_row = (
        page.get_by_role("region", name="Children")
        .get_by_role("listitem")
        .filter(has=page.get_by_role("link", name="second", exact=True))
    )
    box = child_row.bounding_box()
    assert box
    # The padding, not just the name, opens the child container.
    child_row.click(position={"x": box["width"] - 8, "y": box["height"] / 2})
    expect(page.get_by_role("heading", name=second.name, exact=True)).to_be_visible()
    page.get_by_role("navigation", name="Breadcrumb").get_by_role(
        "link", name="JailBee", exact=True
    ).click()
    page.locator(".repo-heading").get_by_role("link", name="work", exact=True).click()
    expect(page.locator("li.nested")).to_have_count(2)
    server.push(snapshot_of(repo_group("work", second, parent)))
    expect(page.locator("li.row")).to_have_count(2)
    expect(page.locator("li.nested")).to_have_count(1)
    expect(page.locator("li.row").first).to_contain_text("Orchestrator · 1 child")
    expect(page.locator("li.row").last).to_have_attribute("data-container", second.name)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_cross_repo_counts_and_missing_parent_labels_agree_across_views(
    server: E2EServer, logged_in: Page
) -> None:
    page = logged_in
    parent = container("alpha-orch")
    child = container(
        "beta-task", repo="beta", child=ChildView(parent.name, "working", (), False, ())
    )
    server.push(snapshot_of(repo_group("alpha", parent), repo_group("beta", child)))
    parent_row = page.locator('[data-container="alpha-orch"]')
    child_row = page.locator('[data-container="beta-task"]')
    expect(parent_row).to_contain_text("Orchestrator · 1 child")
    expect(child_row).to_contain_text("Child of orch")
    expect(child_row).not_to_contain_text("orchestrator not listed")
    page.locator('[data-repo="alpha"] .repo-heading a').click()
    expect(parent_row).to_contain_text("Orchestrator · 1 child")
    parent_row.locator(".name").click()
    expect(page.get_by_role("heading", name="Children (1)")).to_be_visible()
    page.get_by_role("region", name="Children").get_by_role("link", name="task", exact=True).click()
    expect(page.locator(".parent")).not_to_contain_text("orchestrator not listed")
    server.push(snapshot_of(repo_group("alpha"), repo_group("beta", child)))
    expect(page.locator(".parent")).to_contain_text("orchestrator not listed")
    page.get_by_role("navigation", name="Breadcrumb").get_by_role(
        "link", name="JailBee", exact=True
    ).click()
    expect(child_row).to_contain_text("orchestrator not listed")
    page.locator('[data-repo="beta"] .repo-heading a').click()
    expect(child_row).to_contain_text("orchestrator not listed")
    server.push(snapshot_of(repo_group("alpha", parent), repo_group("beta", child)))
    expect(child_row).not_to_contain_text("orchestrator not listed")


def test_nested_eink_rows_keep_phase_chips_distinct(server: E2EServer, logged_in: Page) -> None:
    page = logged_in
    open_settings(page)
    page.get_by_role("group", name="Theme", exact=True).get_by_label("E-ink", exact=True).check()
    page.get_by_role("link", name="JailBee", exact=False).last.click()
    parent = container("work-orch", repo="work")
    children = [
        container("work-rest", repo="work", child=ChildView(parent.name, "working", (), False, ())),
        container(
            "work-job",
            repo="work",
            child=ChildView(parent.name, "working", (), False, ()),
            job_kind="create",
            job_phase="creating",
            job_pid=1,
        ),
        container(
            "work-boot",
            repo="work",
            child=ChildView(parent.name, "working", (), False, ()),
            job_kind="boot",
            job_phase="starting",
            job_pid=1,
            job_live=True,
        ),
    ]
    server.push(snapshot_of(repo_group("work", parent, *children)))
    rest = page.locator('[data-container="work-rest"]')
    job = page.locator('[data-container="work-job"]')
    boot = page.locator('[data-container="work-boot"]')
    expect(rest.locator(".status-chip")).to_have_count(0)
    expect(job.locator(".status-chip")).to_have_text("job")
    expect(boot.locator(".status-chip")).to_have_text("booting")
    assert job.locator(".status-chip svg path").get_attribute("d") != boot.locator(
        ".status-chip svg path"
    ).get_attribute("d")
    for chip in (job.locator(".status-chip"), boot.locator(".status-chip")):
        expect(chip).to_have_css("color", "rgb(0, 0, 0)")
        expect(chip).to_have_css("border-top-color", "rgb(0, 0, 0)")
    for row in (rest, job, boot):
        expect(row).to_have_class(re.compile(r"\bnested\b"))
        expect(row).to_contain_text("Child of orch")


def test_related_container_navigation_resets_eink_page_and_scroll(
    server: E2EServer, logged_in: Page
) -> None:
    page = logged_in
    page.set_viewport_size({"width": 800, "height": 600})
    open_settings(page)
    page.get_by_role("group", name="Display profile", exact=True).get_by_label(
        re.compile("E-ink")
    ).check()
    page.get_by_label("Page controls", exact=True).select_option("bar")
    page.get_by_role("link", name="JailBee", exact=False).last.click()
    parent = container("work-orch", repo="work")
    children = [
        container(
            f"work-task-{i:02}", repo="work", child=ChildView(parent.name, "working", (), False, ())
        )
        for i in range(40)
    ]
    selected = children[20]
    grandchildren = [
        container(
            f"work-sub-{i:02}",
            repo="work",
            child=ChildView(selected.name, "working", (), False, ()),
        )
        for i in range(40)
    ]
    server.push(snapshot_of(repo_group("work", parent, *children, *grandchildren)))
    page.reload()
    page.locator('[data-container="work-orch"] .name').click()
    expect(page.get_by_role("heading", name="Children (40)")).to_be_visible()
    page.get_by_role("navigation", name="Pages").get_by_role(
        "button", name=re.compile("Next")
    ).click()
    wait_for_scroll_settled(page)
    expect(page.locator(".page-indicator")).to_contain_text("2 /")
    page.get_by_role("region", name="Children").get_by_role(
        "link", name="task-20", exact=True
    ).click()
    expect(page.get_by_role("heading", name=selected.name, exact=True)).to_have_count(1)
    expect(page.locator(".page-indicator")).to_contain_text("1 /")
    page.wait_for_function("window.scrollY === 0")
    expect(page.get_by_role("heading", name="Children (40)")).to_be_visible()
    page.locator(".parent a").click()
    expect(page.get_by_role("heading", name=parent.name, exact=True)).to_be_visible()
    expect(page.locator(".page-indicator")).to_contain_text("1 /")
    page.wait_for_function("window.scrollY === 0")
