"""Collects what a page reports as broken: console errors, page errors, CSP violations.

Every e2e test gets one (the `browser_errors` fixture) and fails on any entry
it did not declare with `expect`. A Content-Security-Policy violation is
reported twice by Chrome: as a `securitypolicyviolation` DOM event (relayed
through an exposed binding, which the page's own CSP cannot block) and as a
"Refused to ..." console error.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, ConsoleMessage, Error, Page

_BINDING = "__e2eCspViolation"
_REPORTED = "__e2eCspReported"
_LISTENER = f"""
window.{_REPORTED} = 0
document.addEventListener("securitypolicyviolation", async (e) => {{
    await window.{_BINDING}(`${{e.violatedDirective}} blocked ${{e.blockedURI || "inline"}}`)
    window.{_REPORTED} += 1
}})
"""
CSP_REPORTED = f"() => window.{_REPORTED}"
"""A page expression: how many CSP violations have reached the collector."""


@dataclass
class BrowserErrors:
    """Everything a browser context reported as an error, by kind."""

    console: list[str] = field(default_factory=list)
    page: list[str] = field(default_factory=list)
    csp: list[str] = field(default_factory=list)
    _expected: list[re.Pattern[str]] = field(default_factory=list, init=False)
    _contexts: list[BrowserContext] = field(default_factory=list, init=False)

    def attach(self, context: BrowserContext) -> None:
        """Watch every page of ``context``, present and future."""
        self._contexts.append(context)
        context.expose_binding(_BINDING, lambda _source, text: self.csp.append(str(text)))
        context.add_init_script(_LISTENER)
        for page in context.pages:
            self._watch(page)
        context.on("page", self._watch)

    def _watch(self, page: Page) -> None:
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)

    def _on_console(self, message: ConsoleMessage) -> None:
        if message.type == "error":
            self.console.append(message.text)

    def _on_page_error(self, error: Error) -> None:
        self.page.append(f"{error.name}: {error.message}")

    def expect(self, pattern: str) -> None:
        """Allow entries matching the regular expression ``pattern``."""
        self._expected.append(re.compile(pattern))

    def unexpected(self) -> list[str]:
        entries = (
            [f"console error: {t}" for t in self.console]
            + [f"page error: {t}" for t in self.page]
            + [f"CSP violation: {t}" for t in self.csp]
        )
        return [e for e in entries if not any(p.search(e) for p in self._expected)]

    def settle(self) -> None:
        """Round-trip every open page, so reports the browser already sent are handled.

        Playwright's sync API dispatches events only while it waits on the
        browser; without this an error raised just before the check is missed.
        """
        from playwright.sync_api import Error

        for context in self._contexts:
            for page in context.pages:
                with contextlib.suppress(Error):  # closed meanwhile
                    page.evaluate("0")

    def assert_clean(self) -> None:
        """Fail the current test when anything unexpected was reported."""
        self.settle()
        found = self.unexpected()
        if found:
            pytest.fail("the browser reported errors:\n  " + "\n  ".join(found), pytrace=False)
