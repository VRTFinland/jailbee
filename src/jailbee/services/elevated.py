"""Validate accepted refresh plans and their declared temporary rights."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jailbee.config.models_services import SERVICE_NAME_RE
from jailbee.orchestration.grant import ChildRights, Grant, check_rights
from jailbee.orchestration.profiles import repo_cap

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import ExecResult, Incus
    from jailbee.orchestration.agent_watch import AgentStatus
    from jailbee.orchestration.runner import Runner


class RefreshError(RuntimeError):
    """A refresh plan cannot safely be accepted."""


@dataclass(frozen=True)
class PausedAgent:
    agent: str
    session: str
    runner: "Runner | None"


def pause_agent(
    cfg: "Config", incus: "Incus", full: str, *, force: bool = False, now: datetime
) -> PausedAgent | None:
    from jailbee import autostart, tmux
    from jailbee.accounts.adapters.base import get_adapter
    from jailbee.agents import primary_agent
    from jailbee.incus import IncusError
    from jailbee.orchestration import agent_watch, labels
    from jailbee.orchestration.runner import parse_runner_label, runner_command

    status = agent_watch.probe(incus, full, uid=cfg.container_user.uid)
    if status is None:
        raise RefreshError(
            f"could not determine whether an agent is running in {full}; refresh refused"
        )
    if not status.alive:
        return None
    try:
        config = {
            key: incus.config_get_checked(full, key) or ""
            for key in (labels.AGENT_SESSIONS, labels.RUNNER)
        }
    except IncusError as exc:
        raise RefreshError(f"cannot read agent configuration: {exc}") from exc
    session = agent_watch.current_session(cfg, full, status)
    if not labels.valid_session_id(session):
        history = labels.sessions_of(config)
        session = history[-1] if history else None
    if not isinstance(session, str) or not labels.valid_session_id(session):
        raise RefreshError(
            f"the agent in {full} is running but its session id is unknown, "
            "so it could not be resumed — refresh refused"
        )
    if not force and agent_watch.idle_signal(cfg, full, status, now=now) is None:
        raise RefreshError(
            f"the agent in {full} is working (not idle); refresh again when it waits for input, "
            "or pass force if it has said its work is paused"
        )
    try:
        adapter = get_adapter(status.agent)
        if not adapter.resume_args(session):
            raise RefreshError(f"agent `{status.agent}` cannot resume a session")
        if not adapter.session_exists(cfg, full, session):
            raise RefreshError(f"session `{session}` has no transcript any more")
    except (KeyError, LookupError) as exc:
        raise RefreshError(f"agent `{status.agent}` cannot resume a session") from exc
    runner = parse_runner_label(config[labels.RUNNER])
    target = "claude" if runner is not None else primary_agent(cfg).name
    agent = cfg.agents.get(status.agent)
    if (
        target != status.agent
        or agent is None
        or not agent.enabled
        or not agent.command.strip()
        or not agent.prompt_args
        or not agent.prompt_args.strip()
    ):
        raise RefreshError(f"agent `{status.agent}` cannot be relaunched with its session")
    if runner is not None and runner_command(agent.command, runner) is None:
        raise RefreshError(f"agent `{status.agent}` cannot be relaunched with its runner")
    steps = autostart.agent_autostart_steps(cfg, initial_prompt=True, runner=runner, resume=session)
    if not any(step.name == target and step.run for step in steps):
        raise RefreshError(f"agent `{status.agent}` has no usable relaunch step")
    tmux.kill_window(incus, full, tmux.window_for(status.agent), checked=True)
    return PausedAgent(status.agent, session, runner)


def resume_agent(
    cfg: "Config", incus: "Incus", full: str, paused: PausedAgent, prompt: str
) -> str | None:
    from jailbee.orchestration import agent_session

    try:
        agent_session.relaunch(
            cfg,
            incus,
            full,
            prompt,
            runner=paused.runner,
            old_agent=paused.agent,
            resume=paused.session,
        )
    except Exception as exc:
        return str(exc)
    return None


def foreign_agents(
    cfg: "Config", incus: "Incus", full: str, prompted: "AgentStatus | None"
) -> list[str]:
    from jailbee import agent_status, lifecycle, procstat
    from jailbee.incus import IncusError

    try:
        containers = incus.list_containers()
    except (IncusError, ValueError) as exc:
        raise RefreshError(f"cannot inspect container processes: {exc}") from exc
    item = next((c for c in containers if c.get("name") == full), None)
    state = item.get("state") if item else None
    pid = state.get("pid") if isinstance(state, dict) else None
    if type(pid) is not int or pid <= 0:
        raise RefreshError(f"cannot inspect container processes in {full}")
    sessions = agent_status._one_per_process(
        agent_status.read_sessions(lifecycle.agent_homes(cfg, [full])).get(full, ())
    )
    by_start = {s.proc_start for s in sessions}
    live = set()
    try:
        for host_pid in procstat.read_container_pids(pid, full, strict=True):
            sample = procstat.read_process(host_pid, strict=True)
            if sample is not None and sample.starttime in by_start:
                inner = procstat.read_nspid(host_pid, strict=True)
                live.add((inner, sample.starttime))
    except OSError as exc:
        raise RefreshError(f"cannot inspect container processes: {exc}") from exc
    excluded = (prompted.pid, prompted.proc_start) if prompted and prompted.alive else None
    return [
        f"{s.agent} (pid {s.pid})"
        for s in sessions
        if (s.pid, s.proc_start) in live and (s.pid, s.proc_start) != excluded
    ]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class _Step(_Strict):
    name: str
    run: str
    env: dict[str, str]
    working_dir: str
    timeout: int = Field(gt=0)
    continue_on_error: bool


class _Elevation(_Strict):
    network: Literal["loose"] | None
    mounts: list[str]


class _Service(_Strict):
    name: str
    depends_on: list[str]
    will_prepare: bool
    prepare: list[_Step]
    elevation: _Elevation
    ready_timeout: int = Field(gt=0)
    stop_timeout: int = Field(gt=0)


class _Payload(_Strict):
    version: Literal[1]
    op: Literal["refresh"]
    order: list[str]
    services: list[_Service]


def _name(value: str) -> bool:
    return bool(SERVICE_NAME_RE.fullmatch(value))


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True)
class PlannedService:
    name: str
    will_prepare: bool
    prepare: tuple[Mapping[str, object], ...]
    network: Literal["loose"] | None
    mounts: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    ready_timeout: int = 120
    stop_timeout: int = 10

    @property
    def elevated(self) -> bool:
        return self.will_prepare and (self.network == "loose" or bool(self.mounts))


@dataclass(frozen=True)
class Plan:
    order: tuple[str, ...]
    named: tuple[str, ...]
    services: tuple[PlannedService, ...]

    @property
    def elevated(self) -> bool:
        return any(service.elevated for service in self.services)

    def declared(self) -> ChildRights:
        return ChildRights(
            loose=any(s.elevated and s.network == "loose" for s in self.services),
            mounts=tuple(m for s in self.services if s.elevated for m in s.mounts),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "version": 1,
            "op": "refresh",
            "order": list(self.order),
            "services": [
                {
                    "name": s.name,
                    "depends_on": list(s.depends_on),
                    "will_prepare": s.will_prepare,
                    "prepare": [dict(step) for step in s.prepare],
                    "elevation": {"network": s.network, "mounts": list(s.mounts)},
                    "ready_timeout": s.ready_timeout,
                    "stop_timeout": s.stop_timeout,
                }
                for s in self.services
            ],
        }

    @classmethod
    def from_payload(cls, raw: str | bytes, named: Sequence[str]) -> "Plan":
        try:
            data = raw.encode("utf-8") if isinstance(raw, str) else raw
            if not isinstance(data, bytes) or len(data) > 256 * 1024:
                raise ValueError("invalid payload size")
            decoded = json.loads(data, object_pairs_hook=_object)
            # Literal[1] otherwise accepts True because bool is an int subclass.
            if not isinstance(decoded, dict) or type(decoded.get("version")) is not int:
                raise ValueError("invalid version")
            payload = _Payload.model_validate(decoded)
            if isinstance(named, (str, bytes)) or any(
                not isinstance(n, str) or not _name(n) for n in named
            ):
                raise ValueError("invalid requested service names")
            targets = tuple(named) if named else tuple(payload.order)
            if len(set(targets)) != len(targets) or not set(targets) <= set(payload.order):
                raise ValueError("invalid requested services")
            if (
                any(not _name(n) for n in payload.order)
                or len(set(payload.order)) != len(payload.order)
                or payload.order != [s.name for s in payload.services]
            ):
                raise ValueError("invalid service order")
            positions = {name: i for i, name in enumerate(payload.order)}
            for s in payload.services:
                if (
                    any(not _name(n) for n in s.depends_on)
                    or any(
                        positions[dependency] >= positions[s.name]
                        for dependency in s.depends_on
                        if dependency in positions
                    )
                    or any(not m for m in s.elevation.mounts)
                    or s.will_prepare != (s.name in targets and bool(s.prepare))
                ):
                    raise ValueError("invalid service declaration")
            services = tuple(
                PlannedService(
                    name=s.name,
                    will_prepare=s.will_prepare,
                    prepare=tuple(step.model_dump() for step in s.prepare),
                    network=s.elevation.network,
                    mounts=tuple(s.elevation.mounts),
                    depends_on=tuple(s.depends_on),
                    ready_timeout=s.ready_timeout,
                    stop_timeout=s.stop_timeout,
                )
                for s in payload.services
            )
            plan = cls(tuple(payload.order), targets, services)
            # The default JSON representation must also fit when persisted and reloaded.
            if len(json.dumps(plan.to_json()).encode("utf-8")) > 256 * 1024:
                raise ValueError("serialized payload exceeds size limit")
            return plan
        except (ValueError, TypeError, UnicodeError, ValidationError, RecursionError) as exc:
            raise RefreshError(f"invalid refresh plan: {exc}") from exc


@dataclass(frozen=True)
class ServiceOutcome:
    name: str
    result: str


@dataclass(frozen=True)
class RefreshReport:
    services: tuple[ServiceOutcome, ...]
    ok: bool
    agent: str

    def summary(self) -> str:
        return "\n".join(
            [*(f"{s.name}: {s.result}" for s in self.services), f"agent: {self.agent}"]
        )

    def agent_text(self, who: str) -> str:
        details = "; ".join(f"{s.name}: {s.result}" for s in self.services)
        return (
            f"Your container's services were refreshed ({who}): {details}. "
            "Continue your task; re-read files you had open if dependencies changed."
        )


def _snapshot(cfg: "Config", incus: "Incus", full: str) -> tuple[str, set[str]]:
    import yaml

    from jailbee.lifecycle import current_network_mode
    from jailbee.network_generation import generation_of
    from jailbee.work_mode import work_mode_state

    rows = incus.list_containers()
    matches = [row for row in rows if isinstance(row, dict) and row.get("name") == full]
    if (
        len(matches) != 1
        or not isinstance(matches[0].get("profiles"), list)
        or not all(isinstance(p, str) for p in matches[0]["profiles"])
    ):
        raise RefreshError("container network state unavailable")
    row = matches[0]
    if generation_of(cfg, row) == "work":
        mode, agrees = work_mode_state(cfg, row, incus)
        if not agrees:
            raise RefreshError("work network marker and ACL disagree")
    else:
        mode = current_network_mode(cfg, incus, full)
    if mode not in ("strict", "loose"):
        raise RefreshError("container network mode unavailable")
    data = yaml.safe_load(incus.config_show(full, expanded=True))
    if not isinstance(data, dict) or not isinstance(data.get("devices"), dict):
        raise RefreshError("container devices unavailable")
    devices = data["devices"]
    if not all(isinstance(k, str) and isinstance(v, dict) for k, v in devices.items()):
        raise RefreshError("invalid container devices")
    return mode, {k.removeprefix("optional-") for k in devices if k.startswith("optional-")}


def _execute(
    cfg: "Config", incus: "Incus", full: str, repo: str, operation: str, names: Sequence[str]
) -> "ExecResult":
    from jailbee import container_app
    from jailbee.config import CONTAINER_USERNAME

    return container_app.run_in_container(
        cfg,
        incus,
        full,
        ["service", "--repo", repo, operation, *names],
        uid=cfg.container_user.uid,
        cwd=repo,
        env={"HOME": f"/home/{CONTAINER_USERNAME}"},
        init_groups=True,
    )


def _reason(exc: Exception) -> str:
    from jailbee.autostart import AutostartStepError

    if isinstance(exc, AutostartStepError):
        return f"{exc.step_name}: {exc.reason}" + (
            f" (exit {exc.exit_code})" if exc.exit_code is not None else ""
        )
    return str(exc) or type(exc).__name__


def run_refresh(
    cfg: "Config",
    incus: "Incus",
    full: str,
    plan: Plan,
    *,
    who: str,
    resume_prompt_prefix: str = "",
    before_resume: Callable[[], None] | None = None,
    mirror_endpoint: tuple[str, int] | None,
    on_progress: Callable[[str, str, str], None] | None = None,
    force: bool = False,
) -> RefreshReport:
    from jailbee import autostart
    from jailbee.autostart_profiles import PREPARED_KEY, decode_names, encode_names
    from jailbee.lifecycle import container_repo_dir
    from jailbee.services import autostart as service_autostart

    try:
        raw = incus.config_get_checked(full, service_autostart.ENVELOPE_KEY)
        grants = service_autostart._approved(raw) if raw is not None else {}
    except Exception as exc:
        raise RefreshError(f"invalid approved service elevation envelope: {exc}") from exc
    try:
        entry = _snapshot(cfg, incus, full) if plan.elevated else None
        prepared = decode_names(incus.config_get_checked(full, PREPARED_KEY)) or []
        repo = container_repo_dir(cfg, incus, full)
    except Exception as exc:
        raise RefreshError(f"refresh preflight failed: {exc}") from exc
    paused = (
        pause_agent(cfg, incus, full, force=force, now=datetime.now(UTC)) if plan.elevated else None
    )
    results = {name: "skipped" for name in plan.order}
    agent = "not running" if paused is None else f"resumed {paused.session}"
    ok = False
    restored = True
    try:
        for service in plan.services:
            if not service.will_prepare:
                continue
            failure = None
            if entry is not None:
                restored = False
            try:
                stage = service_autostart.prepare_stage(
                    full,
                    service.name,
                    [dict(s) for s in service.prepare],
                    network=service.network if service.elevated else None,
                    mounts=list(service.mounts) if service.elevated else [],
                )
                autostart.run_stage(
                    cfg,
                    incus,
                    full,
                    stage,
                    repo,
                    mirror_endpoint=mirror_endpoint,
                    on_progress=on_progress,
                    cas_restore=False,
                )
            except BaseException as exc:
                failure = _reason(exc) if isinstance(exc, Exception) else type(exc).__name__
            if entry is not None:
                try:
                    current = _snapshot(cfg, incus, full)
                    extra = current[1] - entry[1]
                    if current[0] != entry[0] or extra:
                        raise RefreshError(
                            f"network {current[0]} (expected {entry[0]}), "
                            f"extra mounts: {', '.join(sorted(extra)) or 'none'}"
                        )
                    restored = True
                except BaseException as exc:
                    detail = _reason(exc) if isinstance(exc, Exception) else type(exc).__name__
                    commands = [
                        f"jb net strict {full}",
                        *(
                            f"jb unmount {m} {full}"
                            for m in sorted(set(plan.declared().mounts) - entry[1])
                        ),
                    ]
                    agent = (
                        f"resume withheld: container may still be elevated ({detail}); "
                        f"recovery: {'; '.join(commands)}"
                    )
                    results[service.name] = (
                        f"prepare failed: {failure or 'restoration unconfirmed'}"
                    )
                    break
            if failure is not None:
                results[service.name] = f"prepare failed: {failure}"
                break
            try:
                previous = grants.get(service.name, {"network": None, "mounts": []})
                grants[service.name] = {
                    "network": "loose"
                    if service.network == "loose" or previous.get("network") == "loose"
                    else None,
                    "mounts": list(dict.fromkeys([*previous["mounts"], *service.mounts])),
                }
                incus.config_set(
                    full,
                    service_autostart.ENVELOPE_KEY,
                    json.dumps({"version": 1, "services": grants}, separators=(",", ":")),
                )
                if service.name not in prepared:
                    prepared.append(service.name)
                incus.config_set(full, PREPARED_KEY, encode_names(prepared))
                results[service.name] = "prepared"
            except Exception as exc:
                results[service.name] = f"prepare failed: {_reason(exc)}"
                break
        else:
            try:
                result = _execute(cfg, incus, full, repo, "restart", plan.named)
                if result.returncode:
                    text = result.stderr.decode(errors="replace").strip().splitlines()
                    raise RefreshError(
                        f"{text[0] if text else 'container restart failed'} "
                        f"(exit {result.returncode})"
                    )
                for name in plan.order:
                    results[name] = (
                        "prepared, restarted"
                        if results[name] == "prepared"
                        else "restarted"
                        if name in plan.named
                        else "restarted (cascade)"
                    )
                ok = True
            except Exception as exc:
                for name in plan.order:
                    results[name] = f"restart failed: {_reason(exc)}"
    finally:
        if paused is not None and restored:
            try:
                if before_resume is not None:
                    before_resume()
            except Exception as exc:
                agent = f"resume failed: {_reason(exc)}"
                ok = False
            report = RefreshReport(
                tuple(ServiceOutcome(n, results[n]) for n in plan.order), ok, agent
            )
            try:
                error = resume_agent(
                    cfg, incus, full, paused, resume_prompt_prefix + report.agent_text(who)
                )
            except Exception as exc:
                error = _reason(exc)
            if error:
                agent = f"resume failed: {error}"
                ok = False
    return RefreshReport(tuple(ServiceOutcome(n, results[n]) for n in plan.order), ok, agent)


def run_plain_refresh(
    cfg: "Config", incus: "Incus", full: str, names: Sequence[str]
) -> RefreshReport:
    from jailbee.lifecycle import container_repo_dir

    try:
        repo = container_repo_dir(cfg, incus, full)
        result = _execute(cfg, incus, full, repo, "refresh", names)
        ok = result.returncode == 0
        output = (result.stderr or result.stdout).decode(errors="replace").strip()
        detail = (
            "prepared, restarted"
            if ok
            else f"failed: {output[-1000:] or f'exit {result.returncode}'}"
        )
    except Exception as exc:
        ok, detail = False, f"failed: {_reason(exc)}"
    return RefreshReport(tuple(ServiceOutcome(name, detail) for name in names), ok, "not running")


def orchestrator_policy(cfg: "Config", grant: Grant, plan: Plan) -> list[str]:
    return check_rights(grant, repo_cap(cfg), plan.declared(), cfg.optional_mounts.keys())


def run_job(cfg: "Config", incus: "Incus", job_path: Path) -> int:
    from jailbee.services.refresh_worker import run_job as worker

    return worker(cfg, incus, job_path)


def host_policy(cfg: "Config", plan: Plan) -> list[str]:
    return [
        f"service {s.name!r} prepare needs optional mount {mount!r}, "
        "which this host does not define"
        for s in plan.services
        if s.will_prepare
        for mount in s.mounts
        if mount not in cfg.optional_mounts
    ]
