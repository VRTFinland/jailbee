"""Device preferences, e-ink reading, phone layout and installable app assets."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urljoin

import pytest
from playwright.sync_api import Page, expect

from jailbee.agent_status import AgentSummary
from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.flows import open_settings
from tests_e2e.server import E2EServer
from tests_e2e.sessions import SESSION_ID, append_entry, open_session, session_path


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    home = tmp_path / "claude"
    fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "transcripts" / "claude"
    shutil.copytree(fixtures, home)
    return home


@pytest.fixture
def server(make_server: Callable[..., E2EServer], agent_home: Path) -> E2EServer:
    server = make_server(agent_homes={"work": [("claude", agent_home)]}.get)
    live = AgentSummary("claude", "busy", None, None, 1, session_ids=(SESSION_ID,))
    server.push(
        snapshot_of(repo_group("work", container("work-main", "work", agent_status=(live,))))
    )
    return server


def test_themes_and_font_sizes_apply_and_survive_reload(logged_in: Page) -> None:
    page = logged_in
    open_settings(page)
    theme = page.get_by_role("group", name="Theme", exact=True)
    sample = page.get_by_text("The quick brown fox", exact=False)
    code = sample.locator("code")
    body_size = page.get_by_role("slider", name="Body:")
    code_size = page.get_by_role("slider", name="Code:")
    body_size.focus()
    body_size.press("Home")
    body_size.press("ArrowRight")
    code_size.focus()
    code_size.press("End")
    code_size.press("ArrowLeft")
    expect(sample).to_have_css("font-size", "13px")
    expect(code).to_have_css("font-size", "27px")

    for label, background, text in [
        ("Light", "rgb(249, 249, 255)", "rgb(25, 28, 32)"),
        ("Dark", "rgb(17, 19, 24)", "rgb(226, 226, 233)"),
        ("E-ink", "rgb(255, 255, 255)", "rgb(0, 0, 0)"),
    ]:
        theme.get_by_role("radio", name=label, exact=True).check()
        expect(page.locator("body")).to_have_css("background-color", background)
        expect(sample).to_have_css("color", text)
        page.reload()
        expect(theme.get_by_role("radio", name=label, exact=True)).to_be_checked()
        expect(page.locator("body")).to_have_css("background-color", background)
        expect(sample).to_have_css("color", text)
        expect(sample).to_have_css("font-size", "13px")
        expect(code).to_have_css("font-size", "27px")
        expect(body_size).to_have_value("13")
        expect(code_size).to_have_value("27")


def test_eink_page_turns_hold_new_messages_and_refresh_snapshot(
    logged_in: Page, server: E2EServer, agent_home: Path
) -> None:
    page = logged_in
    page.set_viewport_size({"width": 800, "height": 600})
    open_settings(page)
    page.get_by_role("group", name="Display profile", exact=True).get_by_role(
        "radio", name=re.compile("E-ink")
    ).check()
    page.get_by_label("Page controls", exact=True).select_option("bar")
    page.get_by_role("link", name="JailBee", exact=False).last.click()

    # A real websocket frame is the delivery barrier; no timer or store access.
    with page.expect_websocket() as connection:
        page.reload()
    socket = connection.value
    expect(page.get_by_role("link", name="main", exact=True)).to_be_visible()
    with socket.expect_event(
        "framereceived", predicate=lambda frame: json.loads(frame).get("type") == "front_page"
    ):
        server.push(snapshot_of(repo_group("work", container("work-updated", "work"))))
    expect(page.get_by_role("link", name="updated", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_role("link", name="updated", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="main", exact=True)).to_have_count(0)

    path = session_path(agent_home)
    append_entry(server, path, "\n\n".join(f"Reading paragraph {n}." for n in range(40)))
    page.get_by_role("link", name="work", exact=True).click()
    open_session(page)
    pages = page.get_by_role("navigation", name="Pages", exact=True)
    indicator = pages.locator(".page-indicator")
    expect(indicator).to_have_text(re.compile(r"1 / [2-9]\d*"))
    expect(pages.get_by_role("button", name=re.compile("Prev"))).to_be_disabled()
    start = page.evaluate("window.scrollY")
    pages.get_by_role("button", name=re.compile("Next")).click()
    expect(indicator).to_have_text(re.compile(r"2 / \d+"))
    assert page.evaluate("window.scrollY") > start
    pages.get_by_role("button", name=re.compile("Prev")).click()
    expect(indicator).to_have_text(re.compile(r"1 / \d+"))
    assert page.evaluate("window.scrollY") == start

    total = int(indicator.inner_text().split("/")[1])
    next_page = pages.get_by_role("button", name=re.compile("Next"))
    for number in range(2, total + 1):
        next_page.click()
        # Let the browser deliver scroll/layout events before checking the counter.
        page.evaluate(
            "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))"
        )
        expect(indicator).to_have_text(f"{number} / {total}")
    expect(next_page).to_be_disabled()
    expect(page.get_by_text("Reading paragraph 39.", exact=True)).to_be_in_viewport()
    for number in range(total - 1, 0, -1):
        pages.get_by_role("button", name=re.compile("Prev")).click()
        expect(indicator).to_have_text(f"{number} / {total}")
    assert page.evaluate("window.scrollY") == start

    append_entry(server, path, "Held reply one.")
    expect(page.get_by_role("button", name="1 new message ↓", exact=True)).to_be_visible()
    expect(page.get_by_text("Held reply one.", exact=True)).to_have_count(0)
    append_entry(server, path, "Held reply two.")
    held = page.get_by_role("button", name="2 new messages ↓", exact=True)
    expect(held).to_be_visible()
    expect(page.get_by_text("Held reply two.", exact=True)).to_have_count(0)
    assert page.evaluate("window.scrollY") == start
    held.click()
    expect(held).to_have_count(0)
    expect(page.get_by_text("Held reply one.", exact=True)).to_have_count(1)
    expect(page.get_by_text("Held reply two.", exact=True)).to_have_count(1)
    first = page.get_by_text("Held reply one.", exact=True).locator("xpath=ancestor::section")
    header_bottom = page.locator(".title-bar").evaluate("el => el.getBoundingClientRect().bottom")
    assert abs(first.evaluate("el => el.getBoundingClientRect().top") - header_bottom) <= 1


@pytest.mark.parametrize("profile", ["Normal", "E-ink"], ids=["normal", "eink"])
def test_phone_pages_have_no_horizontal_scroll(
    logged_in: Page, server: E2EServer, agent_home: Path, profile: str
) -> None:
    page = logged_in
    page.set_viewport_size({"width": 390, "height": 844})
    open_settings(page)
    page.get_by_role("group", name="Display profile", exact=True).get_by_role(
        "radio", name=re.compile(profile)
    ).check()
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("link", name="JailBee", exact=False).last.click()
    expect(page.get_by_role("heading", name="JailBee", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("link", name="work", exact=True).click()
    expect(page.get_by_role("heading", name="work", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("link", name="main", exact=True).click()
    expect(page.get_by_role("heading", name="work-main", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    append_entry(server, session_path(agent_home), "```text\n" + "wide_code_" * 30 + "\n```")
    open_session(page)
    expect(page.locator("pre code")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_pwa_manifest_and_linked_icons_are_served(logged_in: Page) -> None:
    page = logged_in
    manifest_link = page.locator('link[rel="manifest"]')
    expect(manifest_link).to_have_count(1)
    manifest_url = manifest_link.evaluate("el => el.href")
    response = page.request.get(manifest_url)
    assert response.status == 200
    assert response.headers["content-type"].split(";")[0] == "application/manifest+json"
    manifest = response.json()
    assert manifest["name"] == "JailBee"
    assert manifest["display"] == "standalone"
    assert urljoin(manifest_url, manifest["start_url"]) == page.url.split("#")[0]
    assert urljoin(manifest_url, manifest["scope"]) == page.url.split("#")[0]
    assert {icon["sizes"] for icon in manifest["icons"]} == {"192x192", "512x512"}
    for icon in manifest["icons"]:
        icon_url = urljoin(manifest_url, icon["src"])
        image_response = page.request.get(icon_url)
        assert image_response.status == 200
        assert image_response.headers["content-type"] == "image/png"
        # Decode the image in Chrome: a 200 SPA fallback is not a valid icon.
        dimensions = page.evaluate(
            """async url => {
                const image = new Image();
                image.src = url;
                await image.decode();
                return `${image.naturalWidth}x${image.naturalHeight}`;
            }""",
            icon_url,
        )
        assert dimensions == icon["sizes"]
    for relation in ("icon", "apple-touch-icon"):
        link = page.locator(f'link[rel="{relation}"]')
        expect(link).to_have_count(1)
        assert link.evaluate("el => el.href") in {
            urljoin(manifest_url, icon["src"]) for icon in manifest["icons"]
        }
