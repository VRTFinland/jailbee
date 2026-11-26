"""Front-page snapshots and reconnects through the real server and browser."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Locator, Page, WebSocketRoute, expect

from tests.follower_fixture import CLAUDE, FOLLOWER, WRAPPER, gather
from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.flows import DERIVED_DEVICE, log_in_with_pin, open_settings
from tests_e2e.server import E2EServer

if TYPE_CHECKING:
    from pathlib import Path


def expect_refused_reconnect(server: E2EServer, errors: BrowserErrors) -> re.Pattern[str]:
    refused = re.compile(
        rf"WebSocket connection to 'ws://127\.0\.0\.1:{server.port}/api/ws' failed: "
        r"Error in connection establishment: net::ERR_CONNECTION_REFUSED"
    )
    errors.expect("console", refused, at_least=1)
    return refused


@pytest.mark.parametrize("theme", ["light", "dark", "eink"])
def test_front_page_actions_and_rows_match_gallery_primitives(
    server: E2EServer, logged_in: Page, theme: str
) -> None:
    page = logged_in
    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))
    page.evaluate("t => localStorage.setItem('jailbee.theme', t)", theme)
    page.reload()
    row = page.get_by_role("listitem").filter(has=page.get_by_role("link", name="main", exact=True))
    expect(row).to_be_visible()
    action = page.get_by_role("button", name="Log out", exact=True)
    actual = action.evaluate("el => { const s = getComputedStyle(el); return [s.borderRadius, s.color, s.minHeight] }")
    row_look = row.evaluate("el => { const s = getComputedStyle(el); return [s.borderRadius, s.backgroundColor, s.borderColor] }")
    page.goto(f"{server.url}/#/ui")
    label = {"light": "Light", "dark": "Dark", "eink": "E-ink"}[theme]
    panel = page.get_by_role("region", name=f"{label} theme, normal profile", exact=True)
    reference = panel.get_by_role("button", name="text", exact=True)
    assert actual == reference.evaluate("el => { const s = getComputedStyle(el); return [s.borderRadius, s.color, s.minHeight] }")
    reference_row = panel.get_by_role("listitem").filter(has_text="fix-issue-165").first
    assert row_look == reference_row.evaluate("el => { const s = getComputedStyle(el); return [s.borderRadius, s.backgroundColor, s.borderColor] }")


def test_row_header_stays_above_cells_and_long_names_remain_readable(
    server: E2EServer, logged_in: Page
) -> None:
    name = "a-very-long-container-name-" * 6
    server.push(snapshot_of(repo_group("work", container(f"work-{name}", "work", state="Stopped"))))
    row = logged_in.get_by_role("listitem")
    for width in (1280, 390):
        logged_in.set_viewport_size({"width": width, "height": 844})
        link = row.get_by_role("link", name=name, exact=True)
        expect(link).to_be_visible()
        title = row.locator(".list-row-title")
        assert title.evaluate("el => el.scrollWidth <= el.clientWidth")
        assert title.evaluate("el => getComputedStyle(el).whiteSpace") != "nowrap"
        chip, cells = row.locator(".phase").bounding_box(), row.locator("dl").bounding_box()
        assert chip and cells
        assert chip["y"] + chip["height"] <= cells["y"]
        assert logged_in.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_container_opens_from_row_metadata_not_only_its_name(
    server: E2EServer, logged_in: Page
) -> None:
    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))
    row = logged_in.get_by_role("listitem").filter(
        has=logged_in.get_by_role("link", name="main", exact=True)
    )
    expect(row).to_be_visible()
    cells = row.locator("dl").bounding_box()
    box = row.bounding_box()
    assert cells and box
    row.click(position={"x": cells["x"] - box["x"] + 4, "y": cells["y"] - box["y"] + 4})
    expect(logged_in).to_have_url(re.compile(r"#/containers/work-main$"))


def test_snapshots_add_change_and_remove_containers_without_reloading(
    server: E2EServer, logged_in: Page
) -> None:
    navigations: list[str] = []
    logged_in.on("framenavigated", lambda frame: navigations.append(frame.url))
    expect(logged_in.get_by_text("No repositories.")).to_be_visible()

    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))
    main = logged_in.get_by_role("listitem").filter(
        has=logged_in.get_by_role("link", name="main", exact=True)
    )
    expect(main).to_be_visible()

    server.push(
        snapshot_of(
            repo_group(
                "work",
                container("work-main", "work", state="Stopped"),
                container("work-next", "work"),
            )
        )
    )
    expect(main).to_contain_text("Stopped")
    expect(logged_in.get_by_role("link", name="next", exact=True)).to_be_visible()

    server.push(snapshot_of(repo_group("work", container("work-next", "work"))))
    expect(main).to_have_count(0)
    expect(logged_in.get_by_role("listitem")).to_have_count(1)

    server.push(snapshot_of(repo_group("work")))
    expect(logged_in.get_by_text("No containers.")).to_be_visible()
    expect(logged_in.get_by_role("listitem")).to_have_count(0)
    assert navigations == [], "snapshot updates must not navigate or reload the page"


def test_row_phases_have_distinct_icons_and_labels_in_the_eink_theme(
    server: E2EServer, logged_in: Page
) -> None:
    server.push(
        snapshot_of(
            repo_group(
                "work",
                container("work-boot", "work", job_kind="boot", job_phase="starting", job_pid=1),
                container(
                    "work-auto", "work", job_kind="autostart", job_phase="starting", job_pid=1
                ),
                container("work-build", "work", job_kind="create", job_phase="creating", job_pid=1),
                container("work-broken", "work", job_kind="create", job_phase="failed"),
                container("work-off", "work", state="Stopped"),
                container("work-running", "work"),
            )
        )
    )
    open_settings(logged_in)
    logged_in.get_by_role("radio", name="E-ink", exact=True).check()
    logged_in.get_by_role("link", name="JailBee").click()

    running = logged_in.get_by_role("listitem").filter(
        has=logged_in.get_by_role("link", name="running", exact=True)
    )
    expect(running).to_be_visible()
    expect(running.locator(".phase")).to_have_count(0)
    icons: set[str] = set()
    # The shared chip replaces phase-specific row edges. Its visible words and
    # distinct painted vector keep every state legible without colour or fades.
    for name, label in (
        ("boot", "booting"), ("auto", "autostart"), ("build", "job"),
        ("broken", "failed"), ("off", "Stopped"),
    ):
        row = logged_in.get_by_role("listitem").filter(
            has=logged_in.get_by_role("link", name=name, exact=True)
        )
        expect(row).to_be_visible()
        chip = row.locator(".phase")
        expect(chip).to_have_text(label)
        expect(chip).to_be_visible()
        expect(chip).to_have_css("color", "rgb(0, 0, 0)")
        expect(chip).to_have_css("background-color", "rgb(255, 255, 255)")
        expect(chip).to_have_css("border-top-color", "rgb(0, 0, 0)")
        path = chip.locator("svg path")
        assert path.evaluate("el => el.getBBox().width > 0")
        icons.add(path.get_attribute("d") or "")
        expect(row).to_have_css("opacity", "1")
    assert len(icons) == 5


def test_connection_loss_shows_connecting_while_the_last_snapshot_stays_visible(
    server: E2EServer, logged_in: Page, browser_errors: BrowserErrors
) -> None:
    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))
    main = logged_in.get_by_role("link", name="main", exact=True)
    expect(main).to_be_visible()
    expect(logged_in.get_by_text("Connecting…", exact=True)).to_have_count(0)
    refused = expect_refused_reconnect(server, browser_errors)

    with logged_in.expect_console_message(
        predicate=lambda message: refused.fullmatch(message.text) is not None
    ):
        server.stop()
    expect(logged_in.get_by_text("Connecting…", exact=True)).to_be_visible()
    expect(main).to_be_visible()
    server.start()
    expect(logged_in.get_by_text("Connecting…", exact=True)).to_have_count(0)


def test_server_restart_reconnects_to_current_state_without_reloading(
    server: E2EServer, logged_in: Page, browser_errors: BrowserErrors
) -> None:
    server.push(snapshot_of(repo_group("work", container("work-before", "work"))))
    expect(logged_in.get_by_role("link", name="before", exact=True)).to_be_visible()
    navigations: list[str] = []
    logged_in.on("framenavigated", lambda frame: navigations.append(frame.url))
    refused = expect_refused_reconnect(server, browser_errors)

    with logged_in.expect_console_message(
        predicate=lambda message: refused.fullmatch(message.text) is not None
    ):
        server.stop()
    # Change state while no browser is connected: the reconnect must fetch
    # the current snapshot, not just resume updates from the old socket.
    server.push(snapshot_of(repo_group("work", container("work-after", "work"))))
    server.start()

    expect(logged_in.get_by_role("link", name="after", exact=True)).to_be_visible()
    expect(logged_in.get_by_role("link", name="before", exact=True)).to_have_count(0)
    expect(logged_in.get_by_text("Connecting…", exact=True)).to_have_count(0)
    server.push(snapshot_of(repo_group("work", container("work-live-again", "work"))))
    expect(logged_in.get_by_role("link", name="live-again", exact=True)).to_be_visible()
    expect(logged_in.get_by_role("link", name="after", exact=True)).to_have_count(0)
    assert navigations == [], "an ordinary reconnect must not reload the page"


def test_protocol_mismatch_on_reconnect_reloads_the_page(server: E2EServer, page: Page) -> None:
    """Simulate a stale app without faking the server's upgrade handling.

    Forward every connection to the real server; only the reconnect's auth
    version is rewritten to -1. The actual 4002 close and browser reload stay
    real, and the reloaded document sends its normal current version again.
    """
    sockets: list[WebSocketRoute] = []
    closes: list[int | None] = []
    navigations: list[str] = []

    def connect(route: WebSocketRoute) -> None:
        upstream = route.connect_to_server()
        sockets.append(route)
        if len(sockets) != 2:
            return

        # Only the reconnect speaks an obsolete version. The real server
        # rejects it with 4002; after a document reload the client is current.
        def stale_auth(message: str | bytes) -> None:
            payload = json.loads(message)
            if payload["type"] == "auth":
                payload["v"] = -1
            upstream.send(json.dumps(payload))

        def closed(code: int | None, reason: str | None) -> None:
            closes.append(code)
            route.close(code=code, reason=reason)

        route.on_message(stale_auth)
        upstream.on_close(closed)

    page.route_web_socket("**/api/ws", connect)
    server.push(snapshot_of(repo_group("work", container("work-before", "work"))))
    log_in_with_pin(page, server.url, server.pin.current)
    expect(page.get_by_role("link", name="before", exact=True)).to_be_visible()
    page.on(
        "request",
        lambda request: (
            navigations.append(request.url) if request.resource_type == "document" else None
        ),
    )

    with page.expect_navigation(wait_until="load", timeout=10_000):
        sockets[0].close(code=1012, reason="Service restart")

    expect(page.get_by_role("link", name="before", exact=True)).to_be_visible()
    expect(page.get_by_text("Connecting…", exact=True)).to_have_count(0)
    expect(page.locator(".bar")).to_contain_text(DERIVED_DEVICE)
    server.push(snapshot_of(repo_group("work", container("work-live-after-reload", "work"))))
    expect(page.get_by_role("link", name="live-after-reload", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="before", exact=True)).to_have_count(0)
    assert closes[0] == 4002, "the real server must reject the stale protocol"
    assert len(sockets) == 3, "the reloaded document must establish a fresh connection"
    assert navigations == [f"{server.url}/"], "exactly one automatic document reload is needed"


def test_an_idle_agent_with_only_the_inbox_follower_reads_idle(
    server: E2EServer, logged_in: Page, tmp_path: Path
) -> None:
    """#146 ticket 38: the follower is not a background job, so not `shell`."""
    idle_root, busy_root = tmp_path / "idle", tmp_path / "busy"
    follower_only = [(1011, CLAUDE, "bash", WRAPPER), (1012, 1011, "python", FOLLOWER)]
    _, idle = gather(idle_root, "shell", follower_only, name="work-quiet", prefix="work")
    _, shell = gather(
        busy_root,
        "shell",
        [
            *follower_only,
            (1013, CLAUDE, "bash", ("/bin/bash", "-c", "npm run dev")),
            (1014, 1013, "npm", ("npm", "run", "dev")),
        ],
        name="work-dev",
        prefix="work",
    )
    server.push(snapshot_of(repo_group("work", *idle.containers, *shell.containers)))

    def row(name: str) -> Locator:
        return logged_in.get_by_role("listitem").filter(
            has=logged_in.get_by_role("link", name=name, exact=True)
        )

    expect(row("quiet").locator(".agent")).to_have_text("idle")
    expect(row("quiet")).not_to_contain_text("shell")
    expect(row("quiet")).not_to_contain_text("working")
    expect(row("dev").locator(".agent")).to_have_text("shell")
