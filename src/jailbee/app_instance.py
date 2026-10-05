"""Moving a running GUI app to the display it is launched from.

An app with a profile lock (`AppSpec.singleton`) forwards a second launch to
its running instance, which can only draw where it started. This module
finds out whether that instance is on another display and, if the user
wants, closes it so the launch can start it again here. Container commands
go through the `Incus` wrapper; no `subprocess` here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.gui import SHARED_DISPLAY_DIR, SHARED_WAYLAND_SOCKET

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jailbee.incus import RunningInstance


def same_display(running: RunningInstance, env: Mapping[str, str]) -> bool:
    """Whether a launch with ``env`` would draw where ``running`` draws.

    ``DISPLAY`` is compared only when the launch sets it (the host target):
    on an X11 host it is the display Chrome actually uses.
    """
    if running.wayland_display != env.get("WAYLAND_DISPLAY"):
        return False
    return "DISPLAY" not in env or running.display == env["DISPLAY"]


def display_name(running: RunningInstance) -> str:
    """The display ``running`` is on, as the user knows it."""
    wayland = running.wayland_display or ""
    if wayland == SHARED_WAYLAND_SOCKET:
        return "shared RDP"
    if wayland.startswith(f"{SHARED_DISPLAY_DIR}/"):
        return "waypipe"
    return "host"
