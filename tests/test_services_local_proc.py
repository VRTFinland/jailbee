"""Tests for the container-local service process boundary."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from jailbee.services.local_proc import LocalProc


def test_shell_timeout_kills_group_and_reaps(monkeypatch, tmp_path):
    process = Mock(pid=123)
    process.wait.side_effect = [subprocess.TimeoutExpired("bash", 2), -9]
    popen = Mock(return_value=process)
    getpgid = Mock(return_value=123)
    killpg = Mock()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr("os.getpgid", getpgid)
    monkeypatch.setattr("os.killpg", killpg)

    result = LocalProc().run_shell("sleep 50", cwd=str(tmp_path), env={}, timeout=2, capture=False)

    assert result == 124
    assert popen.call_args.kwargs["start_new_session"] is True
    killpg.assert_called_once_with(123, signal.SIGKILL)
    assert process.wait.call_count == 2


def test_run_probe_captures_stdout_stderr_and_exit_code(tmp_path):
    result = LocalProc().run_probe(
        "printf out; printf err >&2; exit 3",
        cwd=str(tmp_path),
        env={},
        timeout=5,
    )
    assert (result.code, result.stdout, result.stderr, result.timed_out) == (3, "out", "err", False)


def test_run_probe_caps_each_output_stream(tmp_path):
    result = LocalProc().run_probe(
        "head -c 100000 /dev/zero | tr '\\0' x; head -c 100000 /dev/zero | tr '\\0' y >&2",
        cwd=str(tmp_path),
        env={},
        timeout=5,
    )
    assert len(result.stdout.encode()) == 16 * 1024
    assert len(result.stderr.encode()) == 16 * 1024
    assert set(result.stdout) == {"x"}
    assert set(result.stderr) == {"y"}


def test_run_probe_timeout_kills_process_group(tmp_path):
    result = LocalProc().run_probe(
        "printf out; printf err >&2; sleep 30",
        cwd=str(tmp_path),
        env={},
        timeout=0.1,
    )
    assert result.code is None
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.timed_out


def test_run_probe_timeout_does_not_wait_for_escaped_pipe_holder(tmp_path):
    pidfile = tmp_path / "escaped.pid"
    started = time.monotonic()
    result = LocalProc().run_probe(
        f"setsid sh -c 'echo $$ > {pidfile}; exec sleep 30' & sleep 0.1; sleep 30",
        cwd=str(tmp_path),
        env={},
        timeout=0.2,
    )
    elapsed = time.monotonic() - started
    try:
        assert result.code is None
        assert result.timed_out
        assert elapsed < 1.0
        assert len(result.stdout.encode()) <= 16 * 1024
        assert len(result.stderr.encode()) <= 16 * 1024
    finally:
        if pidfile.exists():
            try:
                os.killpg(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_interrupted_shell_kills_and_reaps_before_propagating(monkeypatch, tmp_path):
    process = Mock(pid=123)
    process.wait.side_effect = [KeyboardInterrupt(), -9]
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr("os.getpgid", Mock(side_effect=ProcessLookupError))
    monkeypatch.setattr("os.killpg", kill)
    with pytest.raises(KeyboardInterrupt):
        LocalProc().run_shell("sleep 50", cwd=str(tmp_path), env={}, timeout=2, capture=True)
    kill.assert_called_once_with(123, signal.SIGKILL)
    assert process.wait.call_count == 2


def test_service_group_excludes_caller_and_survives_leader(monkeypatch):
    getpgid = Mock(return_value=123)
    killpg = Mock()
    monkeypatch.setattr("os.getpgid", getpgid)
    monkeypatch.setattr("os.getpgrp", Mock(return_value=777))
    monkeypatch.setattr("os.killpg", killpg)
    proc = LocalProc()
    group = proc.service_pgid(123)
    assert group == 123
    getpgid.side_effect = ProcessLookupError
    proc.signal_group(group, signal.SIGKILL)
    killpg.assert_called_once_with(123, signal.SIGKILL)


def test_service_group_rejects_caller_group(monkeypatch):
    monkeypatch.setattr("os.getpgid", Mock(return_value=777))
    monkeypatch.setattr("os.getpgrp", Mock(return_value=777))
    assert LocalProc().service_pgid(123) is None


def test_shell_success_receives_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_PARENT_TEST", "parent")
    result = LocalProc().run_shell(
        'test "$SERVICE_PARENT_TEST:$SERVICE_CHILD_TEST" = parent:child',
        cwd=str(tmp_path),
        env={"SERVICE_CHILD_TEST": "child"},
        timeout=5,
        capture=True,
    )
    assert result == 0


def test_anchor_survives_pane_leader_and_checks_identity(tmp_path, monkeypatch):
    proc = LocalProc()
    monkeypatch.setattr(
        proc, "tmux", Mock(return_value=subprocess.CompletedProcess([], 0, str(os.getpid()), ""))
    )
    identity = tmp_path / "anchor.json"
    command = proc.service_command("sleep 60 & exit 42", identity)
    pane = subprocess.Popen(["bash", "-lc", command], start_new_session=True)
    try:
        assert pane.wait(timeout=10) == 42
        data = json.loads(identity.read_text())
        assert os.getpgid(data["pid"]) == pane.pid
        data["start"] = "0"
        identity.write_text(json.dumps(data))
        assert not proc.signal_identity(identity, signal.SIGKILL)
        data["start"] = Path(f"/proc/{data['pid']}/stat").read_text().rsplit(")", 1)[1].split()[19]
        identity.write_text(json.dumps(data))
        assert proc.signal_identity(identity, signal.SIGKILL)
    finally:
        try:
            os.killpg(pane.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        pane.wait()


def test_signal_identity_refuses_tmux_server(tmp_path, monkeypatch):
    server = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        pid = server.pid
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        identity = tmp_path / "server.json"
        identity.write_text(
            json.dumps(
                {
                    "pid": pid,
                    "pgid": pid,
                    "session": pid,
                    "start": stat[19],
                    "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                }
            )
        )
        proc = LocalProc()
        monkeypatch.setattr(
            proc, "tmux", Mock(return_value=subprocess.CompletedProcess([], 0, str(pid), ""))
        )
        monkeypatch.setattr("os.killpg", kill := Mock())
        assert not proc.signal_identity(identity, 0)
        kill.assert_not_called()
    finally:
        server.kill()
        server.wait()


def test_signal_identity_refuses_when_tmux_server_is_unknown(tmp_path, monkeypatch):
    identity = tmp_path / "anchor.json"
    identity.write_text(
        json.dumps(
            {
                "pid": 123,
                "pgid": 123,
                "session": 123,
                "start": "1",
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            }
        )
    )
    proc = LocalProc()
    monkeypatch.setattr(
        proc, "tmux", Mock(return_value=subprocess.CompletedProcess([], 1, "", "no server"))
    )
    monkeypatch.setattr("os.killpg", kill := Mock())
    assert not proc.signal_identity(identity, 0)
    kill.assert_not_called()


def test_killpg_ignores_vanished_process(monkeypatch):
    monkeypatch.setattr("os.getpgid", Mock(side_effect=ProcessLookupError))
    LocalProc().killpg(123, signal.SIGINT)
