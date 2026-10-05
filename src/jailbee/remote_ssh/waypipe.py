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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.remote_ssh.router import RouteError

if TYPE_CHECKING:
    from pathlib import Path

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
