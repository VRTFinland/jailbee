"""Repository sessions use the gallery rows without changing navigation."""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.server import E2EServer
from tests_e2e.sessions import SESSION_TITLE

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "transcripts" / "claude"


@pytest.fixture
def server(make_server: Callable[..., E2EServer], tmp_path: Path) -> E2EServer:
    home = tmp_path / "claude"
    shutil.copytree(FIXTURES, home)
    server = make_server(agent_homes={"work": [("claude", home)]}.get)
    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))
    return server


@pytest.mark.parametrize("theme", ["light", "dark", "eink"])
@pytest.mark.parametrize("width", [390, 1280])
def test_repo_sessions_match_gallery_rows_and_metadata_opens_the_transcript(
    server: E2EServer, logged_in: Page, tmp_path: Path, theme: str, width: int
) -> None:
    page = logged_in
    page.set_viewport_size({"width": width, "height": 844})
    page.evaluate("t => localStorage.setItem('jailbee.theme', t)", theme)
    page.reload()
    page.get_by_role("link", name="work", exact=True).click()
    link = page.get_by_role(
        "link", name=re.compile(rf"^{re.escape(SESSION_TITLE)} claude unattributed ")
    )
    row = page.get_by_role("listitem").filter(has=link)
    expect(row).to_be_visible()
    expect(row).to_contain_text("claude")
    expect(row).to_contain_text("unattributed")
    expect(row.locator("time")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    look = row.evaluate(
        "el => { const s = getComputedStyle(el); return [s.borderRadius, "
        "s.backgroundColor, s.color, s.borderColor, s.minHeight] }"
    )
    page.screenshot(path=tmp_path / f"repo-{theme}-{width}.png", full_page=True)

    page.goto(f"{server.url}/#/ui")
    label = {"light": "Light", "dark": "Dark", "eink": "E-ink"}[theme]
    panel = page.get_by_role("region", name=f"{label} theme, normal profile", exact=True)
    reference = panel.get_by_role("listitem").filter(has_text="fix-issue-165").first
    assert look == reference.evaluate(
        "el => { const s = getComputedStyle(el); return [s.borderRadius, "
        "s.backgroundColor, s.color, s.borderColor, s.minHeight] }"
    )
    reference.screenshot(path=tmp_path / f"gallery-row-{theme}-{width}.png")

    page.goto(f"{server.url}/#/repos/work")
    expect(row).to_be_visible()
    metadata = row.locator("time").bounding_box()
    box = row.bounding_box()
    assert metadata and box
    row.click(position={"x": metadata["x"] - box["x"] + 4, "y": metadata["y"] - box["y"] + 4})
    expect(page).to_have_url(re.compile(r"#/repos/work/sessions/claude/"))
    expect(page.get_by_text("Done.", exact=True)).to_be_visible()
