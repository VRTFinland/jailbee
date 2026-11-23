"""`make web-e2e-sweep`: sweep the maintainer's own Claude home (never CI).

Not collected by `make web-e2e` (no `test_` prefix); the make target names
this file. `JAILBEE_SWEEP_HOME` is the home to read, `JAILBEE_SWEEP_REPORT`
the report to write and `JAILBEE_SWEEP_SLOW_MS` the render-time threshold.
The format is described in `tests_e2e/sweep.py`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page

from tests.web_fakes import repo_group, snapshot_of
from tests_e2e.browser_errors import BrowserErrors
from tests_e2e.server import E2EServer
from tests_e2e.sweep import DEFAULT_SLOW_MS, SWEEP_PREFIX, run_sweep


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.exit(f"{name} is not set; run `make web-e2e-sweep`", returncode=2)
    return value


@pytest.fixture
def agent_home() -> Path:
    home = Path(_required("JAILBEE_SWEEP_HOME")).expanduser().resolve()
    if not (home / "projects").is_dir():
        pytest.exit(f"{home} has no projects/ directory: not a Claude home", returncode=2)
    return home


@pytest.fixture
def server(make_server: Callable[..., E2EServer], agent_home: Path) -> E2EServer:
    server = make_server(agent_homes={SWEEP_PREFIX: [("claude", agent_home)]}.get)
    server.push(snapshot_of(repo_group(SWEEP_PREFIX)))
    return server


def test_local_sweep(
    logged_in: Page, server: E2EServer, agent_home: Path, browser_errors: BrowserErrors
) -> None:
    report = Path(_required("JAILBEE_SWEEP_REPORT"))
    slow_ms = int(os.environ.get("JAILBEE_SWEEP_SLOW_MS") or DEFAULT_SLOW_MS)
    result = run_sweep(logged_in, server.url, agent_home, browser_errors, report, slow_ms=slow_ms)
    print(f"\n{result.summary}\nReport: {report}")
