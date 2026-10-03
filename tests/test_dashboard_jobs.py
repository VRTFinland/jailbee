"""Tests for the dashboard's detached command runner."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from jailbee.dashboard_jobs import (
    NEEDS_TERMINAL_MARKERS,
    JobResult,
    JobRunner,
    needs_terminal,
)


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _drain(runner: JobRunner, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while runner.active() and time.monotonic() < deadline:
        runner.poll()
        time.sleep(0.01)
    runner.poll()


def test_success_reports_returncode_to_callback(tmp_path: Path) -> None:
    results: list[JobResult] = []
    runner = JobRunner()
    runner.start("k", "doing", _py("pass"), tmp_path, results.append)
    _drain(runner)
    assert results == [JobResult(returncode=0, stderr="")]
    assert runner.active() == []


def test_failure_keeps_stderr_and_failure_line(tmp_path: Path) -> None:
    results: list[JobResult] = []
    runner = JobRunner()
    code = "import sys; sys.stderr.write('noise\\n\\nreal reason\\n\\n'); sys.exit(3)"
    runner.start("k", "doing", _py(code), tmp_path, results.append)
    _drain(runner)
    assert results[0].returncode == 3
    assert results[0].failure_line() == "real reason"


def test_callbacks_run_only_on_the_polling_thread(tmp_path: Path) -> None:
    import threading

    seen: list[threading.Thread] = []
    runner = JobRunner()
    runner.start("k", "doing", _py("pass"), tmp_path, lambda _r: seen.append(threading.current_thread()))
    deadline = time.monotonic() + 10
    while runner.active() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == []  # finished, but nothing delivered until poll()
    runner.poll()
    assert seen == [threading.current_thread()]


def test_key_is_busy_until_the_job_is_delivered(tmp_path: Path) -> None:
    runner = JobRunner()
    runner.start("k", "doing", _py("import time; time.sleep(0.3)"), tmp_path, lambda _r: None)
    assert runner.busy("k")
    assert not runner.busy("other")
    assert runner.active() == ["doing"]
    _drain(runner)
    assert not runner.busy("k")


def test_starting_a_busy_key_is_refused(tmp_path: Path) -> None:
    runner = JobRunner()
    runner.start("k", "doing", _py("import time; time.sleep(0.3)"), tmp_path, lambda _r: None)
    with pytest.raises(ValueError, match="already running"):
        runner.start("k", "again", _py("pass"), tmp_path, lambda _r: None)
    _drain(runner)


def test_unspawnable_command_raises_and_does_not_stay_busy(tmp_path: Path) -> None:
    runner = JobRunner()
    with pytest.raises(OSError):
        runner.start("k", "doing", _py("pass"), tmp_path / "vanished", lambda _r: None)
    assert not runner.busy("k")


def test_child_has_no_stdin(tmp_path: Path) -> None:
    results: list[JobResult] = []
    runner = JobRunner()
    code = "import sys; sys.exit(0 if sys.stdin.read() == '' else 1)"
    runner.start("k", "doing", _py(code), tmp_path, results.append)
    _drain(runner)
    assert results[0].returncode == 0


def test_needs_terminal_matches_the_questions_jb_new_asks() -> None:
    assert needs_terminal(JobResult(2, "error: ... there is no terminal to ask on. Re-run"))
    assert needs_terminal(JobResult(1, "Branch 'x' already exists in source repo.\nAborted!"))
    assert not needs_terminal(JobResult(1, "git fetch failed"))
    assert not needs_terminal(JobResult(0, "already exists in source repo"))


def test_markers_still_exist_in_the_cli_source() -> None:
    """The fallback leans on `jb new`'s wording; renaming it must fail here."""
    cli = (Path(__file__).parent.parent / "src" / "jailbee" / "cli.py").read_text()
    for marker in NEEDS_TERMINAL_MARKERS:
        assert marker in cli, marker
