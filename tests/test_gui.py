"""Tests for GUI launch primitives: `gui_env`, `launch_detached`, and the
host's Wayland compositor socket name.

App-specific launch behavior (Chrome/Firefox/JetBrains specs, URL handling,
dark mode, pool allocation) is tested at the registry level in
`test_apps.py`, `test_browsers.py`, and `test_ide.py` — `gui.py` no longer
knows about any application by name. `open_ide`/`open_chrome` were removed
in Task 14; `cli.py` now reads the registry (`apps.get_app`/`apps.launch`)
directly.
"""

from __future__ import annotations

import subprocess as sp

from jailbee.gui import gui_env, host_wayland_socket, launch_detached


def test_gui_env_sets_home_for_the_container_user(tmp_path):
    """Incus exec --user <uid> doesn't auto-set HOME, so apps that depend on
    it (~/.config, ~/.local) fail without an explicit HOME env var.
    """
    from jailbee.config import CONTAINER_USERNAME
    from tests.conftest import make_cfg

    env = gui_env(make_cfg(tmp_path))
    assert env["HOME"] == f"/home/{CONTAINER_USERNAME}"


def test_gui_env_sets_user_and_logname_for_the_container_user(tmp_path):
    """`incus exec --user <uid>` runs the process directly, not through
    `login`/PAM, so nothing else sets USER/LOGNAME — a GUI app or plain
    shell command reading either sees it empty without this. `profiles.py`'s
    base profile injects the display vars but never these two, so `gui_env`
    is the only place that can supply them.
    """
    from jailbee.config import CONTAINER_USERNAME
    from tests.conftest import make_cfg

    env = gui_env(make_cfg(tmp_path))
    assert env["USER"] == CONTAINER_USERNAME
    assert env["LOGNAME"] == CONTAINER_USERNAME


def test_launch_detached_passes_env_as_incus_env_flags(mocker):
    popen = mocker.patch("jailbee.gui.subprocess.Popen")
    launch_detached("c1", 1000, {"HOME": "/home/dev", "DISPLAY": ":0"}, "/bin/true", "/tmp/x.log")

    argv = popen.call_args.args[0]
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert pairs == ["HOME=/home/dev", "DISPLAY=:0"]


def test_launch_detached_redirects_to_the_log_file(mocker):
    """stdout+stderr go to the per-app log, not to /dev/null — the user needs
    to be able to read them to diagnose why a GUI failed to appear (Wayland
    sockets not visible, missing libs, crash on startup, etc.). stdin is
    closed (`</dev/null`) and the inner process is `setsid`-detached so it
    survives the launcher's own bash exiting, and the whole thing is
    backgrounded (`&`) so `incus exec` returns immediately. Each of these
    four was, until this test, only asserted through the now-deleted
    `open_ide`/`open_chrome` tests — dropping any one of them ships green
    without this.
    """
    popen = mocker.patch("jailbee.gui.subprocess.Popen")
    launch_detached("c1", 1000, {}, "/bin/true", "/tmp/jailbee-app-x.log")
    script = popen.call_args.args[0][-1]
    assert ">/tmp/jailbee-app-x.log" in script
    assert "2>&1" in script
    assert ">/dev/null" not in script
    assert "</dev/null" in script  # stdin only
    assert "setsid" in script
    assert script.rstrip().endswith("&")


def test_launch_detached_fully_detaches_from_parent(mocker):
    """Without start_new_session + DEVNULL stdio, the child `incus exec`
    shares jailbee's TTY. When jailbee exits the terminal is left in a broken
    state (`reset` needed) and SIGHUP propagation kills the GUI before it
    appears. Verify both detach knobs are set.
    """
    popen = mocker.patch("jailbee.gui.subprocess.Popen")
    launch_detached("c1", 1000, {}, "/bin/true", "/tmp/x.log")

    kw = popen.call_args.kwargs
    assert kw.get("start_new_session") is True
    assert kw.get("stdin") == sp.DEVNULL
    assert kw.get("stdout") == sp.DEVNULL
    assert kw.get("stderr") == sp.DEVNULL


def test_host_wayland_socket_reads_the_env_var(monkeypatch):
    """The socket name is per-session, not a constant: Hyprland and Sway
    commonly hand out wayland-1 (#17).
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")

    assert host_wayland_socket() == "wayland-1"


def test_host_wayland_socket_falls_back_to_wayland_0_when_unset(monkeypatch):
    """A non-graphical context (a systemd autostart run, a cron job) has no
    WAYLAND_DISPLAY. The near-universal default keeps the base profile's
    advertised value stable there rather than empty.
    """
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    assert host_wayland_socket() == "wayland-0"


def test_gui_module_no_longer_exports_the_old_launchers():
    import jailbee.gui as gui

    assert not hasattr(gui, "open_chrome")
    assert not hasattr(gui, "open_ide")
