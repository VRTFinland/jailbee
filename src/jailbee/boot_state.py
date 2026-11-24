"""Volatile per-boot decisions for host-wide resume detection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from jailbee.incus import IncusError
from jailbee.tui import warn_plain

if TYPE_CHECKING:
    from jailbee.incus import Incus

MARKER = "/run/jailbee-autostart-state"


def record(incus: Incus, name: str, decision: Literal["started", "handled", "skipped"]) -> bool:
    """Best-effort boot state; an unfinished start stays eligible for resume."""
    try:
        incus.exec(
            name,
            ["bash", "-c", f"printf '%s' {decision} > {MARKER} && chmod 644 {MARKER}"],
        )
    except (IncusError, OSError) as exc:
        warn_plain(f"{name}: cannot record autostart boot state (continuing): {exc}")
        return False
    return True
