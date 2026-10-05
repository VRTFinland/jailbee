"""Tests for moving a running GUI app to the launching display."""

from __future__ import annotations

import pytest

from jailbee.app_instance import display_name, same_display
from jailbee.gui import SHARED_WAYLAND_SOCKET
from jailbee.incus import RunningInstance

HOST = {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"}
SHARED = {"WAYLAND_DISPLAY": SHARED_WAYLAND_SOCKET}
# Real shape: `{SHARED_DISPLAY_DIR}/wp-<session>-<container>` (waypipe.server_name).
WAYPIPE_A = {"WAYLAND_DISPLAY": "/run/jailbee-display/wp-aaaa-c1"}
WAYPIPE_B = {"WAYLAND_DISPLAY": "/run/jailbee-display/wp-bbbb-c1"}


def _running(env: dict[str, str]) -> RunningInstance:
    return RunningInstance(1, env.get("WAYLAND_DISPLAY"), env.get("DISPLAY"))


@pytest.mark.parametrize(
    ("was", "now", "same"),
    [
        (HOST, HOST, True),
        (SHARED, SHARED, True),
        (WAYPIPE_A, WAYPIPE_A, True),
        (HOST, SHARED, False),
        (SHARED, HOST, False),
        (HOST, WAYPIPE_A, False),
        (SHARED, WAYPIPE_A, False),
        (WAYPIPE_A, WAYPIPE_B, False),
    ],
)
def test_same_display(was, now, same):
    assert same_display(_running(was), now) is same


def test_an_x11_host_compares_display_too():
    # On an X11 host Chrome draws on DISPLAY; WAYLAND_DISPLAY is only the
    # unused `wayland-0` fallback and equal on both sides.
    running = RunningInstance(1, "wayland-0", ":1")
    assert same_display(running, {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}) is False
    assert same_display(running, {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":1"}) is True


@pytest.mark.parametrize(
    ("env", "name"),
    [(HOST, "host"), (SHARED, "shared RDP"), (WAYPIPE_A, "waypipe"), ({}, "host")],
)
def test_display_name(env, name):
    assert display_name(_running(env)) == name
