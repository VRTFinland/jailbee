"""Fixtures for the web dashboard's end-to-end browser suite (#152).

Run it with `make web-e2e`, which builds the browser app first. The browser is
the system Chrome (Playwright's `chrome` channel); nothing is downloaded.

- `server`: a fresh dashboard server (`tests_e2e.server.E2EServer`) per test,
  with isolated sessions/rate limits and an empty snapshot. Tests push state.
- `make_server`: additional fresh servers with their own databases and optional
  `WebDeps` overrides. Override `server` to point `logged_in` at one of these.
- `browser_errors`: automatically watches every context made by `browser` and
  checks errors after the test body and again during teardown.
- `logged_in`: a fresh real PIN login against the test's `server`; no token is
  shared between tests.
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


@pytest.fixture
def server(make_server: Callable[..., E2EServer]) -> E2EServer:
    """Fresh auth database and in-memory rate limits for every test."""
    fresh = make_server()
    fresh.push(snapshot_of())
    return fresh


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
    failures: list[Exception] = []
    for fresh in made:
        try:
            fresh.stop()
        except Exception as error:
            failures.append(error)
        finally:
            fresh.engine.dispose()
    if failures:
        raise ExceptionGroup("e2e servers failed to stop", failures)


@pytest.fixture(autouse=True)
def browser_errors(browser: Browser, monkeypatch: pytest.MonkeyPatch) -> Iterator[BrowserErrors]:
    """Watch even contexts made directly by a test or another fixture."""
    errors = BrowserErrors()
    original = browser.new_context

    def new_context(**kwargs: Any) -> BrowserContext:
        made = original(**kwargs)
        errors.attach(made)
        close = made.close

        def close_settled(**kwargs: Any) -> None:
            errors.settle()
            close(**kwargs)

        monkeypatch.setattr(made, "close", close_settled)
        return made

    monkeypatch.setattr(browser, "new_context", new_context)
    yield errors
    errors.assert_clean()


@pytest.fixture
def logged_in(server: E2EServer, page: Page) -> Page:
    log_in_with_pin(page, server.url, server.pin.current, SESSION_DEVICE)
    return page


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    """Fail a passing test whose browser reported an unexpected error."""
    result = yield
    errors = getattr(item, "funcargs", {}).get("browser_errors")
    if isinstance(errors, BrowserErrors):
        errors.assert_clean(require_expected=False)
    return result
