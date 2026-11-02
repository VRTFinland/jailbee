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


def test_idle_episode_threshold_repeat_and_activity_reset() -> None:
    from jailbee.orchestration.agent_watch import advance_idle

    entry = supervised(confirmed=True)
    entry, body = advance_idle(entry, "session-turn-1", now=NOW, after=900)
    assert body is None
    entry, body = advance_idle(entry, "session-turn-1", now=NOW + timedelta(seconds=899), after=900)
    assert body is None
    entry, body = advance_idle(entry, "session-turn-1", now=NOW + timedelta(seconds=900), after=900)
    assert "15m" in body
    assert (
        advance_idle(entry, "session-turn-1", now=NOW + timedelta(seconds=1799), after=900)[1]
        is None
    )
    entry, body = advance_idle(
        entry, "session-turn-1", now=NOW + timedelta(seconds=1800), after=900
    )
    assert "30m" in body
    entry, body = advance_idle(
        entry, "session-turn-2", now=NOW + timedelta(seconds=1801), after=900
    )
    assert body is None
    assert (
        advance_idle(entry, "session-turn-2", now=NOW + timedelta(seconds=2700), after=900)[1]
        is None
    )
    entry, body = advance_idle(entry, None, now=NOW + timedelta(seconds=2701), after=900)
    assert body is None and entry.idle_since is None and entry.idle_reported_at is None
    assert (
        advance_idle(entry, "session-turn-2", now=NOW + timedelta(seconds=5000), after=900)[1]
        is None
    )


def test_blocked_message_suppresses_idle() -> None:
    from jailbee.orchestration.agent_watch import advance_idle

    entry = supervised(confirmed=True, last_message_kind="blocked", idle_since=NOW.isoformat())
    assert advance_idle(entry, "turn", now=NOW + timedelta(hours=1), after=900)[1] is None


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


@pytest.mark.parametrize(
    "state,subagents,expected",
    [
        ("waiting", 0, True),
        ("waiting", 1, True),
        ("idle", 0, True),
        ("idle", 1, False),
        ("busy", 0, False),
        ("shell", 0, False),
        ("unknown", 0, False),
    ],
)
def test_adapter_idle_signal_matches_only_prompted_process(
    tmp_path, mocker, state, subagents, expected
):
    from jailbee.accounts.models import AgentActivity, AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, idle_signal
    from tests.conftest import make_config

    cfg = make_config(tmp_path)
    adapter = MagicMock()
    session = AgentSession("claude", 123, 42, state, "input needed", NOW, 1, "session")
    adapter.read_sessions.return_value = [session]
    adapter.read_activity.return_value = AgentActivity(
        None, "last", subagents=subagents, modified=10, background_clear=subagents == 0
    )
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    status = AgentStatus(True, "claude", pid=123, proc_start=42)
    result = idle_signal(cfg, "kid", status, now=NOW)
    assert (result is not None) is expected
    assert (
        idle_signal(cfg, "kid", AgentStatus(True, "claude", pid=124, proc_start=42), now=NOW)
        is None
    )
    assert (
        idle_signal(cfg, "kid", AgentStatus(True, "claude", pid=123, proc_start=43), now=NOW)
        is None
    )


def test_old_background_work_cannot_be_treated_as_idle(tmp_path, mocker):
    from jailbee.accounts.models import AgentActivity, AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, idle_signal
    from tests.conftest import make_config

    adapter = MagicMock()
    adapter.read_sessions.return_value = [AgentSession("claude", 123, 42, "idle", None, NOW, 1)]
    # A freshness count of zero is only an estimate, not completion evidence.
    adapter.read_activity.return_value = AgentActivity(None, "last", subagents=0)
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    assert (
        idle_signal(
            make_config(tmp_path),
            "kid",
            AgentStatus(True, "claude", pid=123, proc_start=42),
            now=NOW,
        )
        is None
    )


def test_idle_signal_unknown_adapter_and_missing_transcript(tmp_path, mocker):
    from jailbee.accounts.models import AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, idle_signal
    from tests.conftest import make_config

    cfg = make_config(tmp_path)
    lookup = mocker.patch(
        "jailbee.accounts.adapters.base.get_adapter", side_effect=KeyError("codex")
    )
    status = AgentStatus(True, "codex", pid=123, proc_start=42)
    assert idle_signal(cfg, "kid", status, now=NOW) is None
    adapter = MagicMock()
    lookup.side_effect = None
    lookup.return_value = adapter
    adapter.read_sessions.return_value = [AgentSession("claude", 123, 42, "idle", None, NOW, 1)]
    adapter.locate_activity.return_value = None
    assert idle_signal(cfg, "kid", status, now=NOW) is None


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


def test_current_session_matches_prompted_pid(tmp_path, mocker):
    from jailbee.accounts.models import AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, current_session
    from tests.conftest import make_config

    adapter = MagicMock()
    a = "00000000-0000-0000-0000-000000000001"
    b = "00000000-0000-0000-0000-000000000002"
    adapter.read_sessions.return_value = [
        AgentSession("claude", 124, 42, "waiting", None, NOW, 1, a),
        AgentSession("claude", 123, 42, "waiting", None, NOW, 1, b),
    ]
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    assert (
        current_session(
            make_config(tmp_path), "kid", AgentStatus(True, "claude", pid=123, proc_start=42)
        )
        == b
    )
    assert (
        current_session(
            make_config(tmp_path), "kid", AgentStatus(True, "claude", pid=123, proc_start=43)
        )
        is None
    )


@pytest.mark.parametrize("session_id", ["../x", "$(id)", "AAAAAAAA-0000-0000-0000-000000000000"])
def test_current_session_rejects_non_uuid(tmp_path, mocker, session_id):
    from jailbee.accounts.models import AgentSession
    from jailbee.orchestration.agent_watch import AgentStatus, current_session
    from tests.conftest import make_config

    adapter = MagicMock()
    adapter.read_sessions.return_value = [
        AgentSession("claude", 123, 42, "waiting", None, NOW, 1, session_id)
    ]
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    assert (
        current_session(
            make_config(tmp_path), "kid", AgentStatus(True, "claude", pid=123, proc_start=42)
        )
        is None
    )


def test_current_session_reader_raises_returns_none(tmp_path, mocker):
    from jailbee.orchestration.agent_watch import AgentStatus, current_session
    from tests.conftest import make_config

    adapter = MagicMock()
    adapter.read_sessions.side_effect = RuntimeError("broken")
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=adapter)
    assert (
        current_session(
            make_config(tmp_path), "kid", AgentStatus(True, "claude", pid=123, proc_start=42)
        )
        is None
    )
