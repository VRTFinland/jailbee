"""Pure status model for child containers (issue #64, task 1)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jailbee.incus import IncusError
from jailbee.orchestration.child_status import (
    GIT_INTERVAL,
    STATUS_INTERVAL,
    StatusCache,
    activity_at,
    clean_text,
    longest_command,
    probe_head,
)
from jailbee.procstat import ProcSample


def test_clean_text_replaces_controls_and_truncates():
    assert clean_text("a\x1b[31mb\x07", 120) == "a?[31mb?"
    assert clean_text("x" * 200, 120) == "x" * 119 + "…"


def test_clean_text_replaces_del_and_c1_controls():
    assert clean_text("a\x7fb\x9bc", 120) == "a?b?c"


def test_longest_command_picks_oldest_child_of_a_shell():
    samples = {
        1: ProcSample("tmux", 0, 10),
        2: ProcSample("bash", 0, 20, ppid=1),
        3: ProcSample("pytest", 0, 100, ppid=2),
        4: ProcSample("sleep", 0, 300, ppid=2),
        5: ProcSample("bash", 0, 50, ppid=2),
        6: ProcSample("node", 0, 30, ppid=1),
    }
    assert longest_command(samples, uptime=1100.0, clk_tck=100) == {
        "command": "pytest",
        "seconds": 1099,
    }


def test_longest_command_none_without_shell_children():
    assert longest_command({1: ProcSample("claude", 0, 5)}, uptime=10.0, clk_tck=100) is None


def test_activity_at_ignores_unknown():
    assert (
        activity_at(started_at=10.0, agent_last_event_at=None, head_time=50.0, last_busy_at=None)
        == 50.0
    )


def test_status_cache_intervals():
    c = StatusCache()
    assert c.host_due("x", 100.0) and not c.host_due("x", 129.0) and c.host_due("x", 130.0)
    assert c.exec_due("x", 100.0) and not c.exec_due("x", 159.0)


def test_interval_constants():
    assert STATUS_INTERVAL == 30
    assert GIT_INTERVAL == 60


SHA = "a" * 40


def _head_incus(output=None, error=None):
    incus = MagicMock()
    if error:
        incus.exec.side_effect = error
    else:
        incus.exec.return_value = output
    return incus


def test_probe_head_parses() -> None:
    incus = _head_incus(f"{SHA}\x1f1762160000\x1ffix: x\nDIRTY\n")
    assert probe_head(incus, "kid", "/repo", uid=1000) == (
        {"sha": SHA, "time": 1762160000.0, "subject": "fix: x"},
        True,
    )


def test_probe_head_no_commits() -> None:
    assert probe_head(_head_incus("CLEAN\n"), "kid", "/repo", uid=1000) == (None, False)


def test_probe_head_exec_error() -> None:
    assert probe_head(_head_incus(error=IncusError("x")), "kid", "/repo", uid=1000) == (None, None)


def test_probe_head_unparseable() -> None:
    assert probe_head(_head_incus("garbage"), "kid", "/repo", uid=1000) == (None, None)


def test_probe_head_passes_repo_dir_as_argument() -> None:
    incus = _head_incus("CLEAN\n")
    probe_head(incus, "kid", "/repo; rm -rf /", uid=7)
    args, kwargs = incus.exec.call_args
    argv = args[1]
    assert argv[:2] == ["sh", "-c"] and argv[3:] == ["sh", "/repo; rm -rf /"]
    assert "/repo" not in argv[2]
    assert kwargs["uid"] == 7


def test_probe_head_error_sentinel_bare() -> None:
    assert probe_head(_head_incus("ERROR\n"), "kid", "/repo", uid=1) == (None, None)


def test_probe_head_error_sentinel_after_log_line() -> None:
    out = f"{SHA}\x1f1762160000\x1fsubj\nERROR\n"
    assert probe_head(_head_incus(out), "kid", "/repo", uid=1) == (None, None)


def test_probe_head_hostile_subject_is_cleaned_and_capped() -> None:
    subject = "\x1b[31m" + "A" * 10_000
    head, dirty = probe_head(
        _head_incus(f"{SHA}\x1f1762160000\x1f{subject}\nCLEAN\n"), "k", "/r", uid=1
    )
    assert dirty is False
    assert head is not None
    text = head["subject"]
    assert isinstance(text, str)
    assert len(text) == 120 and text.endswith("…")
    assert "\x1b" not in text and text.startswith("?[31m")


def test_probe_head_non_integer_time() -> None:
    out = f"{SHA}\x1fnow\x1fsubj\nCLEAN\n"
    assert probe_head(_head_incus(out), "kid", "/repo", uid=1) == (None, None)


def test_probe_head_clean_repo_with_commit() -> None:
    out = f"{SHA}\x1f1762160000\x1fsubj\nCLEAN\n"
    assert probe_head(_head_incus(out), "kid", "/repo", uid=1) == (
        {"sha": SHA, "time": 1762160000.0, "subject": "subj"},
        False,
    )


def test_probe_head_accepts_sha256_length_sha() -> None:
    sha = "0123456789abcdef" * 4
    head, _ = probe_head(_head_incus(f"{sha}\x1f1762160000\x1fs\nCLEAN\n"), "k", "/r", uid=1)
    assert head is not None and head["sha"] == sha


@pytest.mark.parametrize(
    "sha",
    [
        "abc",
        "A" * 40,
        "a" * 39,
        "a" * 65,
        "\x1b[2J" + "a" * 40,
        "a" * 40 + "\x1b[31m",
        "a" * 1_000_000,
        "g" * 40,
    ],
)
def test_probe_head_rejects_hostile_sha(sha: str) -> None:
    out = f"{sha}\x1f1762160000\x1fsubj\nCLEAN\n"
    assert probe_head(_head_incus(out), "kid", "/repo", uid=1) == (None, None)
