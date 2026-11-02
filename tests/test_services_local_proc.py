"""Tests for the container-local service process boundary."""

import signal
import subprocess
from unittest.mock import Mock

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


def test_killpg_ignores_vanished_process(monkeypatch):
    monkeypatch.setattr("os.getpgid", Mock(side_effect=ProcessLookupError))
    LocalProc().killpg(123, signal.SIGINT)
