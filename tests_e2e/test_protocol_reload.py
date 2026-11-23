"""A stale cached client must not reload indefinitely on protocol mismatch."""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, WebSocketRoute, expect

from tests.web_fakes import container, repo_group, snapshot_of
from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer


@pytest.mark.parametrize("storage_available", [True, False], ids=["tab-storage", "blocked-storage"])
def test_a_client_that_stays_stale_after_reload_stops_and_offers_manual_reload(
    server: E2EServer, page: Page, storage_available: bool
) -> None:
    if not storage_available:
        page.add_init_script("""
            Object.defineProperty(window, 'sessionStorage', {get() {
                throw new DOMException('Storage blocked', 'SecurityError');
            }});
        """)
    closes: list[int | None] = []
    documents: list[str] = []
    stale = True

    def connect(route: WebSocketRoute) -> None:
        upstream = route.connect_to_server()

        def stale_auth(message: str | bytes) -> None:
            payload = json.loads(message)
            if stale and payload["type"] == "auth":
                payload["v"] = -1
            upstream.send(json.dumps(payload))

        def closed(code: int | None, reason: str | None) -> None:
            if code is not None:
                closes.append(code)
            route.close(code=code, reason=reason)

        route.on_message(stale_auth)
        upstream.on_close(closed)

    page.route_web_socket("**/api/ws", connect)
    page.on(
        "request",
        lambda request: (
            documents.append(request.url) if request.resource_type == "document" else None
        ),
    )
    # The state the client shows once current assets are served again.
    server.push(snapshot_of(repo_group("work", container("work-unseen", "work"))))
    log_in_with_pin(page, server.url, server.pin.current, "stale tablet")
    expect(page.get_by_role("alert")).to_have_text("This page is out of date; reload.")
    expect(page.get_by_role("button", name="Reload", exact=True)).to_be_visible()
    attempts = 2 if storage_available else 1
    assert closes == [4002] * attempts
    assert documents == [server.url + "/"] * attempts

    # A user-requested retry can recover once current assets become available.
    stale = False
    page.get_by_role("button", name="Reload", exact=True).click()
    expect(page.get_by_role("link", name="unseen", exact=True)).to_be_visible()
    expect(page.get_by_role("alert")).to_have_count(0)
    assert len(documents) == attempts + 1


def test_a_second_server_upgrade_after_a_good_reload_reloads_again(
    server: E2EServer, page: Page
) -> None:
    closes: list[int | None] = []
    documents: list[str] = []
    stale = True
    links: list[tuple[WebSocketRoute, WebSocketRoute]] = []

    def connect(route: WebSocketRoute) -> None:
        upstream = route.connect_to_server()
        links.append((route, upstream))

        def stale_auth(message: str | bytes) -> None:
            payload = json.loads(message)
            if stale and payload["type"] == "auth":
                payload["v"] = -1
            upstream.send(json.dumps(payload))

        def closed(code: int | None, reason: str | None) -> None:
            nonlocal stale
            if code is not None:
                closes.append(code)
            if code == 4002:
                # The reload this close triggers fetches current assets.
                stale = False
            route.close(code=code, reason=reason)

        route.on_message(stale_auth)
        upstream.on_close(closed)

    page.route_web_socket("**/api/ws", connect)
    page.on(
        "request",
        lambda request: (
            documents.append(request.url) if request.resource_type == "document" else None
        ),
    )
    server.push(snapshot_of(repo_group("work", container("work-first", "work"))))
    log_in_with_pin(page, server.url, server.pin.current, "upgraded tablet")
    expect(page.get_by_role("link", name="first", exact=True)).to_be_visible()
    assert closes == [4002]
    assert len(documents) == 2

    # The server is upgraded again: the live socket drops and the client,
    # still holding the previous assets, reconnects as a stale client.
    stale = True
    route, upstream = links[-1]
    upstream.close(code=1012, reason="restart")
    route.close(code=1012, reason="restart")
    # Playwright delivers route and request events only while it is called.
    for _ in range(100):
        if len(documents) == 3:
            break
        page.wait_for_timeout(100)
    server.push(snapshot_of(repo_group("work", container("work-second", "work"))))
    expect(page.get_by_role("link", name="second", exact=True)).to_be_visible()
    expect(page.get_by_role("alert")).to_have_count(0)
    assert closes == [4002, 4002]
    assert len(documents) == 3
