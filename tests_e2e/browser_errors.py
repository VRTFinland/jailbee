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
from typing import TYPE_CHECKING, Literal

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, ConsoleMessage, WebError

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


Category = Literal["console", "page", "csp"]


@dataclass(frozen=True)
class ExpectedError:
    category: Category
    pattern: str | re.Pattern[str]
    minimum: int
    maximum: int | None

    def matches(self, text: str) -> bool:
        if isinstance(self.pattern, str):
            return self.pattern in text
        return self.pattern.fullmatch(text) is not None


@dataclass
class BrowserErrors:
    """Everything a browser context reported as an error, by kind."""

    console: list[str] = field(default_factory=list)
    page: list[str] = field(default_factory=list)
    csp: list[str] = field(default_factory=list)
    _expected: list[ExpectedError] = field(default_factory=list, init=False)
    _contexts: list[BrowserContext] = field(default_factory=list, init=False)

    def attach(self, context: BrowserContext) -> None:
        """Watch every page of ``context``, present and future."""
        self._contexts.append(context)
        context.expose_binding(_BINDING, lambda _source, text: self.csp.append(str(text)))
        context.add_init_script(_LISTENER)
        context.on("console", self._on_console)
        context.on("weberror", self._on_page_error)

    def _on_console(self, message: ConsoleMessage) -> None:
        if message.type == "error":
            self.console.append(message.text)

    def _on_page_error(self, event: WebError) -> None:
        error = event.error
        self.page.append(f"{error.name}: {error.message}")

    def expect(
        self,
        category: Category,
        pattern: str | re.Pattern[str],
        *,
        times: int | None = None,
        at_least: int | None = None,
    ) -> None:
        """Expect a literal substring, or a compiled regex matching the entire message.

        One declaration consumes one hit by default. Use ``times`` for an exact
        count or ``at_least`` for a required minimum with no upper bound.
        """
        if category not in ("console", "page", "csp"):
            raise ValueError(f"unknown error category: {category}")
        if times is not None and at_least is not None:
            raise ValueError("choose times or at_least, not both")
        count = at_least if at_least is not None else (times if times is not None else 1)
        if count < 1:
            raise ValueError("expected count must be positive")
        self._expected.append(
            ExpectedError(category, pattern, count, None if at_least is not None else count)
        )

    def unexpected(self, *, require_expected: bool = True) -> list[str]:
        entries = [
            (category, label, text)
            for category, label, texts in (
                ("console", "console error", self.console),
                ("page", "page error", self.page),
                ("csp", "CSP violation", self.csp),
            )
            for text in texts
        ]
        matches = [
            [
                i
                for i, expected in enumerate(self._expected)
                if expected.category == category and expected.matches(text)
            ]
            for category, _, text in entries
        ]
        slots = [i for i, expected in enumerate(self._expected) for _ in range(expected.minimum)]
        assigned: dict[int, int] = {}

        def assign(entry: int, seen: set[int]) -> bool:
            # Reassign an earlier hit when overlapping expectations need it.
            for slot, expected in enumerate(slots):
                if expected not in matches[entry] or slot in seen:
                    continue
                seen.add(slot)
                if slot not in assigned or assign(assigned[slot], seen):
                    assigned[slot] = entry
                    return True
            return False

        for entry in range(len(entries)):
            assign(entry, set())
        consumed = set(assigned.values())
        found = [
            f"{label}: {text}"
            for entry, (_, label, text) in enumerate(entries)
            if entry not in consumed
            and not any(self._expected[i].maximum is None for i in matches[entry])
        ]
        if require_expected:
            for i, expected in enumerate(self._expected):
                count = sum(slots[slot] == i for slot in assigned)
                if count < expected.minimum:
                    found.append(
                        f"unmet {expected.category} expectation {expected.pattern!r}: "
                        f"wanted {expected.minimum}, got {count}"
                    )
        return found

    def settle(self) -> None:
        """Round-trip every open page, so reports the browser already sent are handled.

        Playwright's sync API dispatches events only while it waits on the
        browser; without this an error raised just before the check is missed.
        """
        from playwright.sync_api import Error

        for context in self._contexts:
            for page in context.pages:
                with contextlib.suppress(Error):  # closed meanwhile
                    page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")

    def assert_clean(self, *, require_expected: bool = True) -> None:
        """Fail on errors; defer unmet declarations until teardown when requested."""
        self.settle()
        found = self.unexpected(require_expected=require_expected)
        if found:
            pytest.fail("the browser reported errors:\n  " + "\n  ".join(found), pytrace=False)
