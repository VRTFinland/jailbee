"""Front-page snapshots and reconnects through the real server and browser."""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, WebSocketRoute, expect

from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer


def expect_refused_reconnect(server: E2EServer, errors: BrowserErrors) -> re.Pattern[str]:
    refused = re.compile(
        rf"WebSocket connection to 'ws://127\.0\.0\.1:{server.port}/api/ws' failed: "
        r"Error in connection establishment: net::ERR_CONNECTION_REFUSED"
    )
    errors.expect("console", refused, at_least=1)
    return refused


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


def test_row_phases_have_distinct_edges_and_labels_in_the_eink_theme(
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
            )
        )
    )
    logged_in.get_by_role("link", name="Settings", exact=True).click()
    logged_in.get_by_role("radio", name="E-ink", exact=True).check()
    logged_in.get_by_role("link", name="JailBee").click()

    # Check rendered edges, not CSS source or phase attributes: this catches
    # broken scoped selectors and distinguishes states on a monochrome screen.
    for name, label, style, width, outline in (
        ("boot", "booting", "dashed", "4px", "solid"),
        ("auto", "autostart", "dotted", "4px", "solid"),
        ("build", "job", "double", "4px", "solid"),
        ("broken", "failed", "solid", "8px", "solid"),
        ("off", "Stopped", "solid", "4px", "none"),
    ):
        row = logged_in.get_by_role("listitem").filter(
            has=logged_in.get_by_role("link", name=name, exact=True)
        )
        expect(row).to_be_visible()
        expect(row.locator(".phase")).to_have_text(label)
        expect(row.locator(".phase")).to_be_visible()
        expect(row).to_have_css("border-left-style", style)
        expect(row).to_have_css("border-left-width", width)
        expect(row).to_have_css("border-left-color", "rgb(0, 0, 0)")
        expect(row).to_have_css("opacity", "1")
        for side in ("top", "right", "bottom"):
            expect(row).to_have_css(f"border-{side}-style", outline)


def test_connection_loss_shows_connecting_while_the_last_snapshot_stays_visible(
    server: E2EServer, logged_in: Page, browser_errors: BrowserErrors
) -> None:
    server.push(snapshot_of(repo_group("work", container("work-main", "work"))))
    main = logged_in.get_by_role("link", name="main", exact=True)
    expect(main).to_be_visible()
    expect(logged_in.get_by_text("Connecting…", exact=True)).to_have_count(0)
    refused = expect_refused_reconnect(server, browser_errors)

    try:
        with logged_in.expect_console_message(
            predicate=lambda message: refused.fullmatch(message.text) is not None
        ):
            server.stop()
        expect(logged_in.get_by_text("Connecting…", exact=True)).to_be_visible()
        expect(main).to_be_visible()
    finally:
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

    try:
        with logged_in.expect_console_message(
            predicate=lambda message: refused.fullmatch(message.text) is not None
        ):
            server.stop()
        # Change state while no browser is connected: the reconnect must fetch
        # the current snapshot, not just resume updates from the old socket.
        server.push(snapshot_of(repo_group("work", container("work-after", "work"))))
    finally:
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
    log_in_with_pin(page, server.url, server.pin.current, "upgrade tablet")
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
    expect(page.get_by_role("banner")).to_contain_text("upgrade tablet")
    assert closes[0] == 4002, "the real server must reject the stale protocol"
    assert len(sockets) == 3, "the reloaded document must establish a fresh connection"
    assert navigations == [f"{server.url}/"], "exactly one automatic document reload is needed"
