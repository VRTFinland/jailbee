"""The error collector every e2e test relies on: it reports what the browser saw."""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests_e2e.browser_errors import CSP_REPORTED, BrowserErrors
from tests_e2e.server import E2EServer

pytest_plugins = ["pytester"]

REPO_ROOT = Path(__file__).resolve().parent.parent

BREAK_THE_PAGE = """() => {
    console.error("kaboom")
    setTimeout(() => { throw new Error("boom") })
    const script = document.createElement("script")
    script.textContent = "window.inlineRan = true"
    document.body.append(script)
}"""


@pytest.fixture
def watched(browser: Browser, server: E2EServer) -> Iterator[tuple[Page, BrowserErrors]]:
    """A dashboard page in a context of its own, with a collector of its own."""
    context = browser.new_context()
    errors = BrowserErrors()
    errors.attach(context)
    page = context.new_page()
    page.goto(server.url)
    expect(page.get_by_role("heading", name="JailBee")).to_be_visible()
    yield page, errors
    context.close()


def break_the_page(page: Page) -> None:
    """Log a console error, throw an uncaught error and run a CSP-blocked inline script."""
    with page.expect_event("pageerror"):
        page.evaluate(BREAK_THE_PAGE)
    page.wait_for_function(f"({CSP_REPORTED})() > 0")


def test_console_errors_page_errors_and_csp_violations_are_reported(
    watched: tuple[Page, BrowserErrors],
) -> None:
    page, errors = watched

    break_the_page(page)

    with pytest.raises(pytest.fail.Exception) as failure:
        errors.assert_clean()
    message = str(failure.value)
    assert "console error: kaboom" in message
    assert "page error: Error: boom" in message
    assert "CSP violation: script-src" in message


def test_expected_errors_do_not_fail(watched: tuple[Page, BrowserErrors]) -> None:
    page, errors = watched

    break_the_page(page)
    errors.expect("kaboom")
    errors.expect("boom")
    errors.expect("script-src")
    errors.expect("Content Security Policy")

    errors.assert_clean()


def test_a_test_whose_page_logs_an_error_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest('pytest_plugins = ["tests_e2e.conftest"]')
    pytester.makepyfile(
        textwrap.dedent(
            """
            def test_logs_an_error(server, page):
                page.goto(server.url)
                page.evaluate("console.error('unexpected kaboom')")

            def test_declares_it(server, page, browser_errors):
                browser_errors.expect("declared kaboom")
                page.goto(server.url)
                page.evaluate("console.error('declared kaboom')")
            """
        )
    )
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--output", "results")

    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*console error: unexpected kaboom*"])
