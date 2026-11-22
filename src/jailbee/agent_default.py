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

from pydantic import ValidationError

from jailbee import litellm
from jailbee.config_writer import DELETE, YamlChange, patch_local_file
from jailbee.incus import IncusError
from jailbee.orchestration.runner import read_runner, runner_problems
from jailbee.orchestration.runner_model import RUNNER_TIERS, Runner, model_tier

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

PLAIN = "claude"
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
    profile, _, model = text.strip().partition("/")
    if not profile or "/" in model:
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
    value: object = DELETE if runner is None else runner.model_dump(mode="json", exclude_none=True)
    patch_local_file(cfg.container_prefix, [YamlChange(KEY_PATH, value)])


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
        if (runner := read_runner(incus, info.name)) is not None
    )
    if view.default_runner is not None and view.default_runner_source is not None:
        return Effective(view.default_runner, view.default_runner_source, labels=labels)
    agent = primary_agent(cfg).name or "none"
    via = "claude-jb (litellm.autostart)" if litellm_autostart_on(cfg) else agent
    default = cfg.litellm_view().config.default_profile
    if litellm_autostart_on(cfg):
        via += f", profile {default}"
    return Effective(None, "legacy", legacy=via, labels=labels)


_DEFAULT_TIER = ""  # picker sentinel: a `None` value would read as a cancel


def choose(cfg: Config, incus: Incus, gcfg: GlobalConfig, selection: str | None) -> Runner:
    """The runner to write: parsed from `selection`, else asked for (profile, then tier)."""
    from jailbee import prompting
    from jailbee.prompting import Option

    offered = catalog(cfg, incus, gcfg)
    if selection is not None:
        profile, model = parse_selection(selection)
        return build_runner(profile, model)
    profile = prompting.choose_one(
        "agent profile",
        [
            Option(
                name,
                name,
                name,
                "plain Claude" if name == PLAIN else "claude-jb through this LiteLLM profile",
            )
            for name in offered
        ],
        question="Default agent profile for this repo's containers:",
    )
    tiers = offered[profile]
    tier = prompting.choose_one(
        "model tier",
        [
            Option(_DEFAULT_TIER, "profile default", "default", "the profile's own default tier"),
            *(Option(t, t, t) for t in tiers),
        ],
        question=f"Model tier for {profile}:",
        always_ask=True,
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
        found = problems(cfg, incus, gcfg, runner)
        if found:
            raise SelectionError("; ".join(found))
    write(cfg, runner)
    now = reload().orchestration_view().default_runner
    profile = now.profile if now is not None and now.agent == "claude-jb" else None
    return UseResult(runner, resync(cfg, incus, gcfg, profile))
