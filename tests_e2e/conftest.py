"""Fixtures for the web dashboard's end-to-end browser suite (#152).

Run it with `make web-e2e`, which builds the browser app first. The browser is
the system Chrome (Playwright's `chrome` channel); nothing is downloaded.

- `server`: the session's dashboard server (`tests_e2e.server.E2EServer`),
  running, with an empty snapshot pushed. Tests push their own state.
- `make_server`: a fresh server with its own database, for tests that need
  clean auth state or different `WebDeps` overrides.
- `browser_errors`: every test fails on a console error, page error or CSP
  violation it did not declare with `browser_errors.expect(...)`.
- `logged_in`: a page on the front page, its token from one real PIN login
  made once per session.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect
from sqlalchemy.engine import Engine

from jailbee.db import get_engine
from jailbee.web import assets
from tests.web_fakes import snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer

SESSION_DEVICE = "e2e browser"

expect.set_options(timeout=10_000)


def _static_dir() -> Path:
    static = assets.default_static_dir()
    if not assets.frontend_built(static):
        pytest.exit(
            f"the browser app is not built ({static}); run `make web-e2e`, "
            "which builds it, or `make web` first",
            returncode=2,
        )
    return static


def _engine_in(state_home: Path) -> Engine:
    """A real state database under ``state_home``, never the user's own."""
    previous = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(state_home)
    try:
        return get_engine()
    finally:
        if previous is None:
            del os.environ["XDG_STATE_HOME"]
        else:
            os.environ["XDG_STATE_HOME"] = previous


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args: dict[str, Any]) -> dict[str, Any]:
    return {"channel": "chrome", **browser_type_launch_args}


@pytest.fixture(scope="session")
def e2e_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[E2EServer]:
    server = E2EServer(_engine_in(tmp_path_factory.mktemp("state")), _static_dir())
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def base_url(e2e_server: E2EServer) -> str:
    return e2e_server.url


@pytest.fixture
def server(e2e_server: E2EServer) -> E2EServer:
    if not e2e_server.running:
        e2e_server.start()
    e2e_server.push(snapshot_of())
    return e2e_server


@pytest.fixture
def make_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Callable[..., E2EServer]]:
    made: list[E2EServer] = []

    def make(**overrides: Any) -> E2EServer:
        fresh = E2EServer(
            _engine_in(tmp_path_factory.mktemp("state")), _static_dir(), overrides=overrides
        )
        fresh.start()
        made.append(fresh)
        return fresh

    yield make
    for fresh in made:
        fresh.stop()


@pytest.fixture
def browser_errors() -> BrowserErrors:
    return BrowserErrors()


@pytest.fixture(scope="session")
def auth_state(
    browser: Browser,
    browser_context_args: dict[str, Any],
    e2e_server: E2EServer,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    """The browser storage after one real PIN login on the session server."""
    context = browser.new_context(**browser_context_args)
    errors = BrowserErrors()
    errors.attach(context)
    try:
        log_in_with_pin(context.new_page(), e2e_server.url, e2e_server.pin.current, SESSION_DEVICE)
        path = tmp_path_factory.mktemp("auth") / "storage.json"
        context.storage_state(path=path)
    finally:
        context.close()
    errors.assert_clean()
    return path


@pytest.fixture
def context(
    new_context: Callable[..., BrowserContext],
    browser_errors: BrowserErrors,
    request: pytest.FixtureRequest,
) -> BrowserContext:
    kwargs: dict[str, Any] = {}
    if "logged_in" in request.fixturenames:
        kwargs["storage_state"] = request.getfixturevalue("auth_state")
    made = new_context(**kwargs)
    browser_errors.attach(made)
    return made


@pytest.fixture
def logged_in(server: E2EServer, page: Page) -> Page:
    page.goto(server.url)
    expect(page.get_by_role("button", name="Log out")).to_be_visible()
    return page


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    """Fail a passing test whose browser reported an unexpected error."""
    result = yield
    errors = getattr(item, "funcargs", {}).get("browser_errors")
    if isinstance(errors, BrowserErrors):
        errors.assert_clean()
    return result
