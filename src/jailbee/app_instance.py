"""Moving a running GUI app to the display it is launched from.

An app with a profile lock (`AppSpec.singleton`) forwards a second launch to
its running instance, which can only draw where it started. This module
finds out whether that instance is on another display and, if the user
wants, closes it so the launch can start it again here. Container commands
go through the `Incus` wrapper; no `subprocess` here.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from jailbee.gui import SHARED_DISPLAY_DIR, SHARED_WAYLAND_SOCKET

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jailbee.apps import AppSpec
    from jailbee.config import Config
    from jailbee.incus import Incus, RunningInstance


def same_display(running: RunningInstance, env: Mapping[str, str]) -> bool:
    """Whether a launch with ``env`` would draw where ``running`` draws.

    The host is one display: an instance on it matches any launch that does
    not target the shared RDP display or a waypipe session, whatever its
    ``WAYLAND_DISPLAY`` / ``DISPLAY`` say (the host target's fallbacks such as
    ``wayland-0`` / ``:0`` need not equal the names the browser started with,
    and moving a working desktop browser for that would be wrong). Shared and
    waypipe displays compare by their socket path.
    """
    launch = env.get("WAYLAND_DISPLAY")
    if display_name(running) == "host" and not (launch or "").startswith(f"{SHARED_DISPLAY_DIR}/"):
        return True
    return running.wayland_display == launch


def display_name(running: RunningInstance) -> str:
    """The display ``running`` is on, as the user knows it."""
    wayland = running.wayland_display or ""
    if wayland == SHARED_WAYLAND_SOCKET:
        return "shared RDP"
    if wayland.startswith(f"{SHARED_DISPLAY_DIR}/"):
        return "waypipe"
    return "host"


POLL_SECONDS = 0.25
WAIT_SECONDS = 15.0
"""Long enough for a browser to write its session out."""


class AppMoveError(RuntimeError):
    """A running app did not close in time to be moved."""


def decide_move(move: bool | None, question: str) -> bool:
    """Whether to move: the flag when given, else ask on a terminal, else yes.

    Without a terminal the launch itself is the answer — it came from the
    display the user wants the app on (a `waypipe ssh` command, a dashboard
    action).
    """
    from jailbee import prompting

    if move is not None:
        return move
    if prompting.is_interactive():
        return prompting.confirm(question, default=True)
    return True


def _title(name: str) -> str:
    return name[:1].upper() + name[1:]


def ensure_on_this_display(
    cfg: Config,
    incus: Incus,
    container: str,
    spec: AppSpec,
    env: Mapping[str, str],
    *,
    move: bool | None,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Close ``spec``'s running instance if it is on another display and the
    user wants it here; True when one was closed.

    No SIGKILL: a killed browser loses its clean shutdown and its session,
    and the user can close the window themselves.
    """
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.incus import IncusError
    from jailbee.tui import info

    singleton = spec.singleton
    assert singleton is not None  # callers check; narrows the type
    lock = singleton.lock.replace("~", f"/home/{CONTAINER_USERNAME}", 1)
    uid, gid = cfg.container_user.uid, cfg.container_user.gid

    def find() -> RunningInstance | None:
        return incus.running_instance(container, lock, singleton.exe_names, uid=uid, gid=gid)

    running = find()
    if running is None or same_display(running, env):
        return False
    where = display_name(running)
    title = _title(spec.name)
    if not decide_move(move, f"{title} is open on the {where} display. Move it here?"):
        info(f"{title} is open on the {where} display; the window opens there.")
        return False
    info(f"Moving {spec.name} from the {where} display…")
    try:
        incus.exec(container, ["kill", "-TERM", str(running.pid)], uid=uid, gid=gid)
    except IncusError:
        pass  # already gone; the poll below confirms it
    def process_gone() -> bool:
        try:
            incus.exec(container, ["test", "-d", f"/proc/{running.pid}"], uid=uid, gid=gid)
        except IncusError:
            return True
        return False

    # The lock goes before the process does; a relaunch in between would race
    # the dying browser, so both must be gone.
    deadline = now_fn() + WAIT_SECONDS
    while True:
        if find() is None and process_gone():
            return True
        if now_fn() >= deadline:
            break
        sleep_fn(POLL_SECONDS)
    raise AppMoveError(
        f"{title} did not close on the {where} display within 15 s; close it there and retry."
    )
