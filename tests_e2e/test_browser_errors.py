"""The error collector every e2e test relies on: it reports what the browser saw."""

from __future__ import annotations

import re
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
def watched(
    browser: Browser, server: E2EServer, browser_errors: BrowserErrors
) -> Iterator[tuple[Page, BrowserErrors]]:
    """A dashboard page in a context of its own, using the test's collector."""
    context = browser.new_context()
    errors = browser_errors
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
    errors.expect("console", "kaboom")
    errors.expect("page", "Error: boom")
    errors.expect("csp", "script-src")
    errors.expect("console", "Content Security Policy")


def test_expected_errors_do_not_fail(watched: tuple[Page, BrowserErrors]) -> None:
    page, errors = watched

    break_the_page(page)
    errors.expect("console", re.compile("kaboom"))
    errors.expect("page", re.compile("Error: boom"))
    errors.expect("csp", "script-src")
    errors.expect("console", "Content Security Policy")

    errors.assert_clean()


def test_an_error_after_the_test_body_fails(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest(
        """
        pytest_plugins = ["tests_e2e.conftest"]
        import pytest

        @pytest.fixture
        def late_error(page):
            yield
            # Arm only after the call hook's check; no body-side wait can drain it.
            page.evaluate("setTimeout(() => { throw new Error('late kaboom') }, 0)")
        """
    )
    pytester.makepyfile("def test_late(page, late_error):\n    page.goto('about:blank')\n")
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--output", "results")

    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*page error: Error: late kaboom*"])


@pytest.mark.parametrize(
    ("declaration", "action", "message"),
    [
        ('expect("page", "kaboom")', "console.error('kaboom')", "console error: kaboom"),
        ('expect("console", "missing")', "0", "unmet console expectation"),
        (
            'expect("console", "kaboom")',
            "console.error('kaboom'); console.error('kaboom')",
            "console error: kaboom",
        ),
    ],
)
def test_expectations_are_strict(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    declaration: str,
    action: str,
    message: str,
) -> None:
    pytester.makeconftest('pytest_plugins = ["tests_e2e.conftest"]')
    pytester.makepyfile(
        f"""def test_error(page, browser_errors):
    browser_errors.{declaration}
    page.evaluate({action!r})
"""
    )
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--output", "results")

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines([f"*{message}*"])


def test_browser_convenience_page_errors_fail(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest('pytest_plugins = ["tests_e2e.conftest"]')
    pytester.makepyfile("""
        def test_error(browser):
            page = browser.new_page()
            page.evaluate("console.error('convenience kaboom')")
            page.close()
    """)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--output", "results")

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*console error: convenience kaboom*"])


def test_convenience_page_collects_csp_before_navigation(
    browser: Browser, browser_errors: BrowserErrors
) -> None:
    page = browser.new_page()
    try:
        page.evaluate("""() => {
            const policy = document.createElement("meta")
            policy.httpEquiv = "Content-Security-Policy"
            policy.content = "script-src 'none'"
            document.head.append(policy)
        }""")
        page.evaluate("""() => {
            const script = document.createElement("script")
            script.textContent = "window.inlineRan = true"
            document.body.append(script)
        }""")
        browser_errors.settle()
        assert any("script-src" in violation for violation in browser_errors.csp)
    finally:
        browser_errors.expect("console", "Content Security Policy")
        browser_errors.expect("csp", "script-src")
        page.close()


def test_replacing_the_document_is_rejected_loudly(browser: Browser) -> None:
    page = browser.new_page()
    try:
        with pytest.raises(
            pytest.fail.Exception, match=r"page\.set_content replaces collector listeners"
        ):
            page.set_content("<p>replacement</p>")
    finally:
        page.close()


def test_errors_in_a_second_launched_browser_fail(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest('pytest_plugins = ["tests_e2e.conftest"]')
    pytester.makepyfile("""
        def test_error(browser_type, browser_type_launch_args):
            second = browser_type.launch(**browser_type_launch_args)
            try:
                context = second.new_context()
                page = context.new_page()
                page.evaluate("console.error('second browser kaboom')")
                context.close()
            finally:
                second.close()
    """)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--output", "results")

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*console error: second browser kaboom*"])


def test_persistent_contexts_are_rejected_before_launch(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest('pytest_plugins = ["tests_e2e.conftest"]')
    pytester.makepyfile("""
        def test_persistent(browser_type, tmp_path):
            browser_type.launch_persistent_context(tmp_path)
    """)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--output", "results")

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*launch_persistent_context is not supported*"])


@pytest.mark.parametrize("scope", ["module", "session"])
def test_shared_contexts_are_rejected(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    pytester.makeconftest(f"""
        pytest_plugins = ["tests_e2e.conftest"]
        import pytest

        @pytest.fixture(scope={scope!r})
        def shared_page(browser):
            context = browser.new_context()
            yield context.new_page()
            context.close()
    """)
    pytester.makepyfile("def test_shared(shared_page): pass")
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--output", "results")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(
        ["*browser contexts require the function-scoped browser_errors collector*"]
    )


def test_popup_errors_from_a_fixture_created_context_fail(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest("""
        pytest_plugins = ["tests_e2e.conftest"]
        import pytest

        @pytest.fixture
        def extra_page(browser):
            context = browser.new_context()
            yield context.new_page()
            context.close()
    """)
    pytester.makepyfile("""
        def test_popup(extra_page):
            with extra_page.expect_popup() as popup:
                extra_page.evaluate("window.open('about:blank')")
            popup.value.evaluate("setTimeout(() => { throw new Error('popup kaboom') }, 0)")
    """)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--output", "results")

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*page error: Error: popup kaboom*"])


def test_overlapping_expectations_each_consume_a_distinct_hit(
    page: Page, browser_errors: BrowserErrors
) -> None:
    browser_errors.expect("console", "error")
    browser_errors.expect("console", "specific error")
    page.evaluate("console.error('specific error'); console.error('other error')")
    browser_errors.assert_clean()


def test_unbounded_and_exact_overlapping_expectations_share_hits_optimally(
    page: Page, browser_errors: BrowserErrors
) -> None:
    browser_errors.expect("console", "a", at_least=1)
    browser_errors.expect("console", "b")
    page.evaluate("console.error('ab'); console.error('a'); console.error('b')")
    browser_errors.assert_clean()


def test_expected_teardown_errors_do_not_fail_early(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest("""
        pytest_plugins = ["tests_e2e.conftest"]
        import pytest

        @pytest.fixture
        def teardown_error(page):
            yield
            page.evaluate("console.error('expected teardown error')")
    """)
    pytester.makepyfile("""
        def test_expected(page, browser_errors, teardown_error):
            browser_errors.expect("console", "expected teardown error")
    """)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--output", "results")

    result.assert_outcomes(passed=1)


@pytest.mark.parametrize("count", ["times=2", "at_least=1"])
def test_declared_counts_allow_repeated_errors(
    page: Page, browser_errors: BrowserErrors, count: str
) -> None:
    if count == "times=2":
        browser_errors.expect("console", "repeat", times=2)
    else:
        browser_errors.expect("console", "repeat", at_least=1)
    page.evaluate("console.error('repeat'); console.error('repeat')")
    browser_errors.assert_clean()
    browser_errors.assert_clean()  # Checks must not consume the declarations themselves.


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
                browser_errors.expect("console", "declared kaboom")
                page.goto(server.url)
                page.evaluate("console.error('declared kaboom')")
            """
        )
    )
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "--output", "results")

    result.assert_outcomes(passed=1, failed=1, errors=1)
    result.stdout.fnmatch_lines(["*console error: unexpected kaboom*"])
