"""`jailbee agent use|show`: the repo's default runner, `agents.default_runner`.

The selection logic sits behind the `Runner` model (`orchestration.runner_model`):
a *profile* names where the agent runs (`claude` is the plain agent, anything
else a LiteLLM profile run through `claude-jb`) and a *tier* the model. Only the
Claude family is offered now; another agent family is a new branch in
`build_runner` / `catalog`, not a change to the callers.

Host side, repo-scoped, container-agnostic. The choice is written to the repo's
host-local layer (`repos/<prefix>.yaml`); `jailbee apply` and `new` keep the
`claude-jb` default profile in the containers in step with it
(`litellm_default_profile`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from jailbee import litellm
from jailbee.config_writer import DELETE, YamlChange, patch_local_file
from jailbee.dashboard.agent_choice import PLAIN
from jailbee.incus import IncusError
from jailbee.orchestration.runner import read_runner, runner_problems
from jailbee.orchestration.runner_model import RUNNER_TIERS, Runner, model_tier

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

KEY_PATH = ("agents", "default_runner")


class SelectionError(ValueError):
    """The choice cannot be honoured; the message names the candidates."""


def build_runner(profile: str, model: str | None) -> Runner:
    """`claude` is the plain agent; any other profile is `claude-jb` on that profile."""
    try:
        if profile == PLAIN:
            return Runner(agent="claude", model=model)
        return Runner(agent="claude-jb", profile=profile, model=model)
    except ValidationError as e:
        raise SelectionError(
            "; ".join(str(err["msg"]).removeprefix("Value error, ") for err in e.errors())
        ) from None


def parse_selection(text: str) -> tuple[str, str | None]:
    """`profile` or `profile/model` -> (profile, model)."""
    profile, slash, model = text.strip().partition("/")
    if not profile or "/" in model or (slash and not model):
        raise SelectionError(f"expected PROFILE or PROFILE/MODEL, got {text!r}")
    return profile, model or None


def catalog(cfg: Config, incus: Incus, gcfg: GlobalConfig) -> dict[str, tuple[str, ...]]:
    """Profile -> tiers it offers: `claude` plus every LiteLLM profile this repo is served."""
    out: dict[str, tuple[str, ...]] = {PLAIN: RUNNER_TIERS}
    payload = litellm.container_sync_payload(incus, gcfg, view=cfg.litellm_view())
    data = payload.get("json") if payload else None
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if isinstance(profiles, dict):
        for name, body in profiles.items():
            tiers = body.get("tiers") if isinstance(body, dict) else None
            known = tiers if isinstance(tiers, dict) else {}
            if str(name) != PLAIN:  # `claude` always means the plain agent
                out[str(name)] = tuple(t for t in RUNNER_TIERS if t in known)
    return out


def problems(cfg: Config, incus: Incus, gcfg: GlobalConfig, runner: Runner) -> list[str]:
    """Why `runner` cannot be the default; every message names the candidates."""
    offered = catalog(cfg, incus, gcfg)
    name = runner.profile or PLAIN
    if name not in offered:
        return [f"unknown profile {name!r}; choose from: {', '.join(offered)}"]
    if runner.model and model_tier(runner.model) not in offered[name]:
        return [
            f"profile {name!r} has no {model_tier(runner.model)!r} tier; "
            f"choose from: {', '.join(offered[name]) or 'none'}"
        ]
    return runner_problems(cfg, incus, gcfg, runner)


def write(cfg: Config, runner: Runner | None) -> None:
    """Write (or, with None, remove) `agents.default_runner` in the repo's local layer."""
    if runner is not None:
        value = runner.model_dump(mode="json", exclude_none=True)
        patch_local_file(cfg.container_prefix, [YamlChange(KEY_PATH, value)])
        return
    from jailbee.config.local_layer import local_config_path

    try:
        raw = yaml.safe_load(local_config_path(cfg.container_prefix).read_text())
    except (OSError, yaml.YAMLError):
        raw = None
    agents = raw.get("agents") if isinstance(raw, dict) else None
    # Removing the only key leaves no `agents: {}` husk behind.
    only_key = isinstance(agents, dict) and set(agents) == {KEY_PATH[1]}
    patch_local_file(
        cfg.container_prefix, [YamlChange(KEY_PATH[:1] if only_key else KEY_PATH, DELETE)]
    )


def litellm_default_profile(cfg: Config) -> str | None:
    """The LiteLLM profile `claude-jb` should default to in this repo's containers."""
    runner = cfg.orchestration_view().default_runner
    return runner.profile if runner is not None and runner.agent == "claude-jb" else None


@dataclass
class Resync:
    synced: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def resync(cfg: Config, incus: Incus, gcfg: GlobalConfig, profile: str | None) -> Resync:
    """Rewrite the `claude-jb` settings in the repo's running containers.

    ``profile`` is the new default (None: the LiteLLM config's own). Running
    agent sessions are not touched; only the file a manually started
    `claude-jb` reads.
    """
    from jailbee.lifecycle import list_containers

    result = Resync()
    try:
        payload = litellm.container_sync_payload(
            incus, gcfg, view=cfg.litellm_view(), default_profile=profile
        )
    except IncusError as e:
        result.failed.append(("(proxy)", str(e)))
        return result
    for info in list_containers(cfg, incus):
        if info.state != "Running":
            continue
        try:
            litellm.sync_container(incus, info.name, payload)
        except (IncusError, OSError) as e:
            result.failed.append((info.name, str(e)))
        else:
            result.synced.append(info.name)
    return result


@dataclass(frozen=True)
class Effective:
    runner: Runner | None
    source: str  # local / repo / global / legacy
    legacy: str | None = None  # what the legacy path launches, when source == "legacy"
    labels: tuple[tuple[str, Runner], ...] = ()  # running containers carrying a runner label


def effective(cfg: Config, incus: Incus) -> Effective:
    from jailbee.agents import primary_agent
    from jailbee.autostart import litellm_autostart_on
    from jailbee.lifecycle import list_containers

    view = cfg.orchestration_view()
    labels = tuple(
        (info.name, runner)
        for info in list_containers(cfg, incus)
        if info.state == "Running" and (runner := read_runner(incus, info.name)) is not None
    )
    if view.default_runner is not None and view.default_runner_source is not None:
        return Effective(view.default_runner, view.default_runner_source, labels=labels)
    agent = primary_agent(cfg).name or "none"
    via = "claude-jb (litellm.autostart)" if litellm_autostart_on(cfg) else agent
    default = cfg.litellm_view().config.default_profile
    if litellm_autostart_on(cfg):
        via += f", profile {default}"
    return Effective(None, "legacy", legacy=via, labels=labels)


def choose(
    cfg: Config,
    incus: Incus,
    gcfg: GlobalConfig,
    selection: str | None,
    *,
    container: str | None = None,
) -> Runner | None:
    """The runner to write: parsed from `selection`, else asked for (profile, then tier).

    The rows come from `dashboard.agent_choice`, shared with the dashboards; the
    current default is marked and the cursor starts on it. For one ``container``
    the picker starts with a "repo default" row, which answers None (no label).
    """
    from jailbee import prompting
    from jailbee.dashboard import agent_choice

    if selection is not None:
        profile, model = parse_selection(selection)
        return build_runner(profile, model)
    offered = catalog(cfg, incus, gcfg)
    current = (
        read_runner(incus, container)
        if container is not None
        else cfg.orchestration_view().default_runner
    )
    now_profile, now_tier = agent_choice.split_current(current)
    profile = prompting.choose_one(
        "agent profile",
        agent_choice.profile_options(offered, now_profile, repo_default=container is not None),
        question=(
            f"Agent for {container}:"
            if container is not None
            else "Default agent profile for this repo's containers:"
        ),
        default=now_profile
        if now_profile in offered
        else agent_choice.REPO_DEFAULT
        if container is not None
        else None,
    )
    if profile == agent_choice.REPO_DEFAULT:
        return None
    tier_now = now_tier if profile == now_profile else None
    tier = prompting.choose_one(
        "model tier",
        agent_choice.tier_options(offered[profile], tier_now),
        question=f"Model tier for {profile}:",
        always_ask=True,
        default=tier_now or agent_choice.DEFAULT_TIER,
    )
    return build_runner(profile, tier or None)


@dataclass
class UseResult:
    runner: Runner | None
    resync: Resync


def use(
    cfg: Config,
    incus: Incus,
    gcfg: GlobalConfig,
    selection: str | None,
    *,
    reload: Callable[[], Config],
    clear: bool = False,
) -> UseResult:
    """`jailbee agent use`: validate, write the local layer, resync running containers.

    ``reload`` loads the config again, so the resync follows the layers as they
    now stand (a cleared key falls to the repo or global layer).
    """
    runner: Runner | None = None
    if not clear:
        runner = choose(cfg, incus, gcfg, selection)
        assert runner is not None  # only a container picker answers "repo default"
        found = problems(cfg, incus, gcfg, runner)
        if found:
            raise SelectionError("; ".join(found))
    write(cfg, runner)
    now = reload().orchestration_view().default_runner
    profile = now.profile if now is not None and now.agent == "claude-jb" else None
    return UseResult(runner, resync(cfg, incus, gcfg, profile))


def use_for_container(
    cfg: Config,
    incus: Incus,
    gcfg: GlobalConfig,
    container: str,
    selection: str | None,
    *,
    clear: bool = False,
) -> Runner | None:
    """`jailbee agent use --container`: set (or remove) the container's runner label.

    The label outranks `agents.default_runner` at the next autostart. Nothing is
    sent into the container: a running agent session keeps the agent it has.
    Returns the new label's runner, None when the container now follows the repo.
    """
    from jailbee.orchestration.labels import RUNNER

    runner = None if clear else choose(cfg, incus, gcfg, selection, container=container)
    if runner is None:
        incus.config_unset_checked(container, RUNNER)
        return None
    found = problems(cfg, incus, gcfg, runner)
    if found:
        raise SelectionError("; ".join(found))
    incus.config_set(container, RUNNER, runner.to_label())
    return runner


def container_agent(cfg: Config, incus: Incus, container: str) -> tuple[str, str] | None:
    """(`profile/model`, source) of the agent ``container`` launches next, or None.

    The source is ``container`` for its own label, else the layer of the repo's
    default runner. None when only the legacy `primary_agent` path applies.
    """
    from jailbee.dashboard.agent_choice import runner_label

    label = read_runner(incus, container)
    if label is not None:
        return runner_label(label), "container"
    view = cfg.orchestration_view()
    if view.default_runner is not None and view.default_runner_source is not None:
        return runner_label(view.default_runner), view.default_runner_source
    return None


def open_window(cfg: Config, incus: Incus, container: str) -> str:
    """Start the effective agent in a new tmux window of a running container.

    Returns the window's name. The window is new (never replaces a running
    agent's) and the choice is the one autostart would make.
    """
    from jailbee import tmux
    from jailbee.agents import primary_agent
    from jailbee.autostart import agent_autostart_steps
    from jailbee.lifecycle import container_repo_dir, list_containers
    from jailbee.orchestration.labels import child_env, orchestrator_env

    if not any(c.name == container and c.state == "Running" for c in list_containers(cfg, incus)):
        raise SelectionError(f"{container} is not running")
    runner = read_runner(incus, container)
    default = cfg.orchestration_view().default_runner
    name = "claude" if runner is not None or default is not None else primary_agent(cfg).name
    if name is None:
        raise SelectionError("no agent is enabled in this repo")
    extra_env = {**child_env(cfg, incus, container), **orchestrator_env(cfg, incus, container)}
    steps = agent_autostart_steps(
        cfg, extra_env=extra_env, runner=runner, default_runner=default, launch=name
    )
    step = next((s for s in steps if s.name == name), None)
    if step is None:
        raise SelectionError(f"agent {name!r} has no launch command")
    repo_dir = container_repo_dir(cfg, incus, container)
    tmux.ensure_session(incus, container, start_dir=repo_dir)
    return tmux.open_window(
        incus,
        container,
        name,
        step.run,
        {**cfg.autostart.env, **step.env, "REPO_DIR": repo_dir},
        repo_dir,
    )
