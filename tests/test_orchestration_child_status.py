"""Pure status model for child containers (issue #64, task 1)."""

from __future__ import annotations

from jailbee.orchestration.child_status import (
    GIT_INTERVAL,
    STATUS_INTERVAL,
    StatusCache,
    activity_at,
    clean_text,
    longest_command,
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
