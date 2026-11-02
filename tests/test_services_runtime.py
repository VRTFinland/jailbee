"""Container service runtime behavior without real tmux or shell processes."""

import json
import signal
import subprocess
import threading

import pytest

from tests.conftest import make_config
from jailbee.services.local_proc import LocalProc
from jailbee.services.runtime import ServiceRuntime


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
        elif command == "list-windows":
            output = "".join(f"{name}\t{dead}\t{status}\t{pid}\n" for name, (dead, status, pid) in self.windows.items())
        elif command == "new-window":
            name = args[args.index("-n") + 1]
            self.windows[name] = (1, 42, 123) if name in self.immediate_exit else (0, "", 123)
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
    for item in ("-d", "autostart:", "svc-web", str(tmp_path / "app"), "K=V", "bash", "-lc", "serve"):
        assert item in window
    assert any(c[:3] == ["new-session", "-d", "-s"] for c in proc.commands)
    assert any(c[0] == "set-option" and "autostart:=svc-web" in c and c[-2:] == ["remain-on-exit", "on"] for c in proc.commands)
    data = json.loads((rt.state_dir / "state.json").read_text())
    assert data["web"]["state"] == "ready" and "T" in data["web"]["since"]


def test_start_dependencies_skip_ready(tmp_path):
    rt, proc = runtime(tmp_path, {"db": {"run": "db"}, "web": {"run": "web", "depends_on": {"db": {}}}})
    proc.windows["svc-db"] = (0, "", 123)
    assert rt.start(["web"]).done == ("web",)
    assert [c[c.index("-n") + 1] for c in proc.commands if c[0] == "new-window"] == ["svc-web"]


def test_ready_poll_and_timeout(tmp_path):
    rt, proc = runtime(tmp_path, {"db": {"run": "db", "ready": "probe", "ready_timeout": 3}, "web": {"run": "web", "depends_on": {"db": {}}}})
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
    new_index = next(i for i, c in enumerate(proc.commands) if c[0] == "new-window")
    assert any(c[0] == "set-option" and "-g" in c and c[-2:] == ["remain-on-exit", "on"] for c in proc.commands[:new_index])
    assert proc.global_remain == "off"


def test_stop_command_and_signal(tmp_path):
    rt, proc = runtime(tmp_path, {"a": {"run": "a", "stop": "stop", "stop_timeout": 3}, "b": {"run": "b", "stop_timeout": 2}})
    proc.windows.update({"svc-a": (0, "", 1), "svc-b": (0, "", 2)})
    assert rt.stop(["a", "b"]).ok
    assert proc.shells[0][0] == "stop" and proc.shells[0][3] == 3
    assert (2, signal.SIGINT) in proc.signals
    assert proc.clock == 5
    assert all("autostart:=svc-" in c[c.index("-t") + 1] for c in proc.commands if c[0] == "kill-window")


def test_restart_cascade_and_missing_prerequisite(tmp_path):
    rt, proc = runtime(tmp_path, {"db": {"run": "db"}, "pricing": {"run": "price"}, "backend": {"run": "backend", "depends_on": {"db": {"restart": True}, "pricing": {}}}})
    proc.windows.update({"svc-db": (0, "", 1), "svc-backend": (0, "", 2)})
    assert rt.restart(["db"]).ok
    events = [(c[0], c[c.index("-t") + 1] if c[0] == "kill-window" else c[c.index("-n") + 1]) for c in proc.commands if c[0] in ("kill-window", "new-window")]
    assert events == [("kill-window", "autostart:=svc-backend"), ("kill-window", "autostart:=svc-db"), ("new-window", "svc-db"), ("new-window", "svc-pricing"), ("new-window", "svc-backend")]


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


def test_unknown_names_do_not_mutate(tmp_path):
    rt, proc = runtime(tmp_path, {"a": {"run": "a"}})
    with pytest.raises(ValueError):
        rt.stop(["a", "missing"])
    assert not proc.commands
