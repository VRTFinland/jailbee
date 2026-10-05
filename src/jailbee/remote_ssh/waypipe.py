"""`waypipe ssh` sessions: recognise the client's remote command, name its parts.

`waypipe ssh` asks for a reverse unix forward (`-R <remote>:<local>`) and then
runs ``waypipe ... --socket <remote> --display <name> server [cmd...]`` on the
far end. JailBee never runs that command: it listens for the forward at a path
of its own (`links_socket`), runs ``cmd`` as an ordinary session command, and
starts a waypipe server per container inside ``jailbee-display`` when an app
is launched (`start_container_server`). The parser accepts only what waypipe
0.11 was observed to send (spec, spike item 1); anything else is refused.
"""

from __future__ import annotations

import re
import secrets
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.remote_ssh.router import RouteError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jailbee.incus import Incus
    from jailbee.remote_ssh.session import WaypipeSession

SUN_PATH_MAX = 107
"""Usable bytes of a unix socket path (108 including the terminator)."""

SUPPORTED_FORM = (
    "Use a stock client: `waypipe ssh -t -p <port> jailbee@<host> dashboard`, or "
    "`waypipe ssh -p <port> jailbee@<host> --repo <prefix> <gui command>`."
)
_CLIENT_SOCKET_RE = re.compile(r"^/(?:[^/\0]+/)*[^/\0]+-server-[A-Za-z0-9]{6,32}\.sock$")
_COMPRESS_RE = re.compile(r"^(?:none|lz4(?:=-?\d{1,2})?|zstd(?:=-?\d{1,2})?)$")
# Flags `waypipe ssh` adds on its own, with whether they take a value. Only
# `--compress` and `--socket` are kept; the rest are the laptop's local choices.
_FLAGS: dict[str, bool] = {
    "--no-gpu": False,
    "--login-shell": False,
    "--unlink-socket": False,
    "--threads": True,
    "--compress": True,
    "--video": True,
    "--socket": True,
    "--display": True,
}


@dataclass(frozen=True)
class WaypipeRequest:
    """What a waypipe client's remote command asks for."""

    socket: str
    compress: str
    command: str | None


def _refuse(why: str) -> RouteError:
    return RouteError(f"Unsupported waypipe request: {why}. {SUPPORTED_FORM}")


def parse_server_command(raw: str | None) -> WaypipeRequest | None:
    """Parse ``raw`` as waypipe's server command; None if it is not one at all."""
    if raw is None or raw.split(maxsplit=1)[:1] != ["waypipe"]:
        return None
    try:
        argv = shlex.split(raw)
    except ValueError:
        raise _refuse("the command line does not parse") from None
    values: dict[str, str] = {}
    i = 1
    while i < len(argv) and argv[i] != "server":
        flag, eq, inline = argv[i].partition("=")
        takes_value = _FLAGS.get(flag)
        if takes_value is None:
            raise _refuse(f"option {flag!r} is not supported")
        if takes_value:
            if eq:
                values[flag] = inline
            elif i + 1 < len(argv):
                i += 1
                values[flag] = argv[i]
            else:
                raise _refuse(f"option {flag!r} needs a value")
        elif eq:
            raise _refuse(f"option {flag!r} takes no value")
        i += 1
    if i >= len(argv):
        raise _refuse("only the `server` mode is accepted")
    rest = argv[i + 1 :]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    socket = values.get("--socket")
    if socket is None or not is_client_socket_path(socket):
        raise _refuse("the server socket is missing or malformed")
    compress = values.get("--compress", "lz4")
    if not _COMPRESS_RE.fullmatch(compress):
        raise _refuse(f"compression {compress!r} is not supported")
    threads = values.get("--threads")
    if threads is not None and not threads.isdigit():
        raise _refuse("--threads takes a number")
    return WaypipeRequest(socket=socket, compress=compress, command=shlex.join(rest) or None)


def is_client_socket_path(path: str) -> bool:
    """Whether ``path`` has the shape of a waypipe client's server-socket name."""
    return bool(_CLIENT_SOCKET_RE.fullmatch(path)) and "/../" not in f"{path}/"


def new_session_id() -> str:
    return secrets.token_hex(4)


def links_dir() -> Path:
    """Host directory of the sessions' forward listeners; mounted into jailbee-display only."""
    from jailbee.db import state_dir

    return state_dir() / "waypipe-links"


def links_socket(session_id: str) -> Path:
    return links_dir() / f"{session_id}.sock"


def server_name(session_id: str, container: str) -> str:
    """The Wayland socket name of ``container``'s server in this session."""
    return f"wp-{session_id}-{container}"


def unit_name(session_id: str, container: str) -> str:
    return f"jailbee-{server_name(session_id, container)}"


SERVER_WAIT_SECONDS = 15.0
_POLL_SECONDS = 0.25
# Incus instance names: what `incus` itself accepts, so a container name that
# reaches a unit or socket name is already one of these. Checked anyway: the
# name comes from the command line of a remote session.
_CONTAINER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")


def start_container_server(
    incus: Incus,
    session: WaypipeSession,
    container: str,
    *,
    uid: int,
    gid: int,
    sleep_fn: Callable[[float], None] = time.sleep,
    wait_seconds: float = SERVER_WAIT_SECONDS,
) -> str:
    """Make sure ``container`` has its waypipe server in this session; return its display.

    The server runs in jailbee-display as a transient systemd unit: stopping
    the unit takes its per-connection children and so the apps' connections
    with it, which killing waypipe's main process does not (spec, spike
    item 4). Its Wayland socket lands in the shared display directory, which
    ``container`` mounts read-only at `SHARED_DISPLAY_DIR`.
    """
    from jailbee.gui import SHARED_DISPLAY_DIR, display_state_dir
    from jailbee.incus import IncusError
    from jailbee.remote_display import (
        DISPLAY_CONTAINER,
        DISPLAY_CONTAINER_DIR,
        WAYPIPE_LINKS_CONTAINER_DIR,
        DisplayError,
        ensure_display_mount,
    )

    if not _CONTAINER_RE.fullmatch(container):
        raise DisplayError(f"Cannot open a waypipe display for container {container!r}.")
    if not _COMPRESS_RE.fullmatch(session.compress):
        raise DisplayError(f"Invalid compression setting {session.compress!r}.")
    name = server_name(session.id, container)
    unit_base = unit_name(session.id, container)
    unit = f"{unit_base}.service"
    ensure_display_mount(incus, container)
    host_socket = display_state_dir() / name
    if _unit_state(incus, unit) != "active":
        try:
            incus.exec(
                DISPLAY_CONTAINER,
                [
                    "systemd-run",
                    f"--unit={unit_base}",
                    f"--uid={uid}",
                    f"--gid={gid}",
                    "--collect",
                    f"--setenv=XDG_RUNTIME_DIR={DISPLAY_CONTAINER_DIR}",
                    "waypipe",
                    "--no-gpu",
                    "--compress",
                    session.compress,
                    "--title-prefix",
                    f"[{container}] ",
                    "--socket",
                    f"{WAYPIPE_LINKS_CONTAINER_DIR}/{session.id}.sock",
                    "--display",
                    f"{DISPLAY_CONTAINER_DIR}/{name}",
                    "server",
                    "--",
                    "sleep",
                    "infinity",
                ],
                timeout=30,
            )
        except IncusError as e:
            # Two launches into one container at once: the loser finds the
            # winner's unit. Anything else is a real failure.
            if "already exists" not in str(e):
                raise DisplayError(f"Could not start the waypipe server: {e}") from e
    deadline = wait_seconds
    while not host_socket.exists():
        if deadline <= 0:
            _stop(incus, unit)
            raise DisplayError(
                f"The waypipe server for {container} did not start within "
                f"{int(wait_seconds)}s; see `journalctl -u {unit}` in {DISPLAY_CONTAINER}."
            )
        sleep_fn(_POLL_SECONDS)
        deadline -= _POLL_SECONDS
    return f"{SHARED_DISPLAY_DIR}/{name}"


def stop_session(incus: Incus, session_id: str) -> None:
    """Stop every server of one session and remove its sockets. Never raises.

    By glob, so a unit whose start is still in flight when the session ends
    is stopped too (a list built at session end could miss it).
    """
    from jailbee.remote_display import remove_waypipe_sockets

    _stop(incus, f"jailbee-wp-{session_id}-*.service")
    remove_waypipe_sockets(session_id)


def prune_all(incus: Incus) -> None:
    """Remove every session's leftovers; for a server start, when none is live."""
    from jailbee.remote_display import remove_waypipe_sockets

    _stop(incus, "jailbee-wp-*.service")
    remove_waypipe_sockets()


def _unit_state(incus: Incus, unit: str) -> str:
    from jailbee.incus import IncusError
    from jailbee.remote_display import DISPLAY_CONTAINER

    try:
        return incus.exec(DISPLAY_CONTAINER, ["systemctl", "is-active", unit], timeout=10).strip()
    except IncusError:
        return "inactive"


def _stop(incus: Incus, pattern: str) -> None:
    from jailbee.incus import IncusError
    from jailbee.remote_display import DISPLAY_CONTAINER

    try:
        incus.exec(DISPLAY_CONTAINER, ["systemctl", "stop", pattern], timeout=30)
    except IncusError:
        pass
