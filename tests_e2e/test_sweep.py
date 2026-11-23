"""The local read-only sweep (`make web-e2e-sweep`), run over a fixture Claude home.

The fixture copy gets one deliberately broken session: an entry of a type the
renderer does not know and a line that is not JSON. The report must name both,
leave the clean sessions out of its findings, and the home must be untouched.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from tests_e2e import sweep
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.server import E2EServer
from tests_e2e.sweep import run_sweep, serve_home

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "transcripts" / "claude"
BROKEN_ID = "22222222-2222-4222-8222-222222222222"
CLEAN_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    home = tmp_path / "claude"
    shutil.copytree(FIXTURES, home)
    broken = home / "projects" / "-work-repo-other" / f"{BROKEN_ID}.jsonl"
    record = {
        "uuid": "c0000009-0000-4000-8000-000000000000",
        "timestamp": "2026-10-30T08:01:00.000Z",
        "sessionId": BROKEN_ID,
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "hologram", "data": "?"}]},
    }
    with broken.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
        stream.write('{"type": "user", "message": \n')
    return home


@pytest.fixture
def server(make_server: Callable[..., E2EServer], agent_home: Path) -> E2EServer:
    return serve_home(make_server, agent_home)


def fingerprint(home: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(path.relative_to(home)): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(home.rglob("*"))
        if path.is_file()
    }


def sections(report: str) -> dict[str, str]:
    """The report's per-session sections, by heading."""
    found: dict[str, str] = {}
    for part in report.split("\n## ")[1:]:
        heading, _, body = part.partition("\n")
        found[heading] = body
    return found


def test_sweep_reports_the_broken_entry_and_reads_only(
    logged_in: Page,
    server: E2EServer,
    agent_home: Path,
    browser_errors: BrowserErrors,
    tmp_path: Path,
) -> None:
    before = fingerprint(agent_home)
    out = tmp_path / "report.md"

    result = run_sweep(logged_in, server.url, agent_home, browser_errors, out, slow_ms=60_000)

    report = out.read_text()
    assert fingerprint(agent_home) == before
    assert result.summary in report
    assert result.summary.startswith("Swept 2 sessions and 1 subagent: 1 with findings")
    assert "- `assistant/hologram`: x1 in 1 session" in report.partition("\n## ")[0]
    found = sections(report)
    assert list(found) == [f"-work-repo-other/{BROKEN_ID}"]
    broken = found[f"-work-repo-other/{BROKEN_ID}"]
    assert "unknown entry type `assistant/hologram` x1 (first at line 3)" in broken
    assert "unparsable line x1 (first at line 4)" in broken
    assert "JS error" not in broken
    assert "slow" not in broken


def test_sweep_reports_renders_over_the_threshold(
    logged_in: Page,
    server: E2EServer,
    agent_home: Path,
    browser_errors: BrowserErrors,
    tmp_path: Path,
) -> None:
    out = tmp_path / "report.md"

    run_sweep(logged_in, server.url, agent_home, browser_errors, out, slow_ms=0)

    found = sections(out.read_text())
    clean = f"-work-repo/{CLEAN_ID}"
    assert set(found) == {
        clean,
        f"{clean}/subagents/a1b2c3d4e5f6a7b8c",
        f"-work-repo-other/{BROKEN_ID}",
    }
    assert "over the 0 ms threshold" in found[clean]


def test_sweep_attributes_a_browser_error_to_the_session_that_raised_it(
    logged_in: Page,
    server: E2EServer,
    agent_home: Path,
    browser_errors: BrowserErrors,
    tmp_path: Path,
) -> None:
    # Stands in for a renderer bug: an uncaught error on one session's page only.
    logged_in.context.add_init_script(
        f"if (location.hash.endsWith('{CLEAN_ID}')) "
        "setTimeout(() => { throw new Error('sweep probe') })"
    )
    out = tmp_path / "report.md"

    run_sweep(logged_in, server.url, agent_home, browser_errors, out, slow_ms=60_000)

    found = sections(out.read_text())
    assert "JS error: Error: sweep probe" in found[f"-work-repo/{CLEAN_ID}"]
    assert "JS error" not in found[f"-work-repo-other/{BROKEN_ID}"]
    assert f"-work-repo/{CLEAN_ID}/subagents/a1b2c3d4e5f6a7b8c" not in found


EMPTY_ID = "33333333-3333-4333-8333-333333333333"


def add_session(home: Path, session_id: str, records: list[dict[str, object]]) -> None:
    path = home / "projects" / "-work-repo" / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_sweep_renders_an_empty_session_without_a_load_error(
    logged_in: Page,
    server: E2EServer,
    agent_home: Path,
    browser_errors: BrowserErrors,
    tmp_path: Path,
) -> None:
    # Only bookkeeping: the page says "This session is empty." over an empty,
    # zero-height transcript article.
    add_session(
        agent_home,
        EMPTY_ID,
        [
            {
                "type": "file-history-snapshot",
                "messageId": "m1",
                "snapshot": {},
                "sessionId": EMPTY_ID,
                "timestamp": "2026-10-30T08:00:00.000Z",
            }
        ],
    )
    out = tmp_path / "report.md"

    result = run_sweep(logged_in, server.url, agent_home, browser_errors, out, slow_ms=60_000)

    labels = [s.label for s in result.sessions]
    assert f"-work-repo/{EMPTY_ID}" in labels
    assert f"-work-repo/{EMPTY_ID}" not in sections(out.read_text())


def test_sweep_reports_an_unknown_record_type(
    logged_in: Page,
    server: E2EServer,
    agent_home: Path,
    browser_errors: BrowserErrors,
    tmp_path: Path,
) -> None:
    clean = agent_home / "projects" / "-work-repo" / f"{CLEAN_ID}.jsonl"
    with clean.open("a") as stream:
        stream.write(json.dumps({"type": "teleport", "sessionId": CLEAN_ID}) + "\n")
    lines = len(clean.read_text().splitlines())
    out = tmp_path / "report.md"

    run_sweep(logged_in, server.url, agent_home, browser_errors, out, slow_ms=60_000)

    report = out.read_text()
    assert "- `teleport`: x1 in 1 session" in report.partition("\n## ")[0]
    assert f"unknown entry type `teleport` x1 (first at line {lines})" in sections(report)[
        f"-work-repo/{CLEAN_ID}"
    ]


def test_sweep_attributes_a_csp_violation_to_the_session_that_caused_it(
    logged_in: Page,
    server: E2EServer,
    agent_home: Path,
    browser_errors: BrowserErrors,
    tmp_path: Path,
) -> None:
    # Stands in for a renderer that emits an inline script on one session only.
    logged_in.context.add_init_script(
        f"if (location.hash.endsWith('{CLEAN_ID}')) addEventListener('DOMContentLoaded', () => {{"
        " const s = document.createElement('script');"
        " s.textContent = 'void 0'; document.head.appendChild(s) })"
    )
    out = tmp_path / "report.md"

    run_sweep(logged_in, server.url, agent_home, browser_errors, out, slow_ms=60_000)

    found = sections(out.read_text())
    assert "CSP violation: script-src" in found[f"-work-repo/{CLEAN_ID}"]
    assert "CSP violation" not in found[f"-work-repo-other/{BROKEN_ID}"]
    assert f"-work-repo/{CLEAN_ID}/subagents/a1b2c3d4e5f6a7b8c" not in found


def test_sweep_reports_a_page_that_says_the_subagent_is_missing(
    logged_in: Page,
    server: E2EServer,
    agent_home: Path,
    browser_errors: BrowserErrors,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = sweep.targets
    missing = f"-work-repo/{CLEAN_ID}/subagents/afeedfeedfeedfeed"

    def with_a_missing_subagent(home: Path) -> list[tuple[sweep.SessionResult, Path]]:
        found = real(home)
        url = f"/#/repos/{sweep.SWEEP_PREFIX}/sessions/claude/{CLEAN_ID}/subagents/afeedfeedfeedfeed"
        return [*found, (sweep.SessionResult(missing, url), found[0][1])]

    monkeypatch.setattr(sweep, "targets", with_a_missing_subagent)
    out = tmp_path / "report.md"

    run_sweep(logged_in, server.url, agent_home, browser_errors, out, slow_ms=60_000)

    assert "load error: Subagent not found." in sections(out.read_text())[missing]


def test_sweep_records_a_browser_failure_and_still_writes_the_report(
    logged_in: Page,
    server: E2EServer,
    agent_home: Path,
    browser_errors: BrowserErrors,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = sweep._render

    def crash_on_the_broken_session(page: Page, base_url: str, result: sweep.SessionResult) -> None:
        if result.label.endswith(BROKEN_ID):
            raise PlaywrightError("Target crashed")
        real(page, base_url, result)

    monkeypatch.setattr(sweep, "_render", crash_on_the_broken_session)
    out = tmp_path / "report.md"

    result = run_sweep(logged_in, server.url, agent_home, browser_errors, out, slow_ms=60_000)

    assert len(result.sessions) == 3
    assert "load error: navigation failed: Target crashed" in sections(out.read_text())[
        f"-work-repo-other/{BROKEN_ID}"
    ]
