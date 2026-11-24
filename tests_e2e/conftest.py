"""Fixtures for the web dashboard's end-to-end browser suite (#152).

Run it with `make web-e2e`, which builds the browser app first. The browser is
the system Chrome (Playwright's `chrome` channel); nothing is downloaded.

- `server`: a fresh dashboard server (`tests_e2e.server.E2EServer`) per test,
  with isolated sessions/rate limits and an empty snapshot. Tests push state.
- `make_server`: additional fresh servers with their own databases and optional
  `WebDeps` overrides. `setup=True` starts the real registration-only setup app;
  `stop()` then `start()` serves the dashboard at the same origin and database.
  Override `server` to point `logged_in` at one of these.
- `browser_errors`: watches function-scoped sync contexts from any browser,
  including `browser.new_page`; rejects shared/persistent contexts and checks
  errors after the test body and again during teardown.
- `logged_in`: a fresh real PIN login against the test's `server`; no token is
  shared between tests.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Generator, Iterator
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser, BrowserContext, BrowserType, Page, expect
from sqlalchemy.engine import Engine

from jailbee.db import get_engine
from jailbee.web import assets
from tests.web_fakes import snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer

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

    def make(*, setup: bool = False, **overrides: Any) -> E2EServer:
        fresh = E2EServer(
            _engine_in(tmp_path_factory.mktemp("state")), _static_dir(), overrides=overrides
        )
        fresh.start(setup=setup)
        made.append(fresh)
        return fresh

    yield make
    failures: list[Exception] = []
    for fresh in made:
        try:
            fresh.stop()
        except Exception as error:
            failures.append(error)
        try:
            fresh.engine.dispose()
        except Exception as error:
            failures.append(error)
    if failures:
        raise ExceptionGroup("e2e server cleanup failed", failures)


@pytest.fixture(scope="session", autouse=True)
def collector_guard() -> Iterator[Callable[[BrowserErrors | None], None]]:
    """Guard public sync creation APIs even before function-scoped fixtures run."""
    active: BrowserErrors | None = None

    def select(errors: BrowserErrors | None) -> None:
        nonlocal active
        active = errors

    def collector() -> BrowserErrors:
        if active is None:
            pytest.fail("browser contexts require the function-scoped browser_errors collector")
        return active

    with pytest.MonkeyPatch.context() as patch:
        original_context = Browser.new_context
        original_page = Browser.new_page

        def watch(context: BrowserContext, errors: BrowserErrors) -> None:
            errors.attach(context)
            close = context.close

            def close_settled(**kwargs: Any) -> None:
                errors.settle()
                close(**kwargs)

            def watch_page(page: Page) -> None:
                close_page = page.close

                def close_page_settled(**kwargs: Any) -> None:
                    errors.settle()
                    close_page(**kwargs)

                def reject_content(*args: Any, **kwargs: Any) -> None:
                    pytest.fail(
                        "page.set_content replaces collector listeners; use page.goto instead"
                    )

                patch.setattr(page, "close", close_page_settled)
                patch.setattr(page, "set_content", reject_content)

            patch.setattr(context, "close", close_settled)
            context.on("page", watch_page)
            for page in context.pages:
                watch_page(page)

        def new_context(browser: Browser, **kwargs: Any) -> BrowserContext:
            errors = collector()
            made = original_context(browser, **kwargs)
            watch(made, errors)
            return made

        def new_page(browser: Browser, **kwargs: Any) -> Page:
            errors = collector()
            page = original_page(browser, **kwargs)
            watch(page.context, errors)
            return page

        def persistent_context(*args: Any, **kwargs: Any) -> BrowserContext:
            pytest.fail(
                "launch_persistent_context is not supported by the browser_errors collector"
            )

        patch.setattr(Browser, "new_context", new_context)
        patch.setattr(Browser, "new_page", new_page)
        patch.setattr(BrowserType, "launch_persistent_context", persistent_context)
        yield select


@pytest.fixture(autouse=True)
def browser_errors(
    collector_guard: Callable[[BrowserErrors | None], None],
) -> Iterator[BrowserErrors]:
    """Watch function-scoped contexts from any sync browser, including new_page."""
    errors = BrowserErrors()
    collector_guard(errors)
    try:
        yield errors
        errors.assert_clean()
    finally:
        collector_guard(None)


@pytest.fixture
def logged_in(server: E2EServer, page: Page) -> Page:
    log_in_with_pin(page, server.url, server.pin.current)
    return page


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, None, None]:
    """Fail a passing test whose browser reported an unexpected error."""
    result = yield
    errors = getattr(item, "funcargs", {}).get("browser_errors")
    if isinstance(errors, BrowserErrors):
        errors.assert_clean(require_expected=False)
    return result
