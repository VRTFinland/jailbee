"""GUI launch primitives shared by every registry app.

`gui.py` knows about no application by name — see `apps.py` for the
registry (`AppSpec`, `get_app`, `launch`) that resolves what to run and
which container path each app lives at.
"""

from __future__ import annotations

import os
import shlex
import subprocess

from jailbee.config import CONTAINER_USERNAME, Config


def gui_env(cfg: Config) -> dict[str, str]:
    """Environment vars for GUI apps inside the container.

    HOME must be set explicitly: ``incus exec --user <uid>`` doesn't read
    /etc/passwd to derive it, so without this apps see ``HOME=`` and
    fail (Chrome can't write its profile, JetBrains can't find its
    config, etc.).
    """
    uid = cfg.container_user.uid
    return {
        "HOME": f"/home/{CONTAINER_USERNAME}",
        "WAYLAND_DISPLAY": host_wayland_socket(),
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
    }


def host_is_wayland() -> bool:
    """Return True if the host session is Wayland-native.

    Used to pass --ozone-platform=wayland to Chrome and to decide whether
    to bind-mount the host's Wayland socket into containers (see
    ``runtime_mounts``). On X11 hosts there is no compositor socket to
    mount; Chrome falls back to its auto-detect (DISPLAY) and other GUI
    apps follow suit.
    """
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def host_wayland_socket() -> str:
    """Name of the host's Wayland display socket, per ``$WAYLAND_DISPLAY``.

    The single source of truth for that name across jailbee: the GUI launch
    environment, the socket bind-mount in ``runtime_mounts`` and the base
    profile's ``environment.WAYLAND_DISPLAY`` must all agree, or the
    container is handed one socket and told to use another. A hardcoded
    ``wayland-0`` broke every create on compositors that number sessions
    differently — Hyprland and Sway commonly export ``wayland-1``.

    Falls back to ``wayland-0``, the near-universal default, when the
    variable is unset: that is what a non-graphical context (a systemd
    autostart run, a cron job) sees, and an empty value would be worse
    than a stale guess.

    The Wayland spec also allows an absolute path here, in which case the
    socket lives outside ``$XDG_RUNTIME_DIR`` entirely; callers that build
    a host path from this name check that the result exists.
    """
    return os.environ.get("WAYLAND_DISPLAY") or "wayland-0"


def launch_detached(
    container: str,
    uid: int,
    env: dict[str, str],
    inner_cmd: str,
    log_path: str,
    *,
    cwd: str | None = None,
) -> None:
    """Spawn `incus exec` so a GUI app survives `jailbee` returning.

    Two layers of detachment: the parent Python ``subprocess.Popen`` is given
    a fresh session and ``/dev/null`` stdio so the child doesn't share jailbee's
    TTY (which would leave the terminal in a messed-up state on parent exit).
    The inner shell uses ``setsid`` + ``</dev/null`` so the GUI process
    detaches from the bash that launched it.

    This is the one place in jailbee outside ``incus.py`` that runs the
    ``incus`` binary directly, and it stays that way: callers hand it an
    environment and a command line, never their own subprocess.
    """
    cwd_args = ["--cwd", cwd] if cwd else []
    env_args: list[str] = []
    for k, v in env.items():
        env_args += ["--env", f"{k}={v}"]
    shell = f"setsid bash -c {shlex.quote(inner_cmd)} </dev/null >{shlex.quote(log_path)} 2>&1 &"
    subprocess.Popen(
        [
            "incus",
            "exec",
            container,
            "--user",
            str(uid),
            *cwd_args,
            *env_args,
            "--",
            "bash",
            "-c",
            shell,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
