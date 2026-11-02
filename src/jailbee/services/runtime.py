"""Container-side lifecycle for repository service windows."""

import fcntl
import json
import os
import signal
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jailbee.config import Config
from jailbee.config.models_services import Service
from jailbee.services.graph import restart_set, topo_order, with_dependencies
from jailbee.services.local_proc import LocalProc
from jailbee.tmux import SESSION_NAME

WINDOW_PREFIX = "svc-"
READY_GRACE_S = 2
READY_POLL_S = 1.0
State = Literal["stopped", "starting", "ready", "failed", "exited"]


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    state: State
    window: str | None
    since: str | None
    detail: str


@dataclass(frozen=True)
class RunReport:
    ok: bool
    done: tuple[str, ...]
    failed: str | None
    tail: str


@dataclass(frozen=True)
class _Pane:
    dead: bool
    status: str
    pid: int


class ServiceRuntime:
    def __init__(self, cfg: Config, repo_dir: Path, proc: LocalProc, state_dir: Path):
        self.cfg = cfg
        self.repo_dir = repo_dir
        self.proc = proc
        self.state_dir = state_dir

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _state(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads((self.state_dir / "state.json").read_text())
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, name: str, state: State, detail: str = "") -> None:
        data = self._state()
        data[name] = {"state": state, "since": datetime.now(UTC).isoformat(), "detail": detail}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=self.state_dir, prefix=".state-")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream)
            os.replace(path, self.state_dir / "state.json")
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def _panes(self) -> dict[str, _Pane]:
        result = self.proc.tmux(
            [
                "list-windows",
                "-t",
                SESSION_NAME,
                "-F",
                "#{window_name}\t#{pane_dead}\t#{pane_dead_status}\t#{pane_pid}",
            ]
        )
        panes: dict[str, _Pane] = {}
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) == 4:
                try:
                    panes[fields[0]] = _Pane(fields[1] == "1", fields[2], int(fields[3]))
                except ValueError:
                    continue
        return panes

    def status(self) -> list[ServiceStatus]:
        panes = self._panes()
        states = self._state()
        result = []
        for name in self.cfg.services:
            window = WINDOW_PREFIX + name
            pane = panes.get(window)
            saved = states.get(name, {})
            if pane is None:
                result.append(ServiceStatus(name, "stopped", None, None, ""))
            elif pane.dead:
                result.append(
                    ServiceStatus(
                        name,
                        "exited",
                        window,
                        saved.get("since"),
                        f"exited with status {pane.status}",
                    )
                )
            else:
                raw = saved.get("state", "ready")
                state: State = raw if raw in ("starting", "ready", "failed") else "ready"  # type: ignore[assignment]  # narrowed by membership
                result.append(
                    ServiceStatus(name, state, window, saved.get("since"), saved.get("detail", ""))
                )
        return result

    def _target(self, name: str) -> str:
        return f"{SESSION_NAME}:={WINDOW_PREFIX}{name}"

    def logs(self, name: str, lines: int) -> str:
        topo_order(self.cfg.services, [name])
        return self.proc.tmux(
            ["capture-pane", "-p", "-t", self._target(name), "-S", f"-{lines}"]
        ).stdout

    def _shell(self, service: Service, command: str, timeout: float, *, capture: bool) -> int:
        return self.proc.run_shell(
            command,
            cwd=str(self.repo_dir / service.working_dir),
            env=service.env,
            timeout=timeout,
            capture=capture,
        )

    def _start_one(self, name: str) -> bool:
        service = self.cfg.services[name]
        target = self._target(name)
        existing = self._panes().get(WINDOW_PREFIX + name)
        if existing is not None:
            self.proc.tmux(["kill-window", "-t", target])
        if self.proc.tmux(["has-session", "-t", SESSION_NAME]).returncode:
            result = self.proc.tmux(
                ["new-session", "-d", "-s", SESSION_NAME, "-x", "200", "-y", "50"]
            )
            if result.returncode:
                self._save(name, "failed", result.stderr)
                return False
        self._save(name, "starting")
        args = [
            "new-window",
            "-d",
            "-t",
            f"{SESSION_NAME}:",
            "-n",
            WINDOW_PREFIX + name,
            "-c",
            str(self.repo_dir / service.working_dir),
        ]
        for key, value in service.env.items():
            args.extend(["-e", f"{key}={value}"])
        # Keep the initial pane alive until its window-local option is set.
        args.extend(["sleep", "infinity"])
        created = self.proc.tmux(args)
        if created.returncode:
            self._save(name, "failed", created.stderr)
            return False
        configured = self.proc.tmux(["set-option", "-w", "-t", target, "remain-on-exit", "on"])
        if configured.returncode:
            self.proc.tmux(["kill-window", "-t", target])
            self._save(name, "failed", configured.stderr)
            return False
        respawn = ["respawn-pane", "-k", "-t", target]
        for key, value in service.env.items():
            respawn.extend(["-e", f"{key}={value}"])
        respawn.extend(["bash", "-lc", service.run])
        spawned = self.proc.tmux(respawn)
        if spawned.returncode:
            self._save(name, "failed", spawned.stderr)
            return False
        deadline = self.proc.now() + (service.ready_timeout if service.ready else READY_GRACE_S)
        while True:
            pane = self._panes().get(WINDOW_PREFIX + name)
            if pane is None or pane.dead:
                detail = f"exited with status {pane.status}" if pane else "window disappeared"
                self._save(name, "failed", detail)
                return False
            remaining = deadline - self.proc.now()
            if service.ready is None and remaining <= 0:
                self._save(name, "ready")
                return True
            if service.ready is not None:
                if remaining <= 0:
                    self._save(name, "failed", f"not ready after {service.ready_timeout}s")
                    return False
                if self._shell(service, service.ready, remaining, capture=True) == 0:
                    pane = self._panes().get(WINDOW_PREFIX + name)
                    if pane is not None and not pane.dead and self.proc.now() <= deadline:
                        self._save(name, "ready")
                        return True
                    continue
            remaining = deadline - self.proc.now()
            if remaining > 0:
                self.proc.sleep(min(READY_POLL_S, remaining))

    def _start_unlocked(self, names: Sequence[str]) -> RunReport:
        services = self.cfg.services
        order = topo_order(services, with_dependencies(services, names))
        done: list[str] = []
        for name in order:
            if next(s for s in self.status() if s.name == name).state == "ready":
                continue
            if not self._start_one(name):
                return RunReport(False, tuple(done), name, self.logs(name, 30))
            done.append(name)
        return RunReport(True, tuple(done), None, "")

    def start(self, names: Sequence[str]) -> RunReport:
        with self.locked():
            return self._start_unlocked(names)

    def _stop_unlocked(self, names: Sequence[str]) -> RunReport:
        order = topo_order(self.cfg.services, names)
        done: list[str] = []
        for name in reversed(order):
            pane = self._panes().get(WINDOW_PREFIX + name)
            if pane is None:
                continue
            service = self.cfg.services[name]
            pgid = self.proc.service_pgid(pane.pid) if not pane.dead else None
            if not pane.dead:
                if service.stop:
                    self._shell(service, service.stop, service.stop_timeout, capture=False)
                elif pgid is not None:
                    self.proc.signal_group(pgid, signal.SIGINT)
                deadline = self.proc.now() + service.stop_timeout
                while self.proc.now() < deadline:
                    current = self._panes().get(WINDOW_PREFIX + name)
                    if current is None or current.dead:
                        break
                    remaining = deadline - self.proc.now()
                    if remaining <= 0:
                        break
                    self.proc.sleep(min(READY_POLL_S, remaining))
            self.proc.tmux(["kill-window", "-t", self._target(name)])
            if pgid is not None:
                self.proc.signal_group(pgid, signal.SIGKILL)
            self._save(name, "stopped")
            done.append(name)
        return RunReport(True, tuple(done), None, "")

    def stop(self, names: Sequence[str]) -> RunReport:
        with self.locked():
            return self._stop_unlocked(names)

    def _restart_unlocked(self, names: Sequence[str]) -> RunReport:
        selected = restart_set(self.cfg.services, names)
        order = topo_order(self.cfg.services, selected)
        self._stop_unlocked(order)
        return self._start_unlocked(order)

    def restart(self, names: Sequence[str]) -> RunReport:
        with self.locked():
            return self._restart_unlocked(names)
