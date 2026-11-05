"""Container-side lifecycle for repository service windows."""

import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import signal
import tempfile
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jailbee.config import Config
from jailbee.config.models_services import Service
from jailbee.services.graph import restart_set, topo_order, with_dependencies
from jailbee.services.launch_store import Launch, LaunchRequest, LaunchStore, resolve_launch
from jailbee.services.local_proc import LocalProc, ProbeResult
from jailbee.services.probe_cache import ProbeCache
from jailbee.tmux import SESSION_NAME

WINDOW_PREFIX = "svc-"
READY_GRACE_S = 2
READY_POLL_S = 1.0
State = Literal["stopped", "starting", "ready", "failed", "exited", "unhealthy"]


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    state: State
    window: str | None
    since: str | None
    detail: str
    launch: dict[str, object] | None = None
    status: dict[str, object] | str | None = None


def compose_detail(lifecycle: str, status: str | None, launch: str | None) -> str:
    return " · ".join(part for part in (lifecycle, status, launch) if part)


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
        self.launch_store = LaunchStore(state_dir / "launch.json")

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

    def _lifecycle(
        self, name: str, panes: dict[str, _Pane], states: dict[str, dict[str, str]]
    ) -> ServiceStatus:
        window = WINDOW_PREFIX + name
        pane = panes.get(window)
        saved = states.get(name, {})
        if pane is None:
            return ServiceStatus(name, "stopped", None, None, "")
        if pane.dead:
            return ServiceStatus(
                name, "exited", window, saved.get("since"), f"exited with status {pane.status}"
            )
        raw = saved.get("state", "ready")
        state: State = raw if raw in ("starting", "ready", "failed") else "ready"  # type: ignore[assignment]  # narrowed by membership
        return ServiceStatus(name, state, window, saved.get("since"), saved.get("detail", ""))

    def launch_env(self, name: str) -> dict[str, str]:
        current = self.launch_store.current(name)
        launch = current[0] if current else Launch()
        env = {**self.cfg.services[name].env, **launch.env}
        if launch.args:
            env["JAILBEE_SERVICE_ARGS"] = shlex.join(launch.args)
        return env

    def redact(self, name: str, text: str) -> str:
        current = self.launch_store.current(name)
        values = list(self.launch_store.persisted(name).env.values())
        if current:
            values.extend(current[0].env.values())
        for value in sorted(set(values), key=len, reverse=True):
            if len(value) >= 4:
                text = text.replace(value, "***")
        return text

    def probe(
        self,
        name: str,
        kind: Literal["health", "status"],
        cmd: str,
        timeout: int,
        *,
        fresh: bool = False,
    ) -> ProbeResult:
        launch_id = self._state().get(name, {}).get("since", "")
        result, _ = self._probe_launch(name, kind, cmd, timeout, launch_id, fresh=fresh)
        return result

    def _probe_launch(
        self,
        name: str,
        kind: Literal["health", "status"],
        cmd: str,
        timeout: int,
        launch_id: str,
        *,
        fresh: bool,
    ) -> tuple[ProbeResult, float | None]:
        cache = ProbeCache(self.state_dir / "probes.json")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        key = json.dumps([name, kind, launch_id]).encode()
        lock_path = self.state_dir / ("probe-" + hashlib.sha256(key).hexdigest() + ".lock")
        with lock_path.open("a") as lock:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return ProbeResult(None, "", "probe lock timed out", True), time.time()
                    time.sleep(min(0.05, remaining))
            try:
                if not fresh:
                    cached = cache.get(name, kind, launch_id)
                    if cached is not None:
                        cached = replace(
                            cached,
                            stdout=self.redact(name, cached.stdout),
                            stderr=self.redact(name, cached.stderr),
                        )
                        return cached, cache.first_failure(name, kind, launch_id)
                service = self.cfg.services[name]
                try:
                    result = self.proc.run_probe(
                        cmd,
                        cwd=str(self.repo_dir / service.working_dir),
                        env=self.launch_env(name),
                        timeout=timeout,
                    )
                except OSError as exc:
                    result = ProbeResult(None, "", str(exc)[-16384:], False)
                result = replace(
                    result,
                    stdout=self.redact(name, result.stdout),
                    stderr=self.redact(name, result.stderr),
                )
                cache.put(name, kind, launch_id, result)
                return result, cache.first_failure(name, kind, launch_id)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _health(self, item: ServiceStatus, *, fresh: bool) -> ServiceStatus:
        service = self.cfg.services[item.name]
        command = service.health or service.ready
        if item.state != "ready" or not command:
            return item
        launch_id = item.since or ""
        result, first = self._probe_launch(
            item.name, "health", command, service.health_timeout, launch_id, fresh=fresh
        )
        if result.code == 0 and not result.timed_out:
            return item
        if result.timed_out:
            detail = (
                result.stderr
                if result.stderr == "probe lock timed out"
                else f"health check timed out after {service.health_timeout}s"
            )
        else:
            lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
            if not lines:
                lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            detail = f"health check failed (exit {result.code})"
            if lines:
                detail += ": " + lines[-1][:200]
        since = datetime.fromtimestamp(first, UTC).isoformat() if first is not None else item.since
        return replace(
            item,
            state="unhealthy",
            since=since,
            detail=compose_detail(detail, None, item.detail) if item.launch else detail,
        )

    def status(self, *, probe: bool = True, fresh: bool = False) -> list[ServiceStatus]:
        panes, states = self._panes(), self._state()
        items = [
            self._with_launch(self._lifecycle(name, panes, states)) for name in self.cfg.services
        ]
        if not probe:
            return items
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(lambda item: self._health(item, fresh=fresh), items))

    def _with_launch(self, item: ServiceStatus) -> ServiceStatus:
        if item.window is None or item.state == "exited":
            return item
        current = self.launch_store.current(item.name)
        persisted = self.launch_store.persisted(item.name)
        launch, is_persisted = current if current else (Launch(), False)
        if not (launch.args or launch.env or persisted.args or persisted.env):
            return item
        args = [self.redact(item.name, arg) for arg in launch.args]
        persisted_args = [self.redact(item.name, arg) for arg in persisted.args]
        metadata: dict[str, object] = {
            "args": args,
            "env_keys": sorted(launch.env),
            "persisted_args": persisted_args,
            "persisted_env_keys": sorted(persisted.env),
        }
        parts = []
        if launch.args:
            parts.append(
                f"args: {shlex.join(args)} ({'persisted' if is_persisted else 'one-off'})"
            )
        if launch.env:
            parts.append("env: " + ",".join(sorted(launch.env)))
        if persisted.args and persisted.args != launch.args:
            parts.append("persisted args: " + shlex.join(persisted_args))
        return replace(
            item,
            launch=metadata,
            detail=self.redact(item.name, compose_detail(item.detail, None, "; ".join(parts))),
        )

    def wait(
        self, names: Sequence[str], until: str, *, timeout: float, interval: float
    ) -> tuple[int, ServiceStatus | None]:
        states = ("stopped", "starting", "ready", "failed", "exited", "unhealthy")
        if (
            not names
            or any(name not in self.cfg.services for name in names)
            or until not in states
            or not math.isfinite(timeout)
            or timeout < 0
            or not math.isfinite(interval)
            or interval < 1
        ):
            return 2, None
        deadline = self.proc.now() + timeout if timeout else None
        while True:
            statuses = {item.name: item for item in self.status(fresh=True)}
            for name in names:
                item = statuses[name]
                if item.state == until or (
                    until == "unhealthy" and item.state in ("exited", "failed", "stopped")
                ):
                    return 0, item
            if until == "ready":
                for name in names:
                    item = statuses[name]
                    if item.state in ("failed", "exited", "stopped"):
                        return 3, item
            remaining = deadline - self.proc.now() if deadline is not None else None
            if remaining is not None and remaining <= 0:
                return 1, None
            self.proc.sleep(min(interval, remaining) if remaining is not None else interval)

    def _target(self, name: str) -> str:
        return f"{SESSION_NAME}:={WINDOW_PREFIX}{name}"

    def logs(self, name: str, lines: int) -> str:
        topo_order(self.cfg.services, [name])
        result = self.proc.tmux(["capture-pane", "-p", "-t", self._target(name), "-S", f"-{lines}"])
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "failed to capture service window")
        rows = result.stdout.splitlines(keepends=True)
        # capture-pane pads the output with the empty rows of the visible pane;
        # drop them so the last N lines are real output, not blank padding.
        while rows and not rows[-1].strip():
            rows.pop()
        return "".join(rows[-lines:])

    def follow_logs(self, name: str, lines: int) -> Iterator[str]:
        topo_order(self.cfg.services, [name])
        yield from self.proc.follow_pane(self._target(name), lines)

    def _shell(
        self, name: str, service: Service, command: str, timeout: float, *, capture: bool
    ) -> int:
        return self.proc.run_shell(
            command,
            cwd=str(self.repo_dir / service.working_dir),
            env=self.launch_env(name),
            timeout=timeout,
            capture=capture,
        )

    def _identity(self, name: str) -> Path:
        return self.state_dir / f"anchor-{name}.json"

    def _cleanup_group(self, name: str) -> None:
        path = self._identity(name)
        self.proc.signal_identity(path, signal.SIGKILL)
        path.unlink(missing_ok=True)

    def _start_one(self, name: str, effective: Launch, args_persisted: bool) -> bool:
        service = self.cfg.services[name]
        target = self._target(name)
        existing = self._panes().get(WINDOW_PREFIX + name)
        if existing is not None:
            self.proc.tmux(["kill-window", "-t", target])
        self._cleanup_group(name)
        self.launch_store.set_current(name, effective, args_persisted)
        if self.proc.tmux(["has-session", "-t", SESSION_NAME]).returncode:
            result = self.proc.tmux(
                ["new-session", "-d", "-s", SESSION_NAME, "-x", "200", "-y", "50"]
            )
            if result.returncode:
                self._save(name, "failed", self.redact(name, result.stderr))
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
        for key, value in self.launch_env(name).items():
            args.extend(["-e", f"{key}={value}"])
        # Keep the initial pane alive until its window-local option is set.
        args.extend(["sleep", "infinity"])
        created = self.proc.tmux(args)
        if created.returncode:
            self._save(name, "failed", self.redact(name, created.stderr))
            return False
        configured = self.proc.tmux(["set-option", "-w", "-t", target, "remain-on-exit", "on"])
        if configured.returncode:
            self.proc.tmux(["kill-window", "-t", target])
            self._save(name, "failed", self.redact(name, configured.stderr))
            return False
        respawn = ["respawn-pane", "-k", "-t", target]
        for key, value in self.launch_env(name).items():
            respawn.extend(["-e", f"{key}={value}"])
        respawn.extend(
            [
                "bash",
                "-lc",
                self.proc.service_command(service.run, self._identity(name), effective.args),
            ]
        )
        spawned = self.proc.tmux(respawn)
        if spawned.returncode:
            self._save(name, "failed", self.redact(name, spawned.stderr))
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
                if self._shell(name, service, service.ready, remaining, capture=True) == 0:
                    pane = self._panes().get(WINDOW_PREFIX + name)
                    if pane is not None and not pane.dead and self.proc.now() <= deadline:
                        self._save(name, "ready")
                        return True
                    continue
            remaining = deadline - self.proc.now()
            if remaining > 0:
                self.proc.sleep(min(READY_POLL_S, remaining))

    def _validate_launch(self, names: Sequence[str], launch: LaunchRequest | None) -> None:
        topo_order(self.cfg.services, names)
        if launch is None:
            return
        if len(names) != 1:
            raise ValueError("runtime options apply to exactly one service")
        if launch.args is not None and not self.cfg.services[names[0]].args:
            raise ValueError(f"positional arguments require services.{names[0]}.args")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in launch.env):
            raise ValueError("invalid runtime environment key")

    def _start_unlocked(
        self,
        names: Sequence[str],
        *,
        target: str | None = None,
        resolved: tuple[Launch, bool] | None = None,
    ) -> RunReport:
        services = self.cfg.services
        order = topo_order(services, with_dependencies(services, names))
        done: list[str] = []
        for name in order:
            item = self._lifecycle(name, self._panes(), self._state())
            if item.state == "ready" and name != target:
                continue
            effective, persisted = (
                resolved
                if name == target and resolved is not None
                else (self.launch_store.persisted(name), True)
            )
            if not self._start_one(name, effective, persisted):
                try:
                    tail = self.logs(name, 30)
                except RuntimeError as exc:
                    tail = f"failed to capture service logs: {exc}"
                return RunReport(False, tuple(done), name, self.redact(name, tail))
            done.append(name)
        return RunReport(True, tuple(done), None, "")

    def _resolve_request(self, name: str, request: LaunchRequest) -> tuple[Launch, bool]:
        persisted, effective, flag = resolve_launch(
            self.launch_store.persisted(name),
            args=request.args,
            env=request.env,
            persist=request.persist,
            reset=request.reset,
        )
        if request.persist or request.reset:
            self.launch_store.set_persisted(name, persisted)
        return effective, flag

    def start(self, names: Sequence[str], *, launch: LaunchRequest | None = None) -> RunReport:
        self._validate_launch(names, launch)
        with self.locked():
            if launch is not None:
                name = names[0]
                item = self._lifecycle(name, self._panes(), self._state())
                if item.state == "ready":
                    return self._restart_unlocked(names, launch=launch)
                return self._start_unlocked(
                    names, target=name, resolved=self._resolve_request(name, launch)
                )
            return self._start_unlocked(names)

    def _stop_unlocked(self, names: Sequence[str]) -> RunReport:
        order = topo_order(self.cfg.services, names)
        done: list[str] = []
        for name in reversed(order):
            pane = self._panes().get(WINDOW_PREFIX + name)
            if pane is None:
                self._cleanup_group(name)
                self.launch_store.clear_current(name)
                continue
            service = self.cfg.services[name]
            if not pane.dead:
                if service.stop:
                    self._shell(name, service, service.stop, service.stop_timeout, capture=False)
                else:
                    self.proc.signal_identity(self._identity(name), signal.SIGINT)
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
            self._cleanup_group(name)
            self.launch_store.clear_current(name)
            self._save(name, "stopped")
            done.append(name)
        return RunReport(True, tuple(done), None, "")

    def stop(self, names: Sequence[str]) -> RunReport:
        with self.locked():
            return self._stop_unlocked(names)

    def _restart_unlocked(
        self, names: Sequence[str], *, launch: LaunchRequest | None = None
    ) -> RunReport:
        selected = restart_set(self.cfg.services, names)
        order = topo_order(self.cfg.services, selected)
        resolved = self._resolve_request(names[0], launch) if launch is not None else None
        self._stop_unlocked(order)
        return self._start_unlocked(
            order, target=names[0] if launch is not None else None, resolved=resolved
        )

    def restart(self, names: Sequence[str], *, launch: LaunchRequest | None = None) -> RunReport:
        self._validate_launch(names, launch)
        with self.locked():
            return self._restart_unlocked(names, launch=launch)
