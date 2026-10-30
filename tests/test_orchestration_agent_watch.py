from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from jailbee.incus import IncusError
from jailbee.orchestration import labels, watch
from jailbee.orchestration.actions import Node

NOW = datetime(2026, 10, 30, tzinfo=UTC)


def supervised(**kwargs):
    return watch.WatchEntry("s1", "proj-kid", "spawn", NOW.isoformat(), supervise=True, **kwargs)


def advance(entry, status, *, seconds=0, child=None):
    from jailbee.orchestration.agent_watch import advance as step

    if child is None:
        child = Node("proj-kid", "kid", True, {labels.SPAWNED_BY: "proj-orch"})
    return step(entry, child, status, now=NOW + timedelta(seconds=seconds))


def test_alive_requires_two_separated_observations() -> None:
    from jailbee.orchestration.agent_watch import AgentStatus

    e, out = advance(supervised(), AgentStatus(True, "claude"))
    assert out is None and not e.confirmed
    e, out = advance(e, AgentStatus(True, "claude"), seconds=1)
    assert out is None
    e, out = advance(e, AgentStatus(True, "claude"), seconds=3)
    assert e.confirmed and out.state == "done" and "agent is alive" in out.detail
    assert advance(e, AgentStatus(True, "claude"), seconds=5)[1] is None


@pytest.mark.parametrize("rc", [0, 1, 127, 137, 143])
def test_early_exit_fails_and_late_exit_blocks(rc) -> None:
    from jailbee.orchestration.agent_watch import AgentStatus

    e, out = advance(supervised(), AgentStatus(False, "claude", rc))
    assert e is None and out.state == "failed" and f"status {rc}" in out.detail
    e, out = advance(supervised(confirmed=True), AgentStatus(False, "claude", rc))
    assert e is None and out.state == "failed"  # Broker relays later failure as blocked.


def test_never_started_times_out_and_unknown_probe_retries() -> None:
    from jailbee.orchestration.agent_watch import AgentStatus

    e, out = advance(supervised(), AgentStatus(False))
    assert out is None
    assert advance(e, AgentStatus(False), seconds=121)[1].state == "failed"
    assert advance(supervised(), None, seconds=200) == (supervised(), None)


def test_done_suppresses_but_external_stop_reports() -> None:
    from jailbee.orchestration.agent_watch import AgentStatus

    done = Node("proj-kid", "kid", True, {labels.STATE: labels.STATE_DONE})
    assert advance(supervised(confirmed=True), AgentStatus(False, rc=0), child=done) == (None, None)
    stopped = Node("proj-kid", "kid", False, {})
    assert advance(supervised(confirmed=True), None, child=stopped)[1].state == "failed"


def test_probe_live_and_exited() -> None:
    from jailbee.orchestration.agent_watch import AgentStatus, probe

    incus = MagicMock()
    incus.exec.return_value = json.dumps({"alive": True, "agent": "claude", "rc": None})
    assert probe(incus, "kid", uid=1000) == AgentStatus(True, "claude", None)
    assert incus.exec.call_args.kwargs == {"uid": 1000, "timeout": 15}
    incus.exec.return_value = json.dumps({"alive": False, "agent": "codex", "rc": 137})
    assert probe(incus, "kid", uid=1000) == AgentStatus(False, "codex", 137)


@pytest.mark.parametrize("value", ["", "no", "{}", '{"alive": "yes"}'])
def test_probe_bad_reply_is_unknown(value: str) -> None:
    from jailbee.orchestration.agent_watch import probe

    incus = MagicMock()
    incus.exec.return_value = value
    assert probe(incus, "kid", uid=1000) is None


def test_probe_error_is_retryable() -> None:
    from jailbee.orchestration.agent_watch import probe

    incus = MagicMock()
    incus.exec.side_effect = IncusError("temporarily unavailable")
    assert probe(incus, "kid", uid=1000) is None


@pytest.mark.parametrize(
    "rc,expected",
    [
        (0, "status 0"),
        (1, "status 1"),
        (127, "status 127"),
        (137, "SIGKILL"),
        (143, "SIGTERM"),
        (255, "signal 127"),
        (None, "unknown"),
    ],
)
def test_describe_exit(rc, expected) -> None:
    from jailbee.orchestration.agent_watch import AgentStatus, describe

    assert expected in describe(AgentStatus(False, "claude", rc))


@pytest.mark.parametrize(
    "state,start,boot,expected",
    [
        ("S", "42", "boot", True),
        ("Z", "42", "boot", False),
        ("S", "99", "boot", False),
        ("S", "42", "other", False),
    ],
)
def test_probe_script_checks_process_identity(
    monkeypatch, capsys, state, start, boot, expected
) -> None:
    from pathlib import Path

    from jailbee.orchestration.agent_watch import _PROBE

    files = {
        "runtime": json.dumps({"pid": 123, "start": "42", "boot": "boot", "agent": "claude"}),
        "/proc/123/stat": "123 (odd ) name) " + " ".join([state] + ["0"] * 18 + [start]),
        "/proc/sys/kernel/random/boot_id": boot,
    }

    def read(path, *args, **kwargs):
        if str(path) not in files:
            raise FileNotFoundError(str(path))
        return files[str(path)]

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr("sys.argv", ["probe", "runtime", "exit"])
    exec(compile(_PROBE, "probe", "exec"), {})
    assert json.loads(capsys.readouterr().out)["alive"] is expected


def test_output_tail_is_bounded_and_best_effort() -> None:
    from jailbee.orchestration.agent_watch import output_tail

    incus = MagicMock()
    incus.exec.return_value = "x" * 10000
    tail = output_tail(incus, "kid", "claude", uid=1000)
    assert len(tail) <= 4096
    assert "capture-pane" in " ".join(incus.exec.call_args.args[1])
    incus.exec.side_effect = IncusError("no pane")
    assert output_tail(incus, "kid", "claude", uid=1000) == ""
