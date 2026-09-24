"""Which JailBee version each running SSH server was started from.

A server imports its routing and session-marking code once, at startup, so
after an upgrade it keeps enforcing the *old* rules while every session it
starts runs the new CLI — the one combination in which the new CLI's remote
restrictions are never switched on, because the old server never sets the
marker they key on. Two things close that window:

- the server compares `installed_version()` with the version it started
  from, on every connection and on a timer, and exits to be restarted when
  they differ (see `server.serve_async`);
- a server predating that check cannot notice anything, so each server
  records itself here, and `service.stale_service_reason` treats a running
  service with no matching record as one to restart.

One file per server process (`<state>/ssh-server/<pid>.json`), so a
foreground `jb remote ssh serve` and the systemd service never overwrite each
other's record. A record whose process is gone is ignored and pruned.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

from jailbee.db import state_dir

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class RunningServer:
    pid: int
    version: str


def installed_version() -> str | None:
    """The JailBee version installed right now, read afresh from its metadata.

    Not `jailbee.__version__`, which is fixed when this process imported it.
    None when no metadata is found — a source tree without an install, or the
    moment mid-upgrade when the old dist-info is gone and the new one is not
    yet written. Callers treat None as "nothing known", never as a change.
    """
    try:
        return version("jailbee")
    except PackageNotFoundError:
        return None


def _records_dir() -> Path:
    return state_dir() / "ssh-server"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def record_running(running_version: str, *, pid: int | None = None) -> Path:
    """Record that process ``pid`` (default: this one) serves ``running_version``."""
    pid = os.getpid() if pid is None else pid
    directory = _records_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": pid, "version": running_version}))
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def clear_running(*, pid: int | None = None) -> None:
    """Remove the record of process ``pid`` (default: this one), if any."""
    pid = os.getpid() if pid is None else pid
    (_records_dir() / f"{pid}.json").unlink(missing_ok=True)


def running_servers() -> dict[int, RunningServer]:
    """Every recorded server whose process is still alive, by pid.

    Unreadable or malformed records are skipped; records of processes that
    are gone are deleted on the way.
    """
    directory = _records_dir()
    if not directory.is_dir():
        return {}
    servers: dict[int, RunningServer] = {}
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            server = RunningServer(pid=int(data["pid"]), version=str(data["version"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not _alive(server.pid):
            path.unlink(missing_ok=True)
            continue
        servers[server.pid] = server
    return servers
