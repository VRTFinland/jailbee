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
from playwright.sync_api import Page

from tests.web_fakes import repo_group, snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.server import E2EServer
from tests_e2e.sweep import SWEEP_PREFIX, run_sweep

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
    server = make_server(agent_homes={SWEEP_PREFIX: [("claude", agent_home)]}.get)
    server.push(snapshot_of(repo_group(SWEEP_PREFIX)))
    return server


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
    assert CLEAN_ID not in report.partition("\n## ")[2]


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
