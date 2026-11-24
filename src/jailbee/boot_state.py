"""Volatile per-boot decisions for host-wide resume detection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from jailbee.incus import IncusError
from jailbee.tui import warn_plain

if TYPE_CHECKING:
    from jailbee.incus import Incus

MARKER = "/run/jailbee-autostart-state"


def record(incus: Incus, name: str, decision: Literal["started", "handled", "skipped"]) -> bool:
    """Best-effort boot state; an unfinished start stays eligible for resume.

    Complete only a recorded start. A detached continuation then avoids a
    second failed write (and warning) when the initial write failed.
    """
    guard = (
        f'if [ "$(cat {MARKER} 2>/dev/null)" != started ]; then exit 0; fi; '
        if decision == "handled"
        else ""
    )
    script = (
        guard
        + f"temporary=$(mktemp {MARKER}.XXXXXX) || exit 1; "
        + "trap 'rm -f \"$temporary\"' EXIT; "
        + f"printf '%s' {decision} > \"$temporary\" "
        + f'&& chmod 644 "$temporary" && mv -f "$temporary" {MARKER}'
    )
    try:
        incus.exec(name, ["bash", "-c", script])
    except (IncusError, OSError) as exc:
        warn_plain(f"{name}: cannot record autostart boot state (continuing): {exc}")
        return False
    return True
