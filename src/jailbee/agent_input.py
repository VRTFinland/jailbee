"""Send user turns to agent sessions without clearing terminal input."""

from __future__ import annotations

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
        print(name, s[1], s[19])
    except (OSError, IndexError):
        pass
"""


def _empty_prompt(screen: str) -> bool:
    lines = screen.splitlines()
    borders = [i for i, line in enumerate(lines) if re.fullmatch(r"\s*─{3,}\s*", line)]
    if len(borders) < 2:
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


def _processes(incus: Incus, container: str) -> dict[int, tuple[int, int]]:
    tree: dict[int, tuple[int, int]] = {}
    for line in incus.exec(container, ["python3", "-c", _PROCESS_SCRIPT]).splitlines():
        try:
            pid, parent, start = map(int, line.split())
            tree[pid] = (parent, start)
        except ValueError:
            continue
    return tree


def _sessions(cfg: Config, container: str, tree: dict[int, tuple[int, int]]) -> list[AgentSession]:
    sessions = []
    for adapter in wired_adapters(cfg):
        for session in adapter.read_sessions(adapter.session_home(cfg, container)):
            if session.session_id and tree.get(session.pid, (0, -1))[1] == session.proc_start:
                sessions.append(session)
    return sessions


def live_sessions(cfg: Config, incus: Incus, container: str) -> list[AgentSession]:
    """Return recorded sessions whose process identity still matches the container."""
    if not _running(incus, container):
        return []
    return _sessions(cfg, container, _processes(incus, container))


def _resolve(
    cfg: Config, incus: Incus, container: str, session_id: str
) -> tuple[Outcome, str | None]:
    if not _running(incus, container):
        return Outcome("container_not_running", "Container not running"), None
    try:
        tree = _processes(incus, container)
        matches = [s for s in _sessions(cfg, container, tree) if s.session_id == session_id]
        if len(matches) != 1:
            return Outcome("no_live_session", "No live session"), None
        session = matches[0]
        if session.agent != "claude":
            return Outcome("unsupported_agent", "Unsupported agent"), None
        if session.waiting_for:
            return Outcome("waiting_at_dialog", "Waiting at a dialog"), None
        try:
            panes = incus.exec(
                container, _runuser("tmux list-panes -a -F '#{pane_id} #{pane_pid}'")
            )
        except IncusError:
            return Outcome("no_pane", "No pane"), None
        ancestors = set()
        pid = session.pid
        while pid in tree and pid not in ancestors:
            ancestors.add(pid)
            pid = tree[pid][0]
        found = []
        for line in panes.splitlines():
            parts = line.split()
            if len(parts) == 2 and re.fullmatch(r"%[0-9]+", parts[0]) and parts[1].isdigit():
                if int(parts[1]) in ancestors:
                    found.append(parts[0])
        if len(set(found)) != 1:
            return Outcome("no_pane", "No pane"), None
        pane = found[0]
        try:
            screen = incus.exec(container, _runuser(f"tmux capture-pane -p -t {pane}"))
        except IncusError:
            return Outcome("no_pane", "No pane"), None
        if not _empty_prompt(screen):
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
            incus.exec(container, _runuser(f"tmux delete-buffer -b {buffer}"))
    except IncusError as exc:
        return Outcome("delivery_error", f"Delivery failed: {exc}")
    return Outcome("delivered", "Delivered")
