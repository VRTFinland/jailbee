"""Shared helpers for per-container memory-limit overrides."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from jailbee.incus import Incus

LIMIT_KEY = "limits.memory"
DEFAULT_WORD = "default"
OTHER_VALUE = "other"
PRESETS: tuple[str, ...] = ("4GiB", "8GiB", "16GiB", "24GiB", "32GiB")
_GIB = 1024**3
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


def set_limit(incus: Incus, name: str, size: str | None) -> None:
    """Set an instance memory override, or clear it to follow the profile."""
    if size is None:
        incus.config_unset_checked(name, LIMIT_KEY)
    else:
        incus.config_set(name, LIMIT_KEY, size)


def migrate_pinned(
    incus: Incus,
    raws: Sequence[dict[str, Any]],
    *,
    default: str,
    base_profile: str,
) -> list[str]:
    """Remove instance caps equal to the default for matching base-profile rows."""
    migrated: list[str] = []
    for raw in raws:
        if base_profile not in (raw.get("profiles") or []):
            continue
        config = raw.get("config") or {}
        value = config.get(LIMIT_KEY)
        if isinstance(value, str) and same_size(value, default):
            name = raw.get("name")
            if isinstance(name, str):
                incus.config_unset_checked(name, LIMIT_KEY)
                migrated.append(name)
    return migrated
