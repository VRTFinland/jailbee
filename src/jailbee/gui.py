"""GUI app launchers — IDE and Chrome inside containers."""

from __future__ import annotations

import os
import shlex
import subprocess
from typing import get_args

from jailbee.config import CONTAINER_USERNAME, Config, IdeName
from jailbee.incus import Incus
from jailbee.tui import error, info

# Allowed JetBrains launcher names — source of truth is the IdeName Literal
# in config.py. Resolving this once at import time avoids drift.
_SUPPORTED_IDE_LAUNCHERS: frozenset[str] = frozenset(get_args(IdeName))


def _gui_env(cfg: Config) -> dict[str, str]:
    """Environment vars for GUI apps inside the container.

    HOME must be set explicitly: ``incus exec --user <uid>`` doesn't read
    /etc/passwd to derive it, so without this apps see ``HOME=`` and
    fail (Chrome can't write its profile, JetBrains can't find its
    config, etc.).
    """
    uid = cfg.container_user.uid
    return {
        "HOME": f"/home/{CONTAINER_USERNAME}",
        "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", "wayland-0"),
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DISPLAY": os.environ.get("DISPLAY", ":0"),
    }


def host_is_wayland() -> bool:
    """Return True if the host session is Wayland-native.

    Used to pass --ozone-platform=wayland to Chrome and to decide whether
    to bind-mount the host's Wayland socket into containers (see
    ``runtime_mounts``). On X11 hosts there is no wayland-0 socket to
    mount; Chrome falls back to its auto-detect (DISPLAY) and other GUI
    apps follow suit.
    """
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def _detached_incus_exec(
    container: str,
    uid: int,
    env_args: list[str],
    inner_cmd: str,
    log_path: str,
    *,
    cwd: str | None = None,
) -> None:
    """Spawn `incus exec` so the GUI app survives `jailbee` returning.

    Two layers of detachment: the parent Python ``subprocess.Popen`` is given
    a fresh session and ``/dev/null`` stdio so the child doesn't share jailbee's
    TTY (which would leave the terminal in a messed-up state on parent
    exit). The inner shell uses ``setsid`` + ``</dev/null`` so the GUI
    process detaches from the bash that launched it.
    """
    cwd_args = ["--cwd", cwd] if cwd else []
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


def open_ide(cfg: Config, incus: Incus, container: str, app: str) -> None:
    """Launch a JetBrains IDE from /opt/jetbrains-toolbox.

    Toolbox apps live at ``/opt/jetbrains-toolbox/apps/<app-id>/bin/<launcher>``.
    The <app-id> can vary across Toolbox versions and edition flavours
    (e.g. ``intellij-idea-ultimate``, ``pycharm-professional``), but the
    launcher binary is always the IDE's short name — so we match by launcher
    name, not by app-id.
    """
    if app not in _SUPPORTED_IDE_LAUNCHERS:
        supported = ", ".join(sorted(_SUPPORTED_IDE_LAUNCHERS))
        error(f"Unknown IDE app: {app} (must be one of: {supported})")
        return

    find_cmd = (
        f"find /opt/jetbrains-toolbox/apps -maxdepth 4 -type f -name '{app}' -executable | head -1"
    )
    result = incus.exec(
        container,
        ["bash", "-c", find_cmd],
        uid=cfg.container_user.uid,
        gid=cfg.container_user.gid,
    ).strip()

    if not result:
        error(f"No {app} launcher found in /opt/jetbrains-toolbox/apps")
        return

    from jailbee.lifecycle import container_repo_dir

    repo_dir = container_repo_dir(cfg, incus, container)
    log_path = f"/tmp/jailbee-ide-{app}.log"
    info(f"Launching {app} in {container} (background, logs in container: {log_path})")
    env_args: list[str] = []
    for k, v in _gui_env(cfg).items():
        env_args += ["--env", f"{k}={v}"]
    _detached_incus_exec(
        container,
        cfg.container_user.uid,
        env_args,
        f"{shlex.quote(result)} {shlex.quote(repo_dir)}",
        log_path,
        cwd=repo_dir,
    )


def open_chrome(cfg: Config, incus: Incus, container: str, url: str | None) -> None:
    """Launch Chrome inside the container, optionally to a URL.

    Allocates a slot from the chrome profile pool before
    launching, so each container has its own Chrome profile dir and
    they don't collide on Chrome's SingletonLock.
    """
    from jailbee.chrome_pool import allocate as chrome_pool_allocate

    chrome_pool_allocate(cfg, incus, container)

    args = ["/opt/google/chrome/google-chrome"]
    if host_is_wayland():
        # Chrome 2025 defaults to X11 even with WAYLAND_DISPLAY set; force
        # the Ozone Wayland backend explicitly.
        args.append("--ozone-platform=wayland")
    if cfg.chrome.dark_mode:
        args += ["--force-dark-mode", "--enable-features=WebContentsForceDark"]
    if url:
        args.append(url)

    log_path = "/tmp/jailbee-chrome.log"
    info(f"Launching Chrome in {container} (background, logs in container: {log_path})")
    env_args: list[str] = []
    for k, v in _gui_env(cfg).items():
        env_args += ["--env", f"{k}={v}"]
    _detached_incus_exec(
        container,
        cfg.container_user.uid,
        env_args,
        " ".join(shlex.quote(a) for a in args),
        log_path,
    )
