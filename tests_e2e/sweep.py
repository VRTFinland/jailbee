"""The local read-only sweep: render every session of a Claude home and report what broke.

`make web-e2e-sweep CLAUDE_HOME=<path>` runs it (through `sweep_local.py`)
against the maintainer's own sessions; `test_sweep.py` runs it over a fixture
copy. The server only reads the home (the file watch is faked), and the sweep
itself only lists and reads files there. The report goes wherever the caller
says, under `.local/` for the make target, and lists only sessions with a
finding. Per session or subagent:

- JS error: a console error or uncaught page error while it rendered;
- CSP violation: a Content-Security-Policy report while it rendered;
- load error: the page showed an error instead of the transcript, or showed
  nothing within `RENDER_TIMEOUT_MS`;
- slow: the page took more than the threshold from navigation to the rendered
  transcript (app start included);
- unknown entry type: a record type, or a content block type of a user or
  assistant record, that the transcript reader neither renders nor knowingly
  skips (`RENDERED_BLOCKS`, `SKIPPED_RECORDS`), with its count and first line;
- unparsable line: a line that is not a JSON object.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import expect

from jailbee.transcripts.claude import ClaudeSource

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from tests_e2e.browser_errors import BrowserErrors

SWEEP_PREFIX = "sweep"
"""The repository prefix the sweep server serves the home under."""

RENDER_TIMEOUT_MS = 30_000
DEFAULT_SLOW_MS = 2_000

RENDERED_BLOCKS = frozenset(
    {
        ("user", "text"),
        ("user", "tool_result"),
        ("assistant", "text"),
        ("assistant", "thinking"),
        ("assistant", "tool_use"),
    }
)
"""Content blocks the transcript reader turns into entries."""

SKIPPED_RECORDS = frozenset(
    {
        "agent-name",
        "ai-title",
        "api-request",
        "api-request-blob",
        "api-request-shape",
        "atis-latch",
        "attachment",
        "bridge-session",
        "continued-in",
        "cost-state",
        "custom-title",
        "file-history-delta",
        "file-history-snapshot",
        "fork-context-ref",
        "frame-link",
        "history-suppression",
        "last-prompt",
        "mode",
        "permission-mode",
        "pr-link",
        "progress",
        "queue-operation",
        "session-owner",
        "summary",
        "system",
        "tools-used",
    }
)
"""Claude Code bookkeeping records the reader skips on purpose: no conversation."""

_LOAD_ERROR = re.compile(
    r"Session not found\.|Subagent not found\.|The session could not be loaded\."
)


@dataclass
class Finding:
    """How often one unknown shape occurred in a file, and its first line."""

    count: int = 0
    first_line: int = 0


@dataclass
class SessionResult:
    label: str
    url: str
    render_ms: int | None = None
    load_error: str | None = None
    js_errors: list[str] = field(default_factory=list)
    csp: list[str] = field(default_factory=list)
    unknown: dict[str, Finding] = field(default_factory=dict)
    unparsable: Finding = field(default_factory=Finding)
    slow: bool = False

    @property
    def clean(self) -> bool:
        return not (
            self.load_error
            or self.js_errors
            or self.csp
            or self.unknown
            or self.unparsable.count
            or self.slow
        )

    def lines(self, slow_ms: int) -> list[str]:
        found: list[str] = []
        if self.load_error:
            found.append(f"load error: {self.load_error}")
        found.extend(f"JS error: {text}" for text in self.js_errors)
        found.extend(f"CSP violation: {text}" for text in self.csp)
        if self.slow:
            found.append(f"slow: rendered in {self.render_ms} ms, over the {slow_ms} ms threshold")
        found.extend(
            f"unknown entry type `{name}` x{f.count} (first at line {f.first_line})"
            for name, f in sorted(self.unknown.items())
        )
        if self.unparsable.count:
            found.append(
                f"unparsable line x{self.unparsable.count} "
                f"(first at line {self.unparsable.first_line})"
            )
        return found


@dataclass
class SweepResult:
    home: Path
    slow_ms: int
    sessions: list[SessionResult]
    subagents: int
    seconds: float

    @property
    def summary(self) -> str:
        flagged = [s for s in self.sessions if not s.clean]

        tally = Counter[str]()
        for s in flagged:
            tally["load errors"] += bool(s.load_error)
            tally["with JS errors"] += bool(s.js_errors)
            tally["with CSP violations"] += bool(s.csp)
            tally["with unknown entry types"] += bool(s.unknown)
            tally["with unparsable lines"] += bool(s.unparsable.count)
            tally["slow"] += s.slow
        top = len(self.sessions) - self.subagents
        parts = ", ".join(f"{n} {name}" for name, n in tally.items() if n)
        return (
            f"Swept {top} session{'s' * (top != 1)} and {self.subagents} "
            f"subagent{'s' * (self.subagents != 1)}: {len(flagged)} with findings"
            + (f" ({parts})" if parts else "")
            + f" in {self.seconds:.0f} s."
        )

    def render(self) -> str:
        out = [f"# Web dashboard sweep of {self.home}", "", self.summary, ""]
        unknown: dict[str, list[int]] = {}
        for s in self.sessions:
            for name, finding in s.unknown.items():
                unknown.setdefault(name, []).append(finding.count)
        if unknown:
            out += ["Unknown entry types across the home:", ""]
            out += [
                f"- `{name}`: x{sum(counts)} in {len(counts)} session{'s' * (len(counts) != 1)}"
                for name, counts in sorted(unknown.items())
            ]
            out.append("")
        for s in self.sessions:
            if s.clean:
                continue
            out += [f"## {s.label}", "", f"`{s.url}`", ""]
            out += [f"- {line}" for line in s.lines(self.slow_ms)]
            out.append("")
        return "\n".join(out)


def scan(path: Path, result: SessionResult) -> None:
    """Record the unknown entry types and unparsable lines of one transcript file."""
    with path.open("rb") as stream:
        for number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if not isinstance(record, dict):
                _note(result.unparsable, number)
                continue
            kind = record.get("type")
            if kind not in ("user", "assistant"):
                if kind not in SKIPPED_RECORDS:
                    _note(result.unknown.setdefault(str(kind), Finding()), number)
                continue
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                block_type = block.get("type") if isinstance(block, dict) else None
                if (kind, block_type) not in RENDERED_BLOCKS:
                    _note(result.unknown.setdefault(f"{kind}/{block_type}", Finding()), number)


def _note(finding: Finding, line: int) -> None:
    finding.count += 1
    finding.first_line = finding.first_line or line


def targets(home: Path) -> list[tuple[SessionResult, Path]]:
    """Every session of ``home``, each followed by its subagents."""
    source = ClaudeSource()
    found: list[tuple[SessionResult, Path]] = []
    base = f"/#/repos/{SWEEP_PREFIX}/sessions/claude"
    for info in sorted(source.list_sessions(home), key=lambda i: str(i.path)):
        label = f"{info.path.parent.name}/{info.session_id}"
        found.append((SessionResult(label, f"{base}/{info.session_id}"), info.path))
        files = source.locate(home, info.session_id)
        for agent_id, path in sorted((files.subagents if files else {}).items()):
            found.append(
                (
                    SessionResult(
                        f"{label}/subagents/{agent_id}",
                        f"{base}/{info.session_id}/subagents/{agent_id}",
                    ),
                    path,
                )
            )
    return found


def _render(page: Page, base_url: str, result: SessionResult) -> None:
    # A hash change alone keeps the document, and the previous transcript with
    # it: start every session from a fresh document, so its errors are its own.
    page.goto("about:blank")
    started = time.monotonic()
    page.goto(base_url + result.url)
    error = page.get_by_text(_LOAD_ERROR)
    try:
        expect(page.get_by_role("article").or_(error)).to_be_visible(timeout=RENDER_TIMEOUT_MS)
    except AssertionError:
        result.load_error = f"nothing rendered within {RENDER_TIMEOUT_MS // 1000} s"
        return
    result.render_ms = round((time.monotonic() - started) * 1000)
    if error.count():
        result.load_error = error.first.inner_text()


def run_sweep(
    page: Page,
    base_url: str,
    home: Path,
    errors: BrowserErrors,
    report: Path,
    *,
    slow_ms: int = DEFAULT_SLOW_MS,
) -> SweepResult:
    """Open every session and subagent of ``home`` in ``page`` and write ``report``.

    ``page`` is logged in to a server that serves ``home`` under
    `SWEEP_PREFIX`. Browser errors are drained from ``errors`` per session, so
    they land in the report instead of failing the caller.
    """
    started = time.monotonic()
    errors.drain()
    sessions: list[SessionResult] = []
    subagents = 0
    for result, path in targets(home):
        scan(path, result)
        try:
            _render(page, base_url, result)
        except PlaywrightTimeout as timeout:
            result.load_error = f"navigation failed: {timeout.message.splitlines()[0]}"
        seen = errors.drain()
        result.js_errors = seen.console + seen.page
        result.csp = seen.csp
        result.slow = result.render_ms is not None and result.render_ms > slow_ms
        subagents += "/subagents/" in result.label
        sessions.append(result)
    sweep = SweepResult(home, slow_ms, sessions, subagents, time.monotonic() - started)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(sweep.render())
    return sweep
