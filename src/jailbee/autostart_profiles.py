"""Named autostart selection and per-container attempted-stage state."""

from __future__ import annotations

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
        warn(f"autostart profile `{stored}` is not defined; using {cfg.autostart.default_profile!r}")
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
    stages = [] if done is None else [s for s in normalize_stages(selected.on_create) if s.stage not in done]
    return selected.model_copy(update={"on_create": stages, "on_start": []})
