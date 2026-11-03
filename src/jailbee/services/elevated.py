"""Validate accepted refresh plans and their declared temporary rights."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jailbee.config.models_services import SERVICE_NAME_RE
from jailbee.orchestration.grant import ChildRights, Grant, check_rights
from jailbee.orchestration.profiles import repo_cap

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
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


def orchestrator_policy(cfg: "Config", grant: Grant, plan: Plan) -> list[str]:
    return check_rights(grant, repo_cap(cfg), plan.declared(), cfg.optional_mounts.keys())


def host_policy(cfg: "Config", plan: Plan) -> list[str]:
    return [
        f"service {s.name!r} prepare needs optional mount {mount!r}, "
        "which this host does not define"
        for s in plan.services
        if s.will_prepare
        for mount in s.mounts
        if mount not in cfg.optional_mounts
    ]
