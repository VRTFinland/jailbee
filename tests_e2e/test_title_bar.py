"""Browser acceptance tests for the shared signed-in title bar."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer
from tests_e2e.sessions import SESSION_ID, SESSION_TITLE, SUBAGENT_ID

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "transcripts" / "claude"


@pytest.fixture
def chrome_server(make_server: Callable[..., E2EServer], tmp_path: Path) -> E2EServer:
    home = tmp_path / "agent"
    shutil.copytree(FIXTURES, home)
    server = make_server(agent_homes={"work": [("claude", home)]}.get)
    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))
    return server


@pytest.mark.parametrize("width", [390, 1280], ids=["phone", "desktop"])
@pytest.mark.parametrize("profile", ["normal", "eink"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_title_bar_on_every_signed_in_view(
    page: Page, chrome_server: E2EServer, width: int, profile: str, theme: str
) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    log_in_with_pin(page, chrome_server.url, chrome_server.pin.current)
    page.evaluate(
        "([p, t]) => { localStorage.setItem('jailbee.displayProfile', p); "
        "localStorage.setItem('jailbee.theme', t) }",
        [profile, theme],
    )
    paths = [
        ("/", "JailBee"),
        ("/repos/work", "work"),
        ("/containers/work-main", "work-main"),
        (f"/repos/work/sessions/claude/{SESSION_ID}", SESSION_TITLE),
        (f"/repos/work/sessions/claude/{SESSION_ID}/subagents/{SUBAGENT_ID}", "Review auth"),
        ("/settings", "Settings"),
    ]
    for path, title in paths:
        page.goto(f"{chrome_server.url}/#{path}")
        page.reload()
        expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
        bar = page.locator(".title-bar")
        trigger = bar.get_by_role("button", name="Quick settings", exact=True)
        expect(trigger).to_be_visible()
        bar_box = bar.bounding_box()
        trigger_box = trigger.bounding_box()
        assert bar_box is not None and trigger_box is not None
        assert bar_box["height"] <= 44
        assert trigger_box["x"] + trigger_box["width"] >= bar_box["x"] + bar_box["width"] - 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_hidden_title_bar_menu_and_parent_navigation(page: Page, chrome_server: E2EServer) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    log_in_with_pin(page, chrome_server.url, chrome_server.pin.current)
    page.evaluate("localStorage.setItem('jailbee.showTitleBar', '0')")
    base = f"/repos/work/sessions/claude/{SESSION_ID}"
    for path in ["/", "/repos/work", "/containers/work-main", base, f"{base}/subagents/{SUBAGENT_ID}", "/settings"]:
        page.goto(f"{chrome_server.url}/#{path}")
        page.reload()
        expect(page.locator(".title-bar")).to_have_count(0)
        trigger = page.get_by_role("button", name="Quick settings")
        expect(trigger).to_be_visible()
        box = trigger.bounding_box()
        assert box is not None and box["x"] + box["width"] > 350
    for path, label, target in [
        (f"{base}/subagents/{SUBAGENT_ID}", SESSION_TITLE, base),
        (base, "work", "/repos/work"),
        ("/repos/work", "JailBee", "/"),
    ]:
        page.goto(f"{chrome_server.url}/#{path}")
        page.get_by_role("button", name="Quick settings").click()
        page.get_by_role("link", name=f"Back to {label}").click()
        expect(page).to_have_url(f"{chrome_server.url}/#{target}")


def test_breadcrumbs_navigate_to_each_parent(page: Page, chrome_server: E2EServer) -> None:
    page.set_viewport_size({"width": 1280, "height": 844})
    log_in_with_pin(page, chrome_server.url, chrome_server.pin.current)
    base = f"{chrome_server.url}/#/repos/work/sessions/claude/{SESSION_ID}"
    page.goto(f"{base}/subagents/{SUBAGENT_ID}")
    crumbs = page.get_by_role("navigation", name="Breadcrumb")
    expect(crumbs.get_by_role("link", name=SESSION_TITLE, exact=True)).to_be_visible()
    crumbs.get_by_role("link", name=SESSION_TITLE, exact=True).click()
    expect(page.get_by_role("heading", name=SESSION_TITLE, exact=True)).to_be_visible()
    crumbs.get_by_role("link", name="work", exact=True).click()
    expect(page.get_by_role("heading", name="work", exact=True)).to_be_visible()
    crumbs.get_by_role("link", name="JailBee", exact=True).click()
    expect(page.get_by_role("heading", name="JailBee", exact=True)).to_be_visible()
