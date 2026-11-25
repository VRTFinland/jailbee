"""Measured title-bar breadcrumbs through real Chrome layout and navigation."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer
from tests_e2e.sessions import SESSION_ID, SESSION_TITLE, SUBAGENT_ID, session_path

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "transcripts" / "claude"
SUBAGENT_TITLE = "Authentication review"


@pytest.fixture
def breadcrumb_page(
    make_server: Callable[..., E2EServer], page: Page, tmp_path: Path
) -> Page:
    home = tmp_path / "claude"
    shutil.copytree(FIXTURES, home)
    parent_path = session_path(home)
    records = [json.loads(line) for line in parent_path.read_text().splitlines()]
    for record in records:
        for block in record.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("name") == "Agent":
                block["input"]["description"] = SUBAGENT_TITLE
    parent_path.write_text("".join(json.dumps(record) + "\n" for record in records))
    server = make_server(agent_homes={"work": [("claude", home)]}.get)
    log_in_with_pin(page, server.url, server.pin.current)
    page.goto(f"{server.url}/#/repos/work/sessions/claude/{SESSION_ID}/subagents/{SUBAGENT_ID}")
    expect(page.get_by_role("heading", name=SUBAGENT_TITLE, exact=True)).to_be_visible()
    return page


def test_narrow_parent_collapses_and_navigates_without_a_viewport_breakpoint(
    breadcrumb_page: Page,
) -> None:
    page = breadcrumb_page
    page.set_viewport_size({"width": 1280, "height": 800})
    # A narrow containing block on a desktop catches viewport/media-query shortcuts.
    page.locator(".title-bar").evaluate("bar => bar.style.width = '280px'")
    breadcrumb = page.get_by_role("navigation", name="Breadcrumb")
    parent = breadcrumb.get_by_role("link", name=SESSION_TITLE, exact=True)
    expect(parent).to_have_text("‹")
    expect(breadcrumb.get_by_role("link")).to_have_count(1)
    expect(parent).to_have_attribute("aria-label", SESSION_TITLE)
    box = parent.bounding_box()
    assert box is not None and box["height"] == 44
    assert page.get_by_role("heading", name=SUBAGENT_TITLE).evaluate(
        "title => title.clientWidth >= 24"
    )
    parent.click()
    expect(page.get_by_role("heading", name=SESSION_TITLE, exact=True)).to_be_visible()
    expect(page.get_by_text("Done.", exact=True)).to_be_visible()


def test_short_parent_keeps_its_name_when_the_current_title_is_long(breadcrumb_page: Page) -> None:
    page = breadcrumb_page
    breadcrumb = page.get_by_role("navigation", name="Breadcrumb")
    breadcrumb.get_by_role("link", name=SESSION_TITLE, exact=True).click()
    expect(page.get_by_role("heading", name=SESSION_TITLE, exact=True)).to_be_visible()
    page.set_viewport_size({"width": 280, "height": 844})
    expect(breadcrumb.get_by_role("link")).to_have_count(1)
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    expect(breadcrumb.get_by_role("link", name="work", exact=True)).to_have_text("work")
    assert page.get_by_role("heading", name=SESSION_TITLE, exact=True).evaluate(
        "title => title.clientWidth >= 24"
    )


def test_wide_phone_and_resize_restore_the_subagent_path(breadcrumb_page: Page) -> None:
    page = breadcrumb_page
    breadcrumb = page.get_by_role("navigation", name="Breadcrumb")
    page.set_viewport_size({"width": 1280, "height": 800})
    expect(breadcrumb.get_by_role("link")).to_have_text(["JailBee", "work", SESSION_TITLE])
    page.set_viewport_size({"width": 560, "height": 800})
    expect(breadcrumb.get_by_role("link")).to_have_text(["work", SESSION_TITLE])
    page.set_viewport_size({"width": 390, "height": 844})
    expect(breadcrumb.get_by_role("link")).to_have_count(1)
    parent = breadcrumb.get_by_role("link", name=SESSION_TITLE, exact=True)
    expect(parent).to_have_text(SESSION_TITLE)
    assert parent.evaluate("link => link.firstElementChild.scrollWidth > link.clientWidth")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.get_by_role("heading", name=SUBAGENT_TITLE).evaluate(
        "title => title.scrollWidth === title.clientWidth"
    )
    page.set_viewport_size({"width": 280, "height": 844})
    expect(parent).to_have_text("‹")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.set_viewport_size({"width": 1280, "height": 800})
    expect(breadcrumb.get_by_role("link")).to_have_text(["JailBee", "work", SESSION_TITLE])
    for link in breadcrumb.get_by_role("link").all():
        assert link.evaluate("link => link.scrollWidth === link.clientWidth")
    breadcrumb.get_by_role("link", name="work", exact=True).click()
    expect(page.get_by_role("heading", name="work", exact=True)).to_be_visible()
    breadcrumb.get_by_role("link", name="JailBee", exact=True).click()
    expect(page.get_by_role("heading", name="JailBee", exact=True)).to_be_visible()
