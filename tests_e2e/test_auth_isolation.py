"""Auth-mutating tests must not poison the next logged-in test."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.server import E2EServer

pytest_plugins = ["pytester"]
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("mutation", ["logout", "revoke", "rate_limit"])
def test_logout_and_login_work_in_either_order(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, reverse: bool, mutation: str
) -> None:
    pytester.makeconftest('pytest_plugins = ["tests_e2e.conftest"]')
    actions = {
        "logout": """
    logged_in.get_by_role("button", name="Log out").click()
    expect(logged_in.get_by_role("button", name="Log in", exact=True)).to_be_visible()
""",
        "revoke": """
    from jailbee.web.auth_admin import list_sessions, revoke_session
    session, = list_sessions(server.engine)
    revoke_session(server.engine, session.id)
    logged_in.reload()
    expect(logged_in.get_by_role("button", name="Log in", exact=True)).to_be_visible()
""",
        "rate_limit": """
    import httpx
    from jailbee.web.auth import BACKOFF_THRESHOLD
    logged_in.get_by_role("button", name="Log out").click()
    for _ in range(BACKOFF_THRESHOLD):
        response = httpx.post(server.url + "/api/auth", json={
            "method": "pin", "pin": "invalid", "device_name": "limited"
        })
        if response.status_code == 429:
            break
        assert response.status_code == 401
    assert response.status_code == 429
    response = httpx.post(server.url + "/api/auth", json={
        "method": "pin", "pin": server.pin.current, "device_name": "limited"
    })
    assert response.status_code == 429
""",
    }
    pytester.makepyfile(
        "from playwright.sync_api import expect\n"
        "def test_mutate(server, logged_in):\n"
        + actions[mutation]
        + "\ndef test_login(logged_in):\n"
        '    expect(logged_in.get_by_role("button", name="Log out")).to_be_visible()\n'
    )
    names = ["test_mutate", "test_login"]
    if reverse:
        names.reverse()
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess(
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
        "--output",
        "results",
        *(f"test_logout_and_login_work_in_either_order.py::{name}" for name in names),
    )

    result.assert_outcomes(passed=2)


@pytest.fixture
def server(make_server: Callable[..., E2EServer]) -> E2EServer:
    """A test-local server override must also be the logged_in fixture's target."""
    return make_server()


def test_logged_in_uses_an_overridden_server(server: E2EServer, logged_in: Page) -> None:
    assert logged_in.url.startswith(server.url)
    expect(logged_in.get_by_role("button", name="Log out")).to_be_visible()
