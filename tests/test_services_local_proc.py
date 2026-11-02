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


def test_killpg_ignores_vanished_process(monkeypatch):
    monkeypatch.setattr("os.getpgid", Mock(side_effect=ProcessLookupError))
    LocalProc().killpg(123, signal.SIGINT)
