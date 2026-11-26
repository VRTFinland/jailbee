"""Send user turns to agent sessions without clearing terminal input."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.accounts.adapters.base import wired_adapters
from jailbee.incus import IncusError
from jailbee.tmux import _runuser

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


def _empty_prompt(screen: str, cursor: int) -> bool:
    lines = screen.splitlines()
    borders = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*─{3,}\s*", line)]
    if len(borders) < 2:
        return False
    if not borders[-2] < cursor < borders[-1]:
        return False
    region = lines[borders[-2] + 1 : borders[-1]]
    if not region or not re.match(r"^\s*[\u276f>]", region[0]):
        return False
    first = re.sub(r"^\s*[\u276f>]", "", region[0], count=1)
    return not first.strip() and all(not line.strip() for line in region[1:])


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
            return Outcome("waiting_at_dialog", "Waiting at a dialog"), None
        process = tree[session.pid]
        if (
            process.state in {"T", "t", "Z", "X"}
            or process.tty <= 0
            or process.group != process.foreground
        ):
            return Outcome("no_pane", "Agent is not in the foreground"), None
        try:
            panes = incus.exec(
                container, _runuser("tmux list-panes -a -F '#{pane_id} #{pane_pid} #{cursor_y}'")
            )
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
            if (
                len(parts) == 3
                and re.fullmatch(r"%[0-9]+", parts[0])
                and parts[1].isdigit()
                and parts[2].isdigit()
            ):
                pane_process = tree.get(int(parts[1]))
                if int(parts[1]) in ancestors and pane_process and pane_process.tty == process.tty:
                    found.append((parts[0], int(parts[2])))
        if len(set(found)) != 1:
            return Outcome("no_pane", "No pane"), None
        pane, cursor = found[0]
        try:
            screen = incus.exec(container, _runuser(f"tmux capture-pane -p -t {pane}"))
        except IncusError:
            return Outcome("no_pane", "No pane"), None
        if not _empty_prompt(screen, cursor):
            return Outcome(
                "unsent_terminal_input", "Unsent terminal input or unrecognised prompt"
            ), None
        return Outcome("available", "Chat available"), pane
    except IncusError as exc:
        return Outcome("delivery_error", f"Delivery failed: {exc}"), None


def availability(cfg: Config, incus: Incus, container: str, session_id: str) -> Outcome:
    """Return the current availability; send repeats every check before delivery."""
    return _resolve(cfg, incus, container, session_id)[0]


def send(cfg: Config, incus: Incus, container: str, session_id: str, text: str) -> Outcome:
    """Deliver a user turn. Capture and paste cannot exclude concurrent human typing."""
    outcome, pane = _resolve(cfg, incus, container, session_id)
    if pane is None:
        return outcome
    buffer = "jailbee-" + uuid.uuid4().hex
    try:
        incus.exec_with_input(container, _runuser(f"tmux load-buffer -b {buffer} -"), text)
        try:
            incus.exec(container, _runuser(f"tmux paste-buffer -pr -b {buffer} -t {pane}"))
            incus.exec(container, _runuser(f"tmux send-keys -t {pane} Enter"))
        finally:
            try:
                incus.exec(container, _runuser(f"tmux delete-buffer -b {buffer}"))
            except IncusError:
                logging.getLogger(__name__).warning(
                    "Could not delete tmux input buffer", exc_info=True
                )
    except IncusError as exc:
        return Outcome("delivery_error", f"Delivery failed: {exc}")
    return Outcome("delivered", "Delivered")
