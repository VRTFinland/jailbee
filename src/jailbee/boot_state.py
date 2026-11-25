"""Volatile per-boot decisions for host-wide resume detection."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal

from jailbee.incus import IncusError
from jailbee.tui import warn_plain

if TYPE_CHECKING:
    from jailbee.incus import Incus

MARKER = "/run/jailbee-autostart-state"


def start_identity(raw: object) -> str | None:
    """Validated Incus last_used_at, preserving its nanosecond start identity."""
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return raw if parsed.year > 1 and parsed.tzinfo is not None else None


def current_start(incus: Incus, name: str) -> str | None:
    """Best-effort identity captured before any boot setup is run."""
    try:
        instances = incus.list_containers()
    except (IncusError, OSError):
        return None
    for raw in instances:
        if raw.get("name") == name and raw.get("status") == "Running":
            return start_identity(raw.get("last_used_at"))
    return None


def record(
    incus: Incus, name: str, decision: Literal["started", "handled", "skipped"],
    *, expected_start: str | None = None,
) -> bool:
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
        + f'&& chmod 644 "$temporary" && mv -f "$temporary" {MARKER} '
        + "&& printf recorded"
    )
    try:
        # Capture before the guest write so a concurrent restart cannot make
        # completion of the old boot acknowledge the new start.
        identity = current_start(incus, name) if decision != "started" else None
        if expected_start is not None and identity != expected_start:
            return False
        written = incus.exec(name, ["bash", "-c", script])
        if identity is not None and written == "recorded":
            from jailbee.host_resume import remember_boot

            remember_boot(name, identity)
    except (IncusError, OSError, ValueError) as exc:
        warn_plain(f"{name}: cannot record autostart boot state (continuing): {exc}")
        return False
    return True
