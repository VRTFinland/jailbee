"""Container service runtime behavior without real tmux or shell processes."""

import json
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from jailbee.services.local_proc import LocalProc, ProbeResult
from jailbee.services.runtime import ServiceRuntime, compose_detail
from tests.conftest import make_config


class FakeProc(LocalProc):
    def __init__(self):
        self.windows = {}
        self.commands = []
        self.clock = 0.0
        self.session = True
        self.probes = []
        self.health_calls = []
        self.health_result = ProbeResult(0, "", "", False)
        self.signals = []
        self.shells = []
        self.immediate_exit = set()
        self.global_remain = "off"
        self.window_remain = set()
        self.group_signals = []
        self.query_delay = 0
        self.query_count = 0
        self.creation_env = {}
        self.run_env = {}

    def tmux(self, args):
        args = list(args)
        self.commands.append(args)
        command = args[0]
        output = ""
        code = 0
        if command == "has-session":
            code = 0 if self.session else 1
        elif command == "new-session":
            self.session = True
        elif command == "show-options":
            output = self.global_remain + "\n"
        elif command == "set-option" and "-g" in args:
            self.global_remain = args[-1]
        elif command == "set-option" and "-w" in args:
            self.window_remain.add(args[args.index("-t") + 1].split("=", 1)[-1])
        elif command == "list-windows":
            self.query_count += 1
            if self.query_count == 2:
                self.clock += self.query_delay
            output = "".join(
                f"{name}\t{dead}\t{status}\t{pid}\n"
                for name, (dead, status, pid) in self.windows.items()
            )
        elif command == "new-window":
            name = args[args.index("-n") + 1]
            self.windows[name] = (0, "", 123)
            self.creation_env[name] = dict(
                args[i + 1].split("=", 1) for i in range(len(args) - 1) if args[i] == "-e"
            )
        elif command == "respawn-pane":
            name = args[args.index("-t") + 1].split("=", 1)[-1]
            self.run_env[name] = dict(
                args[i + 1].split("=", 1) for i in range(len(args) - 1) if args[i] == "-e"
            )
            if name in self.immediate_exit:
                if name in self.window_remain:
                    self.windows[name] = (1, 42, 123)
                else:
                    self.windows.pop(name, None)
        elif command == "kill-window":
            self.windows.pop(args[args.index("-t") + 1].split("=", 1)[-1], None)
        elif command == "capture-pane":
            output = "failure output\n"
        return subprocess.CompletedProcess(args, code, output, "")

    def run_shell(self, cmd, *, cwd, env, timeout, capture):
        self.shells.append((cmd, cwd, env, timeout, capture))
        return self.probes.pop(0) if self.probes else 1

    def run_probe(self, cmd, *, cwd, env, timeout):
        self.health_calls.append((cmd, cwd, dict(env), timeout))
        return self.health_result

    def killpg(self, pid, sig):
        self.signals.append((pid, sig))

    def service_command(self, command, identity, args=()):
        self.last_args = getattr(self, "last_args", {})
        self.last_args[identity.name] = tuple(args)
        identity.parent.mkdir(parents=True, exist_ok=True)
        identity.write_text("anchor")
        return command

    def signal_identity(self, identity, sig):
        if identity.exists():
            self.group_signals.append((1123, sig))
            return True
        return False

    def service_pgid(self, pid):
        return pid + 1000

    def signal_group(self, pgid, sig):
        self.group_signals.append((pgid, sig))

    def pid_alive(self, pid):
        return False

    def sleep(self, s):
        self.clock += s

    def now(self):
        return self.clock


def runtime(tmp_path, services):
    proc = FakeProc()
    cfg = make_config(tmp_path, services=services)
    return ServiceRuntime(cfg, tmp_path, proc, tmp_path / "state"), proc


def test_health_failure_is_derived_and_cached(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "serve", "health": "check", "env": {"X": "y"}}})
    proc.windows["svc-web"] = (0, "", 123)
    rt._save("web", "ready")
    proc.health_result = ProbeResult(2, "stdout", "first\nreason\n", False)
    status = rt.status()[0]
    assert status.state == "unhealthy"
    assert status.detail == "health check failed (exit 2): reason"
    assert status.since and "T" in status.since
    assert json.loads((rt.state_dir / "state.json").read_text())["web"]["state"] == "ready"
    assert rt.status()[0] == status
    assert proc.health_calls == [("check", str(tmp_path), {"X": "y"}, 5)]


def test_health_timeout_fallback_and_probe_free_states(tmp_path):
    rt, proc = runtime(
        tmp_path,
        {
            "web": {"run": "web", "ready": "ready-check", "health_timeout": 7},
            "other": {"run": "other"},
            "starting": {"run": "s", "health": "s-check"},
            "failed": {"run": "f", "health": "f-check"},
        },
    )
    for name in rt.cfg.services:
        proc.windows["svc-" + name] = (0, "", 1)
    rt._save("starting", "starting")
    rt._save("failed", "failed")
    proc.health_result = ProbeResult(None, "", "", True)
    assert rt.status(probe=False)[0].state == "ready"
    assert not proc.health_calls
    statuses = rt.status()
    assert statuses[0].detail == "health check timed out after 7s"
    assert [s.state for s in statuses] == ["unhealthy", "ready", "starting", "failed"]
    assert [call[0] for call in proc.health_calls] == ["ready-check"]


def test_plain_start_ready_service_is_noop(tmp_path):
    rt, proc = runtime(
        tmp_path,
        {
            "web": {"run": "web", "health": "web-check"},
            "other": {"run": "other", "health": "other-check"},
        },
    )
    proc.windows.update({"svc-web": (0, "", 1), "svc-other": (0, "", 2)})
    proc.health_result = ProbeResult(1, "", "bad", False)
    assert rt.start(["web"]).done == ()
    assert not proc.health_calls


def test_parallel_probes_and_shared_cache_lock(tmp_path):
    rt, proc = runtime(
        tmp_path,
        {
            "a": {"run": "a", "health": "a-check"},
            "b": {"run": "b", "health": "b-check"},
        },
    )
    proc.windows.update({"svc-a": (0, "", 1), "svc-b": (0, "", 2)})
    original = proc.run_probe

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return original(*args, **kwargs)

    proc.run_probe = slow
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: rt.status(), range(2)))
    assert time.monotonic() - start < 0.55
    assert all(s.state == "ready" for result in results for s in result)
    assert sorted(call[0] for call in proc.health_calls) == ["a-check", "b-check"]


@pytest.mark.parametrize("fresh", [False, True])
def test_probe_lock_wait_is_bounded_without_overwriting_writer(tmp_path, fresh):
    rt, proc = runtime(tmp_path, {"a": {"run": "a", "health": "check", "health_timeout": 1}})
    proc.windows["svc-a"] = (0, "", 1)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocked(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return ProbeResult(0, "", "", False)

    proc.run_probe = blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(rt.status, fresh=True)
        assert entered.wait(2)
        try:
            start = time.monotonic()
            result = pool.submit(rt.status, fresh=fresh).result(timeout=3)[0]
            assert time.monotonic() - start < 2.5
            assert result.state == "unhealthy"
            assert "lock timed out" in result.detail
            assert result.since is not None
        finally:
            release.set()
        assert writer.result(timeout=3)[0].state == "ready"
    assert rt.status()[0].state == "ready"
    assert len(calls) == 1


def test_probe_launch_error_is_isolated_from_other_services(tmp_path):
    rt, proc = runtime(
        tmp_path,
        {
            "bad": {"run": "bad", "health": "bad-check"},
            "good": {"run": "good", "health": "good-check"},
        },
    )
    proc.windows.update({"svc-bad": (0, "", 1), "svc-good": (0, "", 2)})

    def probe(cmd, **kwargs):
        if cmd == "bad-check":
            raise OSError("missing directory")
        return ProbeResult(0, "", "", False)

    proc.run_probe = probe
    statuses = rt.status()
    assert [item.state for item in statuses] == ["unhealthy", "ready"]
    assert "missing directory" in statuses[0].detail
    assert rt.wait(["good"], "ready", timeout=1, interval=1)[0] == 0


def test_wait_any_ready_prefers_later_match_over_earlier_failure(tmp_path):
    rt, proc = runtime(tmp_path, {"a": {"run": "a"}, "b": {"run": "b"}})
    proc.windows.update({"svc-a": (0, "", 1), "svc-b": (0, "", 2)})
    rt._save("a", "failed")
    code, item = rt.wait(["a", "b"], "ready", timeout=1, interval=1)
    assert code == 0 and item.name == "b"


def test_concurrent_success_does_not_erase_failed_status_since(tmp_path):
    rt, proc = runtime(tmp_path, {"a": {"run": "a", "health": "check"}})
    proc.windows["svc-a"] = (0, "", 1)
    rt._save("a", "ready")
    launch = rt._state()["a"]["since"]
    failed = threading.Event()
    resume = threading.Event()
    original = rt._probe_launch
    calls = iter([ProbeResult(1, "", "bad", False), ProbeResult(0, "", "", False)])
    proc.run_probe = lambda *args, **kwargs: next(calls)

    def pause_after_failure(*args, **kwargs):
        result = original(*args, **kwargs)
        if threading.current_thread().name == "failure":
            failed.set()
            assert resume.wait(3)
        return result

    rt._probe_launch = pause_after_failure
    statuses = []
    # Call _health directly so the controlled thread constructs the status itself.
    item = rt.status(probe=False)[0]
    thread = threading.Thread(
        target=lambda: statuses.append(rt._health(item, fresh=True)), name="failure"
    )
    thread.start()
    assert failed.wait(3)
    try:
        assert rt._health(item, fresh=True).state == "ready"
    finally:
        resume.set()
        thread.join(3)
    assert not thread.is_alive()
    assert statuses[0].state == "unhealthy"
    assert statuses[0].since != launch
    assert statuses[0].since is not None


def test_probe_snapshot_uses_original_launch(tmp_path):
    rt, proc = runtime(tmp_path, {"a": {"run": "a", "health": "check"}})
    proc.windows["svc-a"] = (0, "", 1)
    rt._save("a", "ready")
    old = rt._state()["a"]["since"]

    def restarting(*args, **kwargs):
        rt._save("a", "ready")
        return ProbeResult(1, "", "old", False)

    proc.run_probe = restarting
    assert rt.status()[0].state == "unhealthy"
    new = rt._state()["a"]["since"]
    assert old != new
    from jailbee.services.probe_cache import ProbeCache

    cache = ProbeCache(rt.state_dir / "probes.json")
    assert cache.get("a", "health", old) is not None
    assert cache.get("a", "health", new) is None


def test_compose_detail():
    assert compose_detail("running", "ok", "launch") == "running · ok · launch"
    assert compose_detail("", None, "launch") == "launch"


def test_status_ground_truth(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "serve"}})
    assert rt.status()[0].state == "stopped"
    proc.windows["svc-web"] = (1, 17, 123)
    status = rt.status()[0]
    assert status.state == "exited" and "17" in status.detail
    proc.windows.clear()
    rt.state_dir.mkdir()
    (rt.state_dir / "state.json").write_text('{"web":{"state":"ready"}}')
    assert rt.status()[0].state == "stopped"


def test_state_save_uses_atomic_replace(tmp_path, monkeypatch):
    import os

    rt, _ = runtime(tmp_path, {"web": {"run": "serve"}})
    replace = os.replace
    calls = []

    def record(source, destination):
        calls.append((source, destination))
        replace(source, destination)

    monkeypatch.setattr(os, "replace", record)
    rt._save("web", "ready")
    assert len(calls) == 1 and calls[0][1] == rt.state_dir / "state.json"
    assert json.loads(calls[0][1].read_text())["web"]["state"] == "ready"


def test_start_window_and_session(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "serve", "working_dir": "app", "env": {"K": "V"}}})
    proc.session = False
    assert rt.start(["web"]).ok
    window = next(c for c in proc.commands if c[0] == "new-window")
    for item in (
        "-d",
        "autostart:",
        "svc-web",
        str(tmp_path / "app"),
        "K=V",
    ):
        assert item in window
    assert any(c[:3] == ["new-session", "-d", "-s"] for c in proc.commands)
    assert any(
        c[0] == "set-option" and "autostart:=svc-web" in c and c[-2:] == ["remain-on-exit", "on"]
        for c in proc.commands
    )
    data = json.loads((rt.state_dir / "state.json").read_text())
    assert data["web"]["state"] == "ready" and "T" in data["web"]["since"]


def test_respawn_service_receives_configured_environment(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "serve", "env": {"K": "V", "TOKEN": "a=b"}}})
    assert rt.start(["web"]).ok
    assert proc.creation_env["svc-web"] == {"K": "V", "TOKEN": "a=b"}
    assert proc.run_env["svc-web"] == {"K": "V", "TOKEN": "a=b"}


def test_start_dependencies_skip_ready(tmp_path):
    rt, proc = runtime(
        tmp_path, {"db": {"run": "db"}, "web": {"run": "web", "depends_on": {"db": {}}}}
    )
    proc.windows["svc-db"] = (0, "", 123)
    assert rt.start(["web"]).done == ("web",)
    assert [c[c.index("-n") + 1] for c in proc.commands if c[0] == "new-window"] == ["svc-web"]


def test_ready_poll_and_timeout(tmp_path):
    rt, proc = runtime(
        tmp_path,
        {
            "db": {"run": "db", "ready": "probe", "ready_timeout": 3},
            "web": {"run": "web", "depends_on": {"db": {}}},
        },
    )
    proc.probes = [1, 1, 0]
    assert rt.start(["web"]).ok
    assert [s[3] for s in proc.shells] == [3, 2, 1]
    rt.stop(["db", "web"])
    proc.shells.clear()
    result = rt.start(["web"])
    assert not result.ok and result.failed == "db" and result.tail == "failure output\n"
    assert "svc-web" not in proc.windows
    assert all(0 < s[3] <= 3 for s in proc.shells)


def test_immediate_exit_preserved_and_failed(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "exit 42"}})
    proc.immediate_exit.add("svc-web")
    result = rt.start(["web"])
    assert not result.ok and result.failed == "web" and result.tail == "failure output\n"
    assert not any(c[0] == "set-option" and "-g" in c for c in proc.commands)
    set_index = next(i for i, c in enumerate(proc.commands) if c[0] == "set-option")
    spawn_index = next(i for i, c in enumerate(proc.commands) if c[0] == "respawn-pane")
    assert set_index < spawn_index
    assert proc.commands[spawn_index][-3:] == ["bash", "-lc", "exit 42"]
    assert proc.global_remain == "off"


def test_stop_command_and_signal(tmp_path):
    rt, proc = runtime(
        tmp_path,
        {
            "a": {"run": "a", "stop": "stop", "stop_timeout": 3},
            "b": {"run": "b", "stop_timeout": 2},
        },
    )
    proc.windows.update({"svc-a": (0, "", 1), "svc-b": (0, "", 2)})
    proc.service_command("", rt._identity("a"))
    proc.service_command("", rt._identity("b"))
    assert rt.stop(["a", "b"]).ok
    assert proc.shells[0][0] == "stop" and proc.shells[0][3] == 3
    assert (1123, signal.SIGINT) in proc.group_signals
    assert proc.clock == 5
    assert all(
        "autostart:=svc-" in c[c.index("-t") + 1] for c in proc.commands if c[0] == "kill-window"
    )


def test_restart_cascade_and_missing_prerequisite(tmp_path):
    rt, proc = runtime(
        tmp_path,
        {
            "db": {"run": "db"},
            "pricing": {"run": "price"},
            "backend": {"run": "backend", "depends_on": {"db": {"restart": True}, "pricing": {}}},
        },
    )
    proc.windows.update({"svc-db": (0, "", 1), "svc-backend": (0, "", 2)})
    assert rt.restart(["db"]).ok
    events = [
        (c[0], c[c.index("-t") + 1] if c[0] == "kill-window" else c[c.index("-n") + 1])
        for c in proc.commands
        if c[0] in ("kill-window", "new-window")
    ]
    assert events == [
        ("kill-window", "autostart:=svc-backend"),
        ("kill-window", "autostart:=svc-db"),
        ("new-window", "svc-db"),
        ("new-window", "svc-pricing"),
        ("new-window", "svc-backend"),
    ]


def test_lock_serializes_threads(tmp_path):
    rt, _ = runtime(tmp_path, {"a": {"run": "a"}})
    entered = threading.Event()

    def worker():
        with rt.locked():
            entered.set()

    with rt.locked():
        thread = threading.Thread(target=worker)
        thread.start()
        assert not entered.wait(0.2)
    thread.join(timeout=2)
    assert entered.is_set()


def test_pane_dies_during_readiness_poll(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "web", "ready": "probe"}})

    def probe(cmd, *, cwd, env, timeout, capture):
        proc.windows["svc-web"] = (1, 23, 123)
        return 1

    proc.run_shell = probe
    result = rt.start(["web"])
    assert not result.ok and result.failed == "web"
    assert result.tail == "failure output\n"
    assert "23" in rt.status()[0].detail


def test_hanging_stop_command_has_timeout(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "web", "stop": "stop", "stop_timeout": 4}})
    proc.windows["svc-web"] = (0, "", 123)

    def hanging(cmd, *, cwd, env, timeout, capture):
        assert timeout == 4
        proc.clock += timeout
        return 124

    proc.run_shell = hanging
    assert rt.stop(["web"]).ok
    assert proc.clock == 8
    assert "svc-web" not in proc.windows


def test_stop_kills_group_even_when_leader_disappears(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "web", "stop_timeout": 1}})
    proc.service_command("", rt._identity("web"))
    proc.windows["svc-web"] = (0, "", 123)
    assert rt.stop(["web"]).ok
    assert (1123, signal.SIGKILL) in proc.group_signals


def test_dead_pane_stop_and_replacement_cleanup_recorded_group(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "web", "stop_timeout": 1}})
    assert rt.start(["web"]).ok
    proc.windows["svc-web"] = (1, 0, 123)
    proc.group_signals.clear()
    assert rt.stop(["web"]).ok
    assert (1123, signal.SIGKILL) in proc.group_signals
    assert rt.start(["web"]).ok
    proc.windows["svc-web"] = (1, 0, 123)
    proc.group_signals.clear()
    assert rt.start(["web"]).ok
    assert (1123, signal.SIGKILL) in proc.group_signals


def test_stop_query_crosses_deadline(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "web", "stop_timeout": 1}})
    proc.windows["svc-web"] = (0, "", 123)
    proc.query_delay = 1.1

    def checked_sleep(seconds):
        assert seconds >= 0
        proc.clock += seconds

    proc.sleep = checked_sleep
    assert rt.stop(["web"]).ok
    assert "svc-web" not in proc.windows


def test_unknown_names_do_not_mutate(tmp_path):
    rt, proc = runtime(tmp_path, {"a": {"run": "a"}})
    with pytest.raises(ValueError):
        rt.stop(["a", "missing"])
    assert not proc.commands


def test_logs_limits_output_and_reports_capture_errors(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "web"}})
    proc.tmux = lambda args: subprocess.CompletedProcess(args, 0, "one\ntwo\nthree\n", "")
    assert rt.logs("web", 2) == "two\nthree\n"
    proc.tmux = lambda args: subprocess.CompletedProcess(args, 1, "", "pane missing")
    with pytest.raises(RuntimeError, match="pane missing"):
        rt.logs("web", 2)


def test_logs_trim_trailing_blank_pane_rows_before_slicing(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "web"}})
    pane = "one\ntwo\nthree\n" + "\n" * 20
    proc.tmux = lambda args: subprocess.CompletedProcess(args, 0, pane, "")
    assert rt.logs("web", 2) == "two\nthree\n"
    # whitespace-only rows below the output are padding too; inner blanks stay
    pane = "one\n\nthree\n   \n\n\n"
    proc.tmux = lambda args: subprocess.CompletedProcess(args, 0, pane, "")
    assert rt.logs("web", 5) == "one\n\nthree\n"
    proc.tmux = lambda args: subprocess.CompletedProcess(args, 0, "\n\n\n", "")
    assert rt.logs("web", 5) == ""


def test_logs_keep_the_dead_pane_line_visible(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "web"}})
    pane = "boom\nPane is dead (status 1, Tue Nov  3 10:00:00 2026)\n" + "\n" * 20
    proc.tmux = lambda args: subprocess.CompletedProcess(args, 0, pane, "")
    assert rt.logs("web", 2) == "boom\nPane is dead (status 1, Tue Nov  3 10:00:00 2026)\n"


def test_failed_start_tail_skips_trailing_blank_pane_rows(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "exit 1"}})
    proc.immediate_exit.add("svc-web")
    original = proc.tmux

    def capture(args):
        if args[0] == "capture-pane":
            return subprocess.CompletedProcess(args, 0, "err1\nerr2\n" + "\n" * 40, "")
        return original(args)

    proc.tmux = capture
    report = rt.start(["web"])
    assert report.tail.splitlines() == ["err1", "err2"]


def test_failed_start_tail_is_limited_and_capture_error_reported(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "exit 1"}})
    proc.immediate_exit.add("svc-web")
    original = proc.tmux

    def capture(args):
        if args[0] == "capture-pane":
            return subprocess.CompletedProcess(args, 0, "".join(f"{i}\n" for i in range(40)), "")
        return original(args)

    proc.tmux = capture
    report = rt.start(["web"])
    assert report.tail.splitlines() == [str(i) for i in range(10, 40)]

    def error(args):
        if args[0] == "capture-pane":
            return subprocess.CompletedProcess(args, 1, "", "window missing")
        return original(args)

    proc.tmux = error
    report = rt.start(["web"])
    assert not report.ok and "window missing" in report.tail


def test_follow_snapshot_boundary_and_saturated_history(tmp_path, monkeypatch):
    from io import BytesIO

    from jailbee.services.local_proc import LocalProc

    # Output before the capture block belongs to the snapshot; only subsequent
    # notifications are streamed. Repeated lines do not depend on history size.
    protocol = (
        b"%begin 1 1 0\n%end 1 1 0\n"
        b"%output %7 NEW\134012\n"
        b"%begin 1 2 0\nOLD\nNEW\n%end 1 2 0\n"
        b"%output %7 same\134012same\134012\n"
        b"%output %8 unrelated\134012\n%exit\n"
    )

    class Process:
        def __init__(self):
            self.stdin = BytesIO()
            self.stdout = BytesIO(protocol)
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return 0

    process = Process()
    monkeypatch.setattr("jailbee.services.local_proc.subprocess.Popen", lambda *a, **kw: process)
    proc = LocalProc()
    monkeypatch.setattr(proc, "tmux", lambda args: subprocess.CompletedProcess(args, 0, "%7\n", ""))
    assert list(proc.follow_pane("autostart:=svc-db", 1)) == ["NEW\n", "same\nsame\n"]
    assert process.returncode == 0


def test_follow_snapshot_preserves_protocol_looking_lines(monkeypatch):
    from io import BytesIO

    protocol = (
        b"%begin 1 1 0\n%end 1 1 0\n"
        b"%begin 1 2 0\n%exit ordinary\n%error 1 9 0\n"
        b"%end 1 8 0\n%begin 1 7 0\nlast\n%end 1 2 0\n"
        b"%output %7 ok\134012\n%exit\n"
    )

    class Process:
        def __init__(self):
            self.stdin = BytesIO()
            self.stdout = BytesIO(protocol)

        def poll(self):
            return 0

        def wait(self):
            return 0

    process = Process()
    monkeypatch.setattr("jailbee.services.local_proc.subprocess.Popen", lambda *a, **kw: process)
    proc = LocalProc()
    monkeypatch.setattr(proc, "tmux", lambda args: subprocess.CompletedProcess(args, 0, "%7\n", ""))
    assert list(proc.follow_pane("autostart:=svc-db", 30)) == [
        "%exit ordinary\n%error 1 9 0\n%end 1 8 0\n%begin 1 7 0\nlast\n",
        "ok\n",
    ]


def test_wait_matches_and_validates_before_status(tmp_path, monkeypatch):
    rt, proc = runtime(tmp_path, {"web": {"run": "web"}, "db": {"run": "db"}})
    proc.windows["svc-db"] = (1, 1, 123)
    code, result = rt.wait(["web", "db"], "unhealthy", timeout=2, interval=1)
    assert code == 0 and result.name == "web" and result.state == "stopped"
    code, result = rt.wait(["db"], "ready", timeout=2, interval=1)
    assert code == 3 and result.state == "exited"
    monkeypatch.setattr(rt, "status", lambda **kwargs: pytest.fail("must validate first"))
    for names, until, timeout, interval in [
        ([], "ready", 0, 1),
        (["missing"], "ready", 0, 1),
        (["web"], "bad", 0, 1),
        (["web"], "ready", -1, 1),
        (["web"], "ready", 0, 0),
    ]:
        assert rt.wait(names, until, timeout=timeout, interval=interval) == (2, None)


def test_wait_timeout_and_unlimited_fresh_poll(tmp_path, monkeypatch):
    rt, proc = runtime(tmp_path, {"web": {"run": "web"}})
    proc.windows["svc-web"] = (0, "", 1)
    assert rt.wait(["web"], "failed", timeout=2, interval=1) == (1, None)
    assert proc.clock == 2
    original = rt.status
    calls = []

    def status(*, fresh):
        calls.append(fresh)
        if len(calls) == 3:
            proc.windows["svc-web"] = (1, 1, 1)
        return original(fresh=fresh)

    monkeypatch.setattr(rt, "status", status)
    code, result = rt.wait(["web"], "exited", timeout=0, interval=1)
    assert code == 0 and result.state == "exited" and calls == [True, True, True]


def test_wait_ready_and_unhealthy_probe(tmp_path):
    rt, proc = runtime(tmp_path, {"web": {"run": "web", "health": "check"}})
    proc.windows["svc-web"] = (0, "", 1)
    code, item = rt.wait(["web"], "ready", timeout=1, interval=1)
    assert code == 0 and item.state == "ready"
    proc.health_result = ProbeResult(1, "", "bad", False)
    code, item = rt.wait(["web"], "unhealthy", timeout=1, interval=1)
    assert code == 0 and item.state == "unhealthy"
    assert len(proc.health_calls) == 2


def test_one_off_args_do_not_survive_plain_restart(tmp_path):
    from jailbee.services.launch_store import LaunchRequest
    rt, proc = runtime(tmp_path, {"web": {"run": "serve", "args": True}})
    assert rt.start(["web"], launch=LaunchRequest(("one",), {"TOKEN": "secret-one"}, False, False)).ok
    assert proc.last_args["anchor-web.json"] == ("one",)
    assert rt.restart(["web"]).ok
    assert proc.last_args["anchor-web.json"] == ()
    assert "TOKEN" not in proc.run_env["svc-web"]


def test_persisted_args_survive_restart_and_store_reload(tmp_path):
    from jailbee.services.launch_store import LaunchRequest
    rt, proc = runtime(tmp_path, {"web": {"run": "serve", "args": True}})
    assert rt.start(["web"], launch=LaunchRequest(("saved",), {"TOKEN": "secret-two"}, True, False)).ok
    rt = ServiceRuntime(rt.cfg, tmp_path, proc, rt.state_dir)
    assert rt.restart(["web"]).ok
    assert proc.last_args["anchor-web.json"] == ("saved",)
    assert proc.run_env["svc-web"]["TOKEN"] == "secret-two"


def test_launch_env_reaches_ready_and_stop_probes(tmp_path):
    from jailbee.services.launch_store import LaunchRequest
    rt, proc = runtime(tmp_path, {"web": {"run": "serve", "args": True, "ready": "ready", "stop": "stop", "env": {"BASE": "yes"}}})
    proc.probes = [0]
    assert rt.start(["web"], launch=LaunchRequest(("a b",), {"TOKEN": "secret-three"}, False, False)).ok
    assert rt.stop(["web"]).ok
    assert [s[2] for s in proc.shells] == [{"BASE": "yes", "TOKEN": "secret-three", "JAILBEE_SERVICE_ARGS": "'a b'"}] * 2


def test_cascade_dependents_use_their_own_launch(tmp_path):
    from jailbee.services.launch_store import Launch, LaunchRequest, LaunchStore
    rt, proc = runtime(tmp_path, {"db": {"run": "db", "args": True}, "web": {"run": "web", "args": True, "depends_on": {"db": {"restart": True}}}})
    LaunchStore(rt.state_dir / "launch.json").set_persisted("web", Launch(("web-arg",), {"WEB": "value"}))
    assert rt.restart(["db"], launch=LaunchRequest(("db-arg",), {}, False, False)).ok
    assert proc.last_args["anchor-db.json"] == ("db-arg",)
    assert proc.last_args["anchor-web.json"] == ("web-arg",)
    assert proc.run_env["svc-web"]["WEB"] == "value"


def test_start_ready_service_with_options_restarts_it(tmp_path):
    from jailbee.services.launch_store import LaunchRequest
    rt, proc = runtime(tmp_path, {"web": {"run": "web", "args": True}})
    assert rt.start(["web"]).ok
    assert rt.start(["web"], launch=LaunchRequest(("new",), {}, False, False)).done == ("web",)
    assert proc.last_args["anchor-web.json"] == ("new",)


def test_status_launch_field_and_detail(tmp_path):
    from jailbee.services.launch_store import Launch, LaunchRequest, LaunchStore
    rt, proc = runtime(tmp_path, {"web": {"run": "web", "args": True, "health": "check"}})
    LaunchStore(rt.state_dir / "launch.json").set_persisted("web", Launch(("saved",), {"SAVED": "saved-secret"}))
    assert rt.start(["web"], launch=LaunchRequest(("a b",), {"TOKEN": "secret-four"}, False, False)).ok
    proc.health_result = ProbeResult(1, "secret-four", "saved-secret", False)
    status = rt.status()[0]
    assert status.launch == {"args": ["a b"], "env_keys": ["SAVED", "TOKEN"], "persisted_args": ["saved"], "persisted_env_keys": ["SAVED"]}
    assert "args: 'a b' (one-off)" in status.detail
    assert "persisted args: saved" in status.detail
    assert "env: SAVED,TOKEN" in status.detail
    assert "secret-four" not in str(status) and "saved-secret" not in str(status)
    assert rt.probe("web", "health", "check", 5).stderr == "***"


def test_failed_start_redacts_launch_secrets(tmp_path):
    from jailbee.services.launch_store import LaunchRequest
    rt, proc = runtime(tmp_path, {"web": {"run": "web"}})
    proc.immediate_exit.add("svc-web")
    original = proc.tmux
    def capture(args):
        if args[0] == "capture-pane":
            return subprocess.CompletedProcess(args, 0, "secret-five\n", "")
        return original(args)
    proc.tmux = capture
    report = rt.start(["web"], launch=LaunchRequest(None, {"TOKEN": "secret-five"}, False, False))
    assert report.tail == "***\n"


def test_invalid_requests_do_not_change_lifecycle(tmp_path):
    from jailbee.services.launch_store import LaunchRequest
    rt, proc = runtime(tmp_path, {"a": {"run": "a"}, "b": {"run": "b"}})
    for names, request in [(["a", "b"], LaunchRequest(None, {}, False, False)), (["a"], LaunchRequest(("x",), {}, False, False)), (["a"], LaunchRequest(None, {"BAD-KEY": "secret-value"}, False, False))]:
        with pytest.raises(ValueError) as error:
            rt.restart(names, launch=request)
        assert "secret-value" not in str(error.value)
    assert not proc.commands


def test_redact_only_launch_values_of_four_or_more(tmp_path):
    from jailbee.services.launch_store import Launch, LaunchStore
    rt, _ = runtime(tmp_path, {"a": {"run": "a", "env": {"CONFIG": "configured"}}})
    store = LaunchStore(rt.state_dir / "launch.json")
    store.set_persisted("a", Launch((), {"S": "persistent", "SHORT": "abc"}))
    store.set_current("a", Launch((), {"X": "current-secret"}), False)
    assert rt.redact("a", "persistent current-secret abc configured") == "*** *** abc configured"


def test_start_stopped_with_options_does_not_start_cascade_dependents(tmp_path):
    from jailbee.services.launch_store import LaunchRequest
    rt, proc = runtime(tmp_path, {"db": {"run": "db", "args": True}, "web": {"run": "web", "depends_on": {"db": {"restart": True}}}})
    result = rt.start(["db"], launch=LaunchRequest(("arg",), {}, False, False))
    assert result.done == ("db",)
    assert "svc-web" not in proc.windows


def test_status_redacts_secrets_in_current_and_persisted_args(tmp_path):
    from jailbee.services.launch_store import Launch, LaunchRequest, LaunchStore
    rt, _ = runtime(tmp_path, {"web": {"run": "web", "args": True}})
    LaunchStore(rt.state_dir / "launch.json").set_persisted("web", Launch(("saved-secret",), {"SAVED": "saved-secret"}))
    assert rt.start(["web"], launch=LaunchRequest(("current-secret", "prefix-saved-secret"), {"TOKEN": "current-secret"}, False, False)).ok
    status = rt.status(probe=False)[0]
    assert status.launch["args"] == ["***", "prefix-***"]
    assert status.launch["persisted_args"] == ["***"]
    rendered = json.dumps(status.launch)
    assert "saved-secret" not in rendered and "current-secret" not in rendered
