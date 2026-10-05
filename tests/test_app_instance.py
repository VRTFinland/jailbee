"""Tests for moving a running GUI app to the launching display."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jailbee import prompting
from jailbee.app_instance import (
    AppMoveError,
    decide_move,
    display_name,
    ensure_on_this_display,
    same_display,
)
from jailbee.apps import AppSpec, SingletonSpec
from jailbee.gui import SHARED_WAYLAND_SOCKET
from jailbee.incus import IncusError, RunningInstance
from tests.conftest import make_cfg

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


SPEC = AppSpec(
    name="chrome",
    command=["/opt/google/chrome/google-chrome"],
    singleton=SingletonSpec(
        lock="~/.config/google-chrome/SingletonLock",
        exe_names=("chrome",),
        restore_args=("--restore-last-session",),
    ),
)
ON_HOST = RunningInstance(42, "wayland-1", ":0")


@pytest.mark.parametrize("interactive", [True, False])
@pytest.mark.parametrize("flag", [True, False])
def test_an_explicit_flag_wins_without_asking(mocker, flag, interactive):
    mocker.patch("jailbee.prompting.is_interactive", return_value=interactive)
    ask = mocker.patch("jailbee.prompting._confirm")
    assert decide_move(flag, "Move?") is flag
    ask.assert_not_called()


@pytest.mark.parametrize("answer", [True, False])
def test_a_terminal_is_asked(mocker, answer):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    ask = mocker.patch("jailbee.prompting._confirm", return_value=answer)
    assert decide_move(None, "Move?") is answer
    ask.assert_called_once_with("Move?", True)


def test_without_a_terminal_the_app_moves(mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    ask = mocker.patch("jailbee.prompting._confirm")
    assert decide_move(None, "Move?") is True
    ask.assert_not_called()


def _incus(*instances):
    incus = MagicMock()
    incus.running_instance.side_effect = list(instances)
    return incus


def test_no_running_instance_closes_nothing(tmp_path):
    incus = _incus(None)
    moved = ensure_on_this_display(
        make_cfg(tmp_path), incus, "c1", SPEC, SHARED, move=True, sleep_fn=lambda s: None
    )
    assert moved is False
    incus.exec.assert_not_called()


def test_the_lock_glob_is_expanded_for_the_container_user(tmp_path):
    incus = _incus(None)
    cfg = make_cfg(tmp_path)
    ensure_on_this_display(cfg, incus, "c1", SPEC, SHARED, move=True, sleep_fn=lambda s: None)
    args, kwargs = incus.running_instance.call_args
    assert args[1] == "/home/dev/.config/google-chrome/SingletonLock"
    assert list(args[2]) == ["chrome"]
    assert kwargs == {"uid": cfg.container_user.uid, "gid": cfg.container_user.gid}


def test_the_same_display_closes_nothing(tmp_path):
    incus = _incus(ON_HOST)
    moved = ensure_on_this_display(
        make_cfg(tmp_path), incus, "c1", SPEC, HOST, move=True, sleep_fn=lambda s: None
    )
    assert moved is False
    incus.exec.assert_not_called()


def test_declining_keeps_the_old_window_and_says_where(tmp_path, mocker, capsys):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._confirm", return_value=False)
    incus = _incus(ON_HOST)
    moved = ensure_on_this_display(
        make_cfg(tmp_path), incus, "c1", SPEC, SHARED, move=None, sleep_fn=lambda s: None
    )
    assert moved is False
    incus.exec.assert_not_called()
    assert "Chrome is open on the host display; the window opens there." in capsys.readouterr().out


def test_a_move_terms_the_pid_and_waits_for_it(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    incus = _incus(ON_HOST, ON_HOST, ON_HOST, None)
    sleeps: list[float] = []
    moved = ensure_on_this_display(
        cfg, incus, "c1", SPEC, SHARED, move=True, sleep_fn=sleeps.append
    )
    assert moved is True
    incus.exec.assert_called_once_with(
        "c1", ["kill", "-TERM", "42"], uid=cfg.container_user.uid, gid=cfg.container_user.gid
    )
    assert sleeps == [0.25, 0.25]
    assert "Moving chrome from the host display" in capsys.readouterr().out


def test_a_process_gone_before_the_kill_still_counts_as_closed(tmp_path):
    # Review Focus 3: it exited between detection and `kill`.
    incus = _incus(ON_HOST, None)
    incus.exec.side_effect = IncusError("kill: (42) - No such process")
    moved = ensure_on_this_display(
        make_cfg(tmp_path), incus, "c1", SPEC, SHARED, move=True, sleep_fn=lambda s: None
    )
    assert moved is True


def test_an_instance_that_does_not_close_is_an_error(tmp_path):
    incus = _incus(*([ON_HOST] * 62))
    sleeps: list[float] = []
    with pytest.raises(AppMoveError, match="Chrome did not close on the host display within 15 s"):
        ensure_on_this_display(
            make_cfg(tmp_path), incus, "c1", SPEC, SHARED, move=True, sleep_fn=sleeps.append
        )
    assert len(sleeps) == 60


def test_cancelling_the_prompt_closes_nothing(tmp_path, mocker):
    # Review Focus 4.
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._confirm", return_value=None)
    incus = _incus(ON_HOST)
    with pytest.raises(prompting.Cancelled):
        ensure_on_this_display(
            make_cfg(tmp_path), incus, "c1", SPEC, SHARED, move=None, sleep_fn=lambda s: None
        )
    incus.exec.assert_not_called()
