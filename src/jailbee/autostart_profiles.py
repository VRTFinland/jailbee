"""Named autostart selection and per-container attempted-stage state."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from jailbee.autostart_plan import normalize_stages, select_profile
from jailbee.config import Autostart, Config
from jailbee.config.models_agents import BUILTIN_PROFILES, AutostartProfile
from jailbee.incus import Incus
from jailbee.tui import warn

PROFILE_KEY = "user.jailbee.autostart_profile"
CREATED_KEY = "user.jailbee.autostart_created"
SERVICES_KEY = "user.jailbee.autostart_services"
PREPARED_KEY = "user.jailbee.services_prepared"


def encode_names(names: Sequence[str]) -> str:
    return ",".join(names) if names else ","


def decode_names(value: str | None) -> list[str] | None:
    return None if value is None else list(filter(None, value.split(",")))


class ProfileError(ValueError):
    """Invalid profile selection."""


def profile_names(autostart: Autostart) -> tuple[str, ...]:
    return (*autostart.profiles, *BUILTIN_PROFILES)


def validate_profile_name(autostart: Autostart, name: str) -> None:
    if name not in profile_names(autostart):
        raise ProfileError(f"autostart profile `{name}` is not defined")


def store_choice(
    cfg: Config,
    incus: Incus,
    container: str,
    *,
    profile: str | None,
    services: list[str] | None,
    reset_services: bool,
) -> None:
    from jailbee.create_flow import validate_service_names

    if services is not None and reset_services:
        raise ValueError("--services and --reset-services cannot be used together")
    if profile is not None:
        validate_profile_name(cfg.autostart, profile)
    validate_service_names(services)
    if reset_services:
        incus.config_unset_checked(container, SERVICES_KEY)
    if profile is not None:
        incus.config_set(container, PROFILE_KEY, profile)
    if services is not None:
        incus.config_set(container, SERVICES_KEY, encode_names(services))


def selection(autostart: Autostart, name: str | None) -> Literal["all"] | AutostartProfile:
    if name is None or name == "all":
        return "all"
    if name == "none":
        return AutostartProfile.model_construct(stages=[], services=[])
    if name not in autostart.profiles:
        raise ProfileError(
            f"autostart profile `{name}` is not defined; choose from: "
            + ", ".join(profile_names(autostart))
        )
    return autostart.profiles[name]


def resolve_profile(cfg: Config, incus: Incus, container: str) -> str | None:
    stored = incus.config_get(container, PROFILE_KEY)
    if stored in profile_names(cfg.autostart):
        return stored
    if stored:
        warn(
            f"autostart profile `{stored}` is not defined; using {cfg.autostart.default_profile!r}"
        )
    return cfg.autostart.default_profile


def profiled_config(cfg: Config, profile: str | None) -> Config:
    return cfg.model_copy(update={"autostart": select_profile(cfg.autostart, profile)})


def created_stages(incus: Incus, container: str) -> frozenset[str] | None:
    value = incus.config_get(container, CREATED_KEY)
    return None if value is None else frozenset(filter(None, value.split(",")))


def record_created(incus: Incus, container: str, stage: str) -> None:
    value = incus.config_get(container, CREATED_KEY) or ""
    names = list(filter(None, value.split(",")))
    if stage not in names:
        incus.config_set(container, CREATED_KEY, ",".join([*names, stage]))


def missing_on_create(cfg: Config, incus: Incus, container: str, profile: str | None) -> Autostart:
    selected = select_profile(cfg.autostart, profile)
    done = created_stages(incus, container)
    stages = (
        []
        if done is None
        else [s for s in normalize_stages(selected.on_create) if s.stage not in done]
    )
    return selected.model_copy(update={"on_create": stages, "on_start": []})


def show_profile(cfg: Config, incus: Incus, container: str) -> None:
    from jailbee.tui import info

    current = resolve_profile(cfg, incus, container)
    info(f"Current autostart profile: {current}")
    for name in profile_names(cfg.autostart):
        marks = []
        if name == current:
            marks.append("current")
        if name == cfg.autostart.default_profile:
            marks.append("default")
        info(f"  {name}" + (f" ({', '.join(marks)})" if marks else ""))
    done = created_stages(incus, container)
    info(
        "on_create attempted: "
        + ("all (legacy container)" if done is None else ", ".join(sorted(done)) or "none")
    )


@dataclass(frozen=True)
class SwitchResult:
    previous: str | None
    current: str
    ran_on_create: tuple[str, ...]
    ran_on_start: tuple[str, ...]
    deferred: bool


def switch_profile(
    cfg: Config,
    incus: Incus,
    container: str,
    name: str,
    *,
    run: bool,
    on_progress: Callable[[str, str, str], None] | None = None,
) -> SwitchResult:
    from jailbee.autostart import run_autostart
    from jailbee.autostart_plan import AutostartTrigger, selected_stage_names
    from jailbee.autostart_status import refuse_while_running
    from jailbee.lifecycle import container_repo_dir

    validate_profile_name(cfg.autostart, name)
    refuse_while_running(cfg, container)
    previous = resolve_profile(cfg, incus, container)
    override_services = decode_names(incus.config_get(container, SERVICES_KEY))

    def profile_services(profile: str | None) -> frozenset[str] | None:
        chosen = selection(cfg.autostart, profile)
        if chosen == "all" or chosen.services is None:
            return None
        return frozenset(chosen.services)

    old_services = profile_services(previous)
    new_services = profile_services(name)
    # The container's service graph may differ from the host's graph.
    services_added = (
        override_services is None and new_services != frozenset() and new_services != old_services
    )
    incus.config_set(container, PROFILE_KEY, name)
    running = any(
        c.get("name") == container and c.get("status") == "Running" for c in incus.list_containers()
    )
    if not run or not running:
        return SwitchResult(previous, name, (), (), True)
    missing = missing_on_create(cfg, incus, container, name)
    selected = select_profile(cfg.autostart, name)
    old_names = set(selected_stage_names(cfg.autostart, previous, AutostartTrigger.ON_START))
    added = [s for s in normalize_stages(selected.on_start) if s.stage not in old_names]
    creates = tuple(s.stage for s in normalize_stages(missing.on_create))
    starts = tuple(s.stage for s in added)
    if creates or starts or services_added:
        from jailbee.docker_daemon import mirror_endpoint_or_none

        mirror_endpoint = mirror_endpoint_or_none(cfg, incus)
        repo_dir = container_repo_dir(cfg, incus, container)
        for trigger, stages in (
            (AutostartTrigger.ON_CREATE, missing.on_create),
            (AutostartTrigger.ON_START, added),
        ):
            if stages:
                block = selected.model_copy(
                    update={"on_create": [], "on_start": [], trigger.value: stages}
                )
                # In-place switches never launch agents again or detach.
                # ON_START's generated agents are disabled on this derived copy.
                agents = {
                    key: value.model_copy(update={"autostart": False})
                    for key, value in cfg.agents.items()
                }
                run_cfg = cfg.model_copy(update={"autostart": block, "agents": agents})
                run_autostart(
                    run_cfg,
                    incus,
                    container,
                    trigger,
                    repo_dir=repo_dir,
                    override="wait",
                    on_progress=on_progress,
                    mirror_endpoint=mirror_endpoint,
                    skip_services=True,
                )
        if services_added:
            from jailbee.services.autostart import run_services_stage

            run_services_stage(
                cfg,
                incus,
                container,
                repo_dir,
                mirror_endpoint=mirror_endpoint,
                on_progress=on_progress,
                cas_restore=False,
                live_switch=True,
            )
    return SwitchResult(previous, name, creates, starts, not (creates or starts or services_added))
