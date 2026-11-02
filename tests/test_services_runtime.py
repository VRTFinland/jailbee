"""Container service runtime behavior without real tmux or shell processes."""

import json
import signal
import subprocess
import threading

import pytest

from jailbee.services.local_proc import LocalProc
from jailbee.services.runtime import ServiceRuntime
from tests.conftest import make_config


class FakeProc(LocalProc):
    def __init__(self):
        self.windows = {}
        self.commands = []
        self.clock = 0.0
        self.session = True
        self.probes = []
        self.signals = []
        self.shells = []
        self.immediate_exit = set()
        self.global_remain = "off"
        self.window_remain = set()
        self.group_signals = []
        self.query_delay = 0
        self.query_count = 0

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
        elif command == "respawn-pane":
            name = args[args.index("-t") + 1].split("=", 1)[-1]
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

    def killpg(self, pid, sig):
        self.signals.append((pid, sig))

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
    assert rt.stop(["a", "b"]).ok
    assert proc.shells[0][0] == "stop" and proc.shells[0][3] == 3
    assert (1002, signal.SIGINT) in proc.group_signals
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
    proc.windows["svc-web"] = (0, "", 123)
    assert rt.stop(["web"]).ok
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
