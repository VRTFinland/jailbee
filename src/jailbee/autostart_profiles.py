"""Named autostart selection and per-container attempted-stage state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from jailbee.autostart_plan import normalize_stages, select_profile
from jailbee.config import Autostart, Config
from jailbee.incus import Incus
from jailbee.tui import warn

PROFILE_KEY = "user.jailbee.autostart_profile"
CREATED_KEY = "user.jailbee.autostart_created"


class ProfileError(ValueError):
    """Invalid profile selection."""


def validate_profile_name(autostart: Autostart, name: str) -> None:
    if not autostart.profiles:
        raise ProfileError("this repository defines no autostart profiles")
    if name not in autostart.profiles:
        raise ProfileError(f"autostart profile `{name}` is not defined")


def resolve_profile(cfg: Config, incus: Incus, container: str) -> str | None:
    if not cfg.autostart.profiles:
        return None
    stored = incus.config_get(container, PROFILE_KEY)
    if stored in cfg.autostart.profiles:
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


def run_profile_start(cfg: Config, incus: Incus, container: str) -> None:
    """Foreground boot continuation for the broker (which has no CLI post-boot hook)."""
    from jailbee.autostart import run_autostart
    from jailbee.autostart_plan import AutostartTrigger
    from jailbee.lifecycle import container_repo_dir

    profile = resolve_profile(cfg, incus, container)
    missing = missing_on_create(cfg, incus, container, profile)
    repo_dir = container_repo_dir(cfg, incus, container)
    if missing.on_create:
        run_autostart(
            cfg.model_copy(update={"autostart": missing}),
            incus,
            container,
            AutostartTrigger.ON_CREATE,
            repo_dir,
            override="wait",
        )
    run_autostart(
        profiled_config(cfg, profile),
        incus,
        container,
        AutostartTrigger.ON_START,
        repo_dir,
        override="wait",
    )


def show_profile(cfg: Config, incus: Incus, container: str) -> None:
    from jailbee.tui import info

    current = resolve_profile(cfg, incus, container)
    info(f"Current autostart profile: {current}")
    for name in cfg.autostart.profiles:
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
    if creates or starts:
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
                )
    return SwitchResult(previous, name, creates, starts, not (creates or starts))
