"""Volatile per-boot decisions for host-wide resume detection."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from jailbee.incus import Incus

MARKER = "/run/jailbee-autostart-state"


def record(incus: Incus, name: str, decision: Literal["handled", "skipped"]) -> None:
    """Record successful setup or an intentional skip; /run clears on reboot."""
    incus.exec(
        name,
        ["bash", "-c", f"printf '%s' {decision} > {MARKER} && chmod 644 {MARKER}"],
    )
