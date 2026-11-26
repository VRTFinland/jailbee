"""Send user turns to live, foreground agent sessions through tmux."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.accounts.adapters.base import wired_adapters
from jailbee.incus import IncusError
from jailbee.tmux import runuser

if TYPE_CHECKING:
    from jailbee.accounts.models import AgentSession
    from jailbee.config import Config
    from jailbee.incus import Incus


@dataclass(frozen=True)
class Outcome:
    code: str
    message: str


_PROCESS_SCRIPT = """import os
for name in os.listdir('/proc'):
    if not name.isdigit():
        continue
    try:
        s = open('/proc/' + name + '/stat').read().rsplit(')', 1)[1].split()
        print(name, s[1], s[19], s[0], s[2], s[5], s[4])
    except (OSError, IndexError):
        pass
"""


def _running(incus: Incus, container: str) -> bool:
    return any(
        c.get("name") == container and c.get("status", "").lower() == "running"
        for c in incus.list_containers()
    )


@dataclass(frozen=True)
class _Process:
    parent: int
    start: int
    state: str
    group: int
    foreground: int
    tty: int


def _processes(incus: Incus, container: str) -> dict[int, _Process]:
    tree: dict[int, _Process] = {}
    for line in incus.exec(container, ["python3", "-c", _PROCESS_SCRIPT]).splitlines():
        try:
            pid, parent, start, state, group, foreground, tty = line.split()
            tree[int(pid)] = _Process(
                int(parent), int(start), state, int(group), int(foreground), int(tty)
            )
        except ValueError:
            continue
    return tree


def _sessions(cfg: Config, container: str, tree: dict[int, _Process]) -> list[AgentSession]:
    records: dict[tuple[str, int, int], list[AgentSession]] = {}
    for adapter in wired_adapters(cfg):
        for session in adapter.read_sessions(adapter.session_home(cfg, container)):
            process = tree.get(session.pid)
            if process and process.start == session.proc_start:
                records.setdefault((session.agent, session.pid, session.proc_start), []).append(
                    session
                )
    sessions = []
    for group in records.values():
        latest = max(s.updated_at if s.updated_at is not None else -1 for s in group)
        newest = [s for s in group if (s.updated_at if s.updated_at is not None else -1) == latest]
        if len(newest) == 1 and newest[0].session_id:
            sessions.append(newest[0])
    return sessions


class ContainerNotRunningError(Exception):
    """The selected container is not running."""


def live_sessions(cfg: Config, incus: Incus, container: str) -> list[AgentSession]:
    """Return recorded sessions whose process identity still matches the container."""
    if not _running(incus, container):
        raise ContainerNotRunningError("Container not running")
    return _sessions(cfg, container, _processes(incus, container))


def _resolve(
    cfg: Config, incus: Incus, container: str, session_id: str
) -> tuple[Outcome, str | None]:
    try:
        if not _running(incus, container):
            return Outcome("container_not_running", "Container not running"), None
        tree = _processes(incus, container)
        matches = [s for s in _sessions(cfg, container, tree) if s.session_id == session_id]
        if len(matches) != 1:
            return Outcome("no_live_session", "No live session"), None
        session = matches[0]
        if session.agent != "claude":
            return Outcome("unsupported_agent", "Unsupported agent"), None
        if session.waiting_for:
            return (
                Outcome(
                    "waiting_at_dialog",
                    "The agent is waiting at a dialog in the terminal; answer it there first.",
                ),
                None,
            )
        process = tree[session.pid]
        if (
            process.state in {"T", "t", "Z", "X"}
            or process.tty <= 0
            or process.group != process.foreground
        ):
            return Outcome("no_pane", "Agent is not in the foreground"), None
        try:
            panes = incus.exec(container, runuser("tmux list-panes -a -F '#{pane_id} #{pane_pid}'"))
        except IncusError:
            return Outcome("no_pane", "No pane"), None
        ancestors = set()
        pid = session.pid
        while pid in tree and pid not in ancestors:
            ancestors.add(pid)
            pid = tree[pid].parent
        found = []
        for line in panes.splitlines():
            parts = line.split()
            if len(parts) == 2 and re.fullmatch(r"%[0-9]+", parts[0]) and parts[1].isdigit():
                pane_process = tree.get(int(parts[1]))
                if int(parts[1]) in ancestors and pane_process and pane_process.tty == process.tty:
                    found.append(parts[0])
        if len(set(found)) != 1:
            return Outcome("no_pane", "No pane"), None
        pane = found[0]
        return Outcome("available", "Chat available"), pane
    except IncusError as exc:
        return Outcome("delivery_error", f"Delivery failed: {exc}"), None


def availability(cfg: Config, incus: Incus, container: str, session_id: str) -> Outcome:
    """Return the current availability; send repeats every check before delivery."""
    return _resolve(cfg, incus, container, session_id)[0]


def sanitize_text(text: str) -> str:
    """Strip terminal control characters while preserving multiline input."""
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) <= 159))


def send(cfg: Config, incus: Incus, container: str, session_id: str, text: str) -> Outcome:
    """Deliver a user turn; Claude queues busy turns and owns any existing input."""
    from jailbee.state_service.paths import ensure_runtime_dir

    text = sanitize_text(text)
    if not text.strip():
        return Outcome("invalid_message", "Message must not be blank")
    outcome, pane = _resolve(cfg, incus, container, session_id)
    for _ in range(4):
        if pane is None:
            return outcome
        # Fixed lock stripes bound storage without unlinking held or awaited inodes.
        digest = hashlib.sha256(f"{container}\0{pane}".encode()).hexdigest()[:2]
        directory = ensure_runtime_dir() / "agent-input-locks"
        directory.mkdir(mode=0o700, exist_ok=True)
        with (directory / f"{digest}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            outcome, current = _resolve(cfg, incus, container, session_id)
            if current == pane:
                return _paste(incus, container, pane, text)
            if current is None:
                return outcome
            pane = current
    return Outcome("delivery_error", "Agent pane changed during delivery")


def _paste(incus: Incus, container: str, pane: str, text: str) -> Outcome:
    buffer = "jailbee-" + uuid.uuid4().hex
    try:
        incus.exec_with_input(container, runuser(f"tmux load-buffer -b {buffer} -"), text)
        try:
            incus.exec(container, runuser(f"tmux paste-buffer -pr -b {buffer} -t {pane}"))
            incus.exec(container, runuser(f"tmux send-keys -t {pane} Enter"))
        finally:
            try:
                incus.exec(container, runuser(f"tmux delete-buffer -b {buffer}"))
            except IncusError:
                logging.getLogger(__name__).warning(
                    "Could not delete tmux input buffer", exc_info=True
                )
    except IncusError as exc:
        return Outcome("delivery_error", f"Delivery failed: {exc}")
    return Outcome("delivered", "Delivered")
