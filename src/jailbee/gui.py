"""GUI launch primitives shared by every registry app.

`gui.py` knows about no application by name — see `apps.py` for the
registry (`AppSpec`, `get_app`, `launch`) that resolves what to run and
which container path each app lives at.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from jailbee.config import CONTAINER_USERNAME, Config

DisplayTarget = Literal["host", "shared", "waypipe"]

SHARED_DISPLAY_DIR = "/run/jailbee-display"
"""Where the shared display's directory is mounted in every container."""
SHARED_WAYLAND_SOCKET = f"{SHARED_DISPLAY_DIR}/wayland-0"


def display_state_dir() -> Path:
    """Host directory holding the shared display's Wayland socket."""
    from jailbee.db import state_dir

    return state_dir() / "display"


def display_target(environ: Mapping[str, str] | None = None) -> DisplayTarget:
    """Where this process's GUI apps should draw: the host, the shared RDP display, or waypipe.

    ``waypipe`` for a `waypipe ssh` session, ``shared`` for any other SSH
    session whose server has `remote.ssh.gui` on (see
    `remote_ssh.session.child_environment`); everything else, every local
    command included, keeps drawing on the host exactly as before.
    """
    from jailbee.remote_ssh.session import is_shared_display_session, waypipe_session

    if waypipe_session(environ) is not None:
        return "waypipe"
    return "shared" if is_shared_display_session(environ) else "host"


def gui_env(
    cfg: Config, target: DisplayTarget = "host", *, wayland_display: str | None = None
) -> dict[str, str]:
    """Environment vars for GUI apps inside the container.

    ``target`` picks the display: the host's (default), the shared RDP one, or
    a waypipe server's; the last two offer Wayland only. ``wayland_display`` is
    the waypipe server's socket and is required for ``"waypipe"``.

    HOME, USER and LOGNAME must all be set explicitly: ``incus exec
    --user <uid>`` runs the process directly rather than through
    ``login``/PAM, so none of the usual mechanisms that would derive
    them from ``/etc/passwd`` ever run. Without HOME, apps see ``HOME=``
    and fail (Chrome can't write its profile, JetBrains can't find its
    config, etc.); USER/LOGNAME are just as real a dependency — a GUI
    app (or a plain shell command) reading ``$USER`` sees it empty
    otherwise.
    """
    uid = cfg.container_user.uid
    env = {
        "HOME": f"/home/{CONTAINER_USERNAME}",
        "USER": CONTAINER_USERNAME,
        "LOGNAME": CONTAINER_USERNAME,
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
    }
    if target == "waypipe":
        if wayland_display is None:
            raise ValueError("the waypipe target needs its server's socket")
        # Wayland only, as on the shared display: waypipe forwards no X11 here.
        env["WAYLAND_DISPLAY"] = wayland_display
        return env
    if target == "shared":
        # An absolute path is a valid WAYLAND_DISPLAY. No X11: the shared
        # compositor offers Wayland only.
        env["WAYLAND_DISPLAY"] = SHARED_WAYLAND_SOCKET
        return env
    env["WAYLAND_DISPLAY"] = host_wayland_socket()
    env["DISPLAY"] = os.environ.get("DISPLAY", ":0")
    return env


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


def _exec_prefix(container: str, uid: int, env: dict[str, str], cwd: str | None) -> list[str]:
    """The `incus exec` argv up to and including ``--``: user, cwd and env flags."""
    cwd_args = ["--cwd", cwd] if cwd else []
    env_args: list[str] = []
    for k, v in env.items():
        env_args += ["--env", f"{k}={v}"]
    return ["incus", "exec", container, "--user", str(uid), *cwd_args, *env_args, "--"]


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

    With `launch_attached`, this is the one place in jailbee outside
    ``incus.py`` that runs the ``incus`` binary directly, and it stays that way: callers hand it an
    environment and a command line, never their own subprocess.
    """
    shell = f"setsid bash -c {shlex.quote(inner_cmd)} </dev/null >{shlex.quote(log_path)} 2>&1 &"
    subprocess.Popen(
        [*_exec_prefix(container, uid, env, cwd), "bash", "-c", shell],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def launch_attached(
    container: str,
    uid: int,
    env: dict[str, str],
    inner_cmd: str,
    log_path: str,
    *,
    cwd: str | None = None,
) -> int:
    """Run a GUI app through `incus exec` and wait for it; return its status.

    For a `waypipe ssh` session whose own command is the launch: the session,
    and with it the forward the app draws through, lasts as long as the app.
    Output still goes to ``log_path`` in the container, as when detached.
    """
    shell = f"{inner_cmd} </dev/null >{shlex.quote(log_path)} 2>&1"
    return subprocess.run(
        [*_exec_prefix(container, uid, env, cwd), "bash", "-c", shell],
        stdin=subprocess.DEVNULL,
        check=False,
    ).returncode
