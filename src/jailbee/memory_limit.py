"""Shared helpers for per-container memory-limit overrides."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

from jailbee.incus import Incus

if TYPE_CHECKING:
    from jailbee.config import Config

LIMIT_KEY = "limits.memory"
DEFAULT_WORD = "default"
OTHER_VALUE = "other"
PRESETS: tuple[str, ...] = ("4GiB", "8GiB", "16GiB", "24GiB", "32GiB")
_PERCENT_RE = re.compile(r"(\d+)%")


def _parse_size(text: str) -> int | None:
    # Keep the existing Incus size parser as the single source of truth while
    # avoiding a module-level lifecycle import cycle.
    from jailbee.lifecycle import _parse_memory_limit

    return _parse_memory_limit(text)


def validate_size(text: str) -> str | None:
    """Return a validation error, or None for a positive Incus size / 1-100%."""
    percentage = _PERCENT_RE.fullmatch(text.strip())
    if percentage is not None and 1 <= int(percentage.group(1)) <= 100:
        return None
    if _parse_size(text) is not None:
        return None
    return "Enter a positive Incus memory size (for example 8GiB) or a percentage from 1% to 100%."


def same_size(a: str, b: str) -> bool:
    """Compare fixed sizes by bytes, falling back to exact trimmed text."""
    parsed_a = _parse_size(a)
    parsed_b = _parse_size(b)
    if parsed_a is not None and parsed_b is not None:
        return parsed_a == parsed_b
    return a.strip() == b.strip()


@dataclass(frozen=True)
class SizeOption:
    label: str
    value: str
    current: bool = False


def size_options(
    *, current: str | None, override: bool, default: str, host_total: int | None
) -> list[SizeOption]:
    """Build picker rows, including a custom current override when necessary."""
    rows = [SizeOption(f"Default ({default})", DEFAULT_WORD, current=not override)]
    visible_presets = [
        preset
        for preset in PRESETS
        if host_total is None or (_parse_size(preset) or 0) <= host_total
    ]
    preset_row = next(
        (
            preset
            for preset in visible_presets
            if override and current and same_size(preset, current)
        ),
        None,
    )
    custom_row: SizeOption | None = None
    if override and current and preset_row is None:
        custom_row = SizeOption(current, current, current=True)

    for preset in visible_presets:
        if custom_row is not None:
            custom_bytes = _parse_size(custom_row.value)
            preset_bytes = _parse_size(preset)
            if (
                custom_bytes is not None
                and preset_bytes is not None
                and custom_bytes < preset_bytes
            ):
                rows.append(custom_row)
                custom_row = None
        rows.append(SizeOption(preset, preset, current=preset == preset_row))
    if custom_row is not None:
        rows.append(custom_row)
    rows.append(SizeOption("Other…", OTHER_VALUE))
    return rows


def below_usage(size: str, usage: int | None) -> bool:
    """Whether a fixed memory cap is lower than known current use."""
    parsed = _parse_size(size)
    return parsed is not None and usage is not None and parsed < usage


def profile_limit(
    incus: Incus, profiles: Sequence[str], *, cache: dict[str, str | None] | None = None
) -> str | None:
    """Resolve profile-only memory in Incus order; an empty value removes a cap.

    Checked wrapper reads intentionally propagate failures, rather than treating
    an unreadable profile as an absent limit.
    """
    values = cache if cache is not None else {}
    target: str | None = None
    for profile in profiles:
        if profile not in values:
            raw = yaml.safe_load(incus.profile_show(profile))
            if not isinstance(raw, dict) or not isinstance(raw.get("config", {}), dict):
                raise ValueError(f"Cannot read memory config from profile {profile}")
            config = raw.get("config", {})
            value = config.get(LIMIT_KEY)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Invalid memory limit in profile {profile}")
            values[profile] = value
        value = values[profile]
        if value is not None:
            target = value
    return target if target and validate_size(target) is None else None


def inherited_limit(incus: Incus, name: str) -> str | None:
    """The actual post-unset cap, ignoring the instance-local override."""
    raw = yaml.safe_load(incus.config_show(name))
    if not isinstance(raw, dict):
        raise ValueError(f"Cannot read profiles for {name}")
    profiles = raw.get("profiles", [])
    if not isinstance(profiles, list) or not all(isinstance(p, str) for p in profiles):
        raise ValueError(f"Cannot read profiles for {name}")
    return profile_limit(incus, profiles)


def set_limit(incus: Incus, name: str, size: str | None) -> None:
    """Set an instance memory override, or clear it to follow the profile."""
    if size is None:
        incus.config_unset_checked(name, LIMIT_KEY)
    else:
        incus.config_set(name, LIMIT_KEY, size)


def run_mem(
    cfg: Config,
    incus: Incus,
    name: str,
    size: str,
    *,
    force: bool,
    confirm: Callable[[str], bool],
    interactive: bool,
    confirmed_target: str | None = None,
) -> str:
    """Change an override after checking the effective target against current use."""
    from jailbee.lifecycle import list_containers, short_name
    from jailbee.prompting import Cancelled

    clear = size == DEFAULT_WORD
    target = inherited_limit(incus, name) if clear else size
    if target is None:
        raise ValueError(
            "Cannot clear memory override without an inherited cap; run `jb apply` first."
        )
    if confirmed_target is not None and not same_size(target, confirmed_target):
        raise ValueError(
            f"Memory target changed from {confirmed_target} to {target}; select it again."
        )
    if message := validate_size(target):
        raise ValueError(message)
    container = next((row for row in list_containers(cfg, incus) if row.name == name), None)
    if container is None:
        raise ValueError(f"Container {name} is not in the current repository")
    if below_usage(target, container.memory_usage) and not force:
        warning = (
            f"{short_name(cfg, name)}: memory limit {target} is below current usage; "
            "the kernel will reclaim memory and may OOM-kill processes"
        )
        if not interactive:
            raise ValueError(f"{warning}; use --force to continue")
        if not confirm(f"{warning}. Continue?"):
            raise Cancelled()
    if clear:
        latest = inherited_limit(incus, name)
        if latest is None or not same_size(latest, target):
            raise ValueError("Inherited memory target changed; select it again before clearing.")
    set_limit(incus, name, None if clear else size)
    description = f"follows the default ({target})" if clear else f"{size} (override)"
    return f"{short_name(cfg, name)}: memory limit {description}"


def migrate_pinned(
    incus: Incus,
    raws: Sequence[dict[str, Any]],
    *,
    default: str,
    base_profile: str,
) -> list[str]:
    """Remove instance caps equal to the default for matching base-profile rows."""
    migrated: list[str] = []
    cache: dict[str, str | None] = {}
    for raw in raws:
        if base_profile not in (raw.get("profiles") or []):
            continue
        config = raw.get("config") or {}
        value = config.get(LIMIT_KEY)
        if isinstance(value, str) and same_size(value, default):
            inherited = profile_limit(incus, raw["profiles"], cache=cache)
            if inherited is None or not same_size(inherited, default):
                continue
            name = raw.get("name")
            if isinstance(name, str):
                incus.config_unset_checked(name, LIMIT_KEY)
                migrated.append(name)
    return migrated
