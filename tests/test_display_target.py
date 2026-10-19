"""The display target: host by default, shared only for a GUI-enabled SSH session."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.gui import (
    SHARED_DISPLAY_DIR,
    SHARED_WAYLAND_SOCKET,
    display_state_dir,
    display_target,
    gui_env,
)
from jailbee.remote_ssh.session import (
    SSH_GUI_ENV,
    WaypipeSession,
    child_environment,
    is_shared_display_session,
    shared_display_port,
    waypipe_session,
)
from tests.conftest import make_cfg


def test_gui_env_without_a_target_is_the_host_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("DISPLAY", ":1")
    cfg = make_cfg(tmp_path)
    uid = cfg.container_user.uid

    env = gui_env(cfg)

    assert env["WAYLAND_DISPLAY"] == "wayland-1"
    assert env["DISPLAY"] == ":1"
    assert env["XDG_RUNTIME_DIR"] == f"/run/user/{uid}"
    assert set(env) == {"HOME", "USER", "LOGNAME", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DISPLAY"}
    assert gui_env(cfg, "host") == env


def test_shared_target_points_at_the_shared_socket_and_has_no_x11(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", ":1")
    cfg = make_cfg(tmp_path)

    env = gui_env(cfg, "shared")

    assert env["WAYLAND_DISPLAY"] == SHARED_WAYLAND_SOCKET
    assert "DISPLAY" not in env
    assert env["XDG_RUNTIME_DIR"] == f"/run/user/{cfg.container_user.uid}"
    assert SHARED_WAYLAND_SOCKET.startswith(SHARED_DISPLAY_DIR + "/")


def test_display_target_is_host_unless_the_session_is_marked() -> None:
    assert display_target({}) == "host"
    assert display_target({"JAILBEE_REMOTE_SSH": "1"}) == "host"
    assert display_target({SSH_GUI_ENV: "8022"}) == "host"  # not an SSH session
    assert display_target({"JAILBEE_SSH_SESSION": "1", SSH_GUI_ENV: "8022"}) == "shared"


def test_display_state_dir_lives_under_the_jailbee_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert display_state_dir() == tmp_path / "jailbee" / "display"


def test_child_environment_marks_a_gui_session_with_its_port() -> None:
    env = child_environment({}, gui_port=8022)

    assert env[SSH_GUI_ENV] == "8022"
    assert is_shared_display_session(env) is True
    assert shared_display_port(env) == 8022


def test_child_environment_never_inherits_the_gui_markers() -> None:
    """A marker in the server's own environment must not leak."""
    env = child_environment({SSH_GUI_ENV: "8022"})

    assert SSH_GUI_ENV not in env
    assert is_shared_display_session(env) is False
    assert shared_display_port(env) is None


def test_a_non_numeric_gui_marker_is_not_a_session() -> None:
    assert shared_display_port({SSH_GUI_ENV: "yes"}) is None
    assert is_shared_display_session({"JAILBEE_SSH_SESSION": "1", SSH_GUI_ENV: "yes"}) is False


def test_a_unicode_digit_marker_is_not_a_port() -> None:
    """`"²".isdigit()` is True but `int("²")` raises."""
    assert shared_display_port({SSH_GUI_ENV: "²"}) is None


def test_a_waypipe_session_needs_no_shared_display_port():
    env = {
        "JAILBEE_SSH_SESSION": "1",
        "JAILBEE_WAYPIPE_SESSION": "0a1b2c3d",
        "JAILBEE_WAYPIPE_COMPRESS": "lz4",
    }

    assert waypipe_session(env) == WaypipeSession(id="0a1b2c3d", compress="lz4")
    assert display_target(env) == "waypipe"


def test_waypipe_markers_outside_an_ssh_session_mean_nothing():
    env = {"JAILBEE_WAYPIPE_SESSION": "0a1b2c3d", "JAILBEE_WAYPIPE_COMPRESS": "lz4"}

    assert waypipe_session(env) is None
