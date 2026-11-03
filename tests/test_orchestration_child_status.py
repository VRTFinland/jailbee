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


def _tree():
    # sh(2) -> claude(3, the agent) -> bash(4) -> pytest(5), sleep(6)
    return {
        1: ProcSample("tmux", 0, 10),
        2: ProcSample("sh", 0, 20, ppid=1),
        3: ProcSample("claude", 0, 30, ppid=2),
        4: ProcSample("bash", 0, 40, ppid=3),
        5: ProcSample("pytest", 0, 100, ppid=4),
        6: ProcSample("sleep", 0, 300, ppid=4),
    }


def test_longest_command_picks_oldest_tool_under_the_agent():
    assert longest_command(_tree(), agent_pid=3, uptime=1100.0, clk_tck=100) == {
        "command": "pytest",
        "seconds": 1099,
    }


def test_longest_command_excludes_agent_that_is_child_of_a_shell():
    samples = {k: v for k, v in _tree().items() if k < 4}
    assert longest_command(samples, agent_pid=3, uptime=1100.0, clk_tck=100) is None


def test_longest_command_unknown_agent_is_none_not_the_agent():
    assert longest_command(_tree(), agent_pid=None, uptime=1100.0, clk_tck=100) is None
    assert longest_command(_tree(), agent_pid=99, uptime=1100.0, clk_tck=100) is None


def test_longest_command_ignores_processes_outside_the_agent_tree():
    samples = {**_tree(), 7: ProcSample("old", 0, 1, ppid=2), 8: ProcSample("bash", 0, 5, ppid=1)}
    samples[9] = ProcSample("older", 0, 2, ppid=8)
    assert longest_command(samples, agent_pid=3, uptime=1100.0, clk_tck=100)["command"] == "pytest"


def test_longest_command_excludes_inbox_waiters_by_comm_and_cmdline():
    samples = _tree()
    samples[7] = ProcSample("jailbee-inbox", 0, 50, ppid=4)
    samples[8] = ProcSample("python3", 0, 60, ppid=4)
    cmd = {8: "/usr/bin/python3 /usr/local/bin/jailbee inbox wait --quiet-after 5m"}
    got = longest_command(
        samples, agent_pid=3, uptime=1100.0, clk_tck=100, cmdline=lambda pid: cmd.get(pid)
    )
    assert got == {"command": "pytest", "seconds": 1099}
    only_waiters = {k: samples[k] for k in (1, 2, 3, 4, 7, 8)}
    assert (
        longest_command(
            only_waiters, agent_pid=3, uptime=1100.0, clk_tck=100, cmdline=lambda pid: cmd.get(pid)
        )
        is None
    )


def test_head_script_never_takes_the_index_lock() -> None:
    incus = _head_incus("CLEAN\n")
    probe_head(incus, "kid", "/repo", uid=1)
    script = incus.exec.call_args.args[1][2]
    assert "GIT_OPTIONAL_LOCKS=0" in script
    assert script.count("git --no-optional-locks") == 2
    assert "git -C" not in script


def test_status_cache_prune_drops_vanished():
    cache = StatusCache()
    cache.host_at.update({"a": 1.0, "b": 1.0})
    cache.exec_at.update({"a": 1.0, "b": 1.0})
    cache.agent.update({"a": MagicMock(), "b": MagicMock()})
    cache.sampler_for("o1")
    cache.sampler_for("o2")
    cache.prune({"a"}, {"o1"})
    assert set(cache.host_at) == set(cache.exec_at) == set(cache.agent) == {"a"}
    assert set(cache.samplers) == {"o1"}


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
