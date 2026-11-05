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


@pytest.fixture
def follow_control(monkeypatch):
    process = Mock()
    process.stdout.fileno.return_value = 42
    process.poll.return_value = None
    selector = Mock()
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr("selectors.DefaultSelector", Mock(return_value=selector))
    monkeypatch.setattr("os.set_blocking", Mock())
    read = Mock()
    monkeypatch.setattr("os.read", read)
    proc = LocalProc()
    monkeypatch.setattr(
        proc, "tmux", Mock(return_value=subprocess.CompletedProcess([], 0, "%1", ""))
    )
    return proc, process, selector, read


def test_follow_attach_callback_precedes_buffered_output(follow_control):
    proc, _process, selector, read = follow_control
    selector.select.return_value = [1]
    read.side_effect = [b"%end 1 2 3\n%output %1 hit\\012\n", b""]
    events = []
    stream = proc.follow_pane(
        "svc", 10, initial=False, poll_interval=0.1, on_attached=lambda: events.append("attached")
    )
    for chunk in stream:
        events.append(chunk)
    assert events == ["attached", "hit\n"]


def test_follow_poll_frames_ticks_and_decodes(follow_control):
    proc, process, selector, read = follow_control
    selector.select.side_effect = [[], [1], [1], [1], [1]]
    read.side_effect = [
        b"%end 1 2 3\n%output %1 caf\\303",
        b"\\251\\012\n%output %1 \\342\n%output %1 \\202\\254\n%out",
        b"put %1 done\n",
        b"",
    ]
    assert list(proc.follow_pane("svc", 10, initial=False, poll_interval=0.25)) == [
        "",
        "caf\u00e9\n",
        "\u20ac",
        "done",
    ]
    process.stdin.write.assert_not_called()
    selector.close.assert_called_once()
    process.stdout.close.assert_called_once()
    process.stdin.close.assert_called_once()
    process.wait.assert_called_with(timeout=0.1)


def test_follow_poll_ticks_during_continuous_unrelated_buffered_output(follow_control, monkeypatch):
    proc, _process, selector, read = follow_control
    clock = iter(i * 0.1 for i in range(100))
    monkeypatch.setattr(proc, "now", lambda: next(clock))
    selector.select.return_value = [1]
    read.side_effect = [b"%end 1 2 3\n" + b"%output %2 ignored\n" * 20, b""]
    stream = proc.follow_pane("svc", 10, initial=False, poll_interval=0.5)
    assert next(stream) == ""
    assert read.call_count == 1
    stream.close()


def test_follow_poll_slow_tick_consumer_does_not_starve_output(follow_control, monkeypatch):
    proc, _process, selector, read = follow_control
    clock = [0.0]
    monkeypatch.setattr(proc, "now", lambda: clock[0])
    selector.select.side_effect = [[], [1], [1]]
    read.side_effect = [b"%end 1 2 3\n%output %1 ready\n", b""]
    stream = proc.follow_pane("svc", 10, initial=False, poll_interval=0.5)
    assert next(stream) == ""
    clock[0] = 1.0
    assert next(stream) == "ready"
    stream.close()


def test_follow_poll_attach_error_cleans_up(follow_control):
    proc, process, selector, read = follow_control
    selector.select.return_value = [1]
    read.return_value = b"%error 1 2 3\n"
    with pytest.raises(RuntimeError, match="failed to attach"):
        list(proc.follow_pane("svc", 10, initial=False, poll_interval=0.1))
    selector.close.assert_called_once()
    process.terminate.assert_called_once()
    process.wait.assert_called_with(timeout=0.1)


def test_follow_poll_close_bounds_reap(follow_control):
    proc, process, selector, _read = follow_control
    selector.select.return_value = []
    process.wait.side_effect = [subprocess.TimeoutExpired("tmux", 0.1), 0]
    stream = proc.follow_pane("svc", 10, initial=False, poll_interval=0.1)
    assert next(stream) == ""
    stream.close()
    process.kill.assert_called_once()
    assert process.wait.call_count == 2
    selector.close.assert_called_once()


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


def test_run_probe_retains_maximal_tail_after_invalid_utf8_expansion(tmp_path):
    result = LocalProc().run_probe(
        "head -c 16383 /dev/zero | tr '\\0' a; printf '\\377useful-tail'",
        cwd=str(tmp_path),
        env={},
        timeout=5,
    )
    encoded = result.stdout.encode("utf-8")
    assert len(encoded) <= 16 * 1024
    assert len(encoded) >= 16 * 1024 - 32
    assert result.stdout.endswith("useful-tail")


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
    marker = tmp_path / "child-finished"
    result = LocalProc().run_probe(
        f"(sleep 2; touch {marker}) & printf out; printf err >&2; sleep 30",
        cwd=str(tmp_path),
        env={},
        timeout=0.1,
    )
    assert result.code is None
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.timed_out
    time.sleep(2.1)
    assert not marker.exists()


def test_run_probe_bounds_cleanup_when_wait_times_out(monkeypatch, tmp_path):
    process = Mock(pid=123, stdout=None, stderr=None, returncode=None)
    process.stdout = Mock()
    process.stderr = Mock()
    process.stdout.fileno.return_value = 10
    process.stderr.fileno.return_value = 11
    process.wait.side_effect = [
        subprocess.TimeoutExpired("bash", 0),
        subprocess.TimeoutExpired("bash", 0),
    ]
    process.poll.return_value = None
    process.kill = Mock()
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr("os.set_blocking", Mock())
    monkeypatch.setattr("os.close", Mock())
    monkeypatch.setattr("selectors.DefaultSelector", lambda: Mock(get_map=lambda: {}))
    result = LocalProc().run_probe("sleep 30", cwd=str(tmp_path), env={}, timeout=1)
    assert result.timed_out
    assert process.wait.call_count == 2
    assert all(call.kwargs.get("timeout") is not None for call in process.wait.call_args_list)


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
