"""Route GPG sockets in Jailbee-owned directories, never host /run/user/<uid>/gnupg.

Symlink targets are container paths: only S.gpg-agent switches to the newest
remote forward. Other sockets, especially the SSH agent, stay on the host.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from jailbee import db

FORWARD_MARKER = "/jailbee/gpg-agent"
CONTAINER_HOST_DIR = "/run/jailbee-gpg/host"
CONTAINER_REMOTE_DIR = "/run/jailbee-gpg/remote"
SWITCHED = "S.gpg-agent"
SOCKET_NAMES: tuple[str, ...] = (
    SWITCHED,
    "S.gpg-agent.ssh",
    "S.gpg-agent.extra",
    "S.gpg-agent.browser",
    "S.dirmngr",
    "S.keyboxd",
    "S.scdaemon",
)


def switch_dir() -> Path:
    return db.state_dir() / "gpg-switch"


def remote_dir() -> Path:
    return db.state_dir() / "gpg-remote"


def remote_socket(session_id: str) -> Path:
    return remote_dir() / f"{session_id}.sock"


def ensure_switch_dir() -> Path:
    """Initialize host fallback even if the SSH service has never run."""
    directory = switch_dir()
    for path in (directory, remote_dir()):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    for name in SOCKET_NAMES:
        try:
            (directory / name).symlink_to(f"{CONTAINER_HOST_DIR}/{name}")
        except FileExistsError:
            # Container-path links look dangling on the host; preserve them.
            pass
    return directory


def point_agent(session_id: str | None) -> None:
    """Atomically select the remote agent, or the independent host fallback."""
    directory = ensure_switch_dir()
    target = (
        f"{CONTAINER_REMOTE_DIR}/{session_id}.sock"
        if session_id is not None
        else f"{CONTAINER_HOST_DIR}/{SWITCHED}"
    )
    temporary = directory / f".{SWITCHED}.{uuid4().hex}"
    try:
        temporary.symlink_to(target)
        os.replace(temporary, directory / SWITCHED)
    finally:
        temporary.unlink(missing_ok=True)


class GpgForwards:
    """Live forwards in establishment order; process-local and never persisted."""

    def __init__(self) -> None:
        self._sessions: list[str] = []

    def current(self) -> str | None:
        return self._sessions[-1] if self._sessions else None

    def add(self, session_id: str) -> None:
        point_agent(session_id)
        self._sessions.append(session_id)

    def remove(self, session_id: str) -> None:
        self._sessions = [item for item in self._sessions if item != session_id]
        try:
            point_agent(self.current())
        finally:
            remote_socket(session_id).unlink(missing_ok=True)

    def reset(self) -> None:
        self._sessions.clear()
        point_agent(None)
        for socket in remote_dir().glob("*.sock"):
            socket.unlink(missing_ok=True)
