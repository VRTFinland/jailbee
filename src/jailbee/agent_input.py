"""Send a user turn to a recorded Claude session without clearing terminal input."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.accounts.adapters.base import get_adapter
from jailbee.incus import IncusError
from jailbee.tmux import _runuser

if TYPE_CHECKING:
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


def send(cfg: Config, incus: Incus, container: str, session_id: str, text: str) -> Outcome:
    """Deliver to one live session, or return a refusal; never clear terminal input.

    Capture and paste cannot be atomic with a human typing in the same pane.
    """
    if not any(
        c.get("name") == container and c.get("status", "").lower() == "running"
        for c in incus.list_containers()
    ):
        return Outcome("container_not_running", "Container not running")
    adapter = get_adapter("claude")
    sessions = adapter.read_sessions(adapter.session_home(cfg, container))
    matches = [s for s in sessions if s.session_id == session_id]
    if not matches:
        return Outcome("no_live_session", "No live session")
    try:
        processes = incus.exec(container, ["python3", "-c", _PROCESS_SCRIPT])
        tree: dict[int, tuple[int, int]] = {}
        for line in processes.splitlines():
            try:
                pid, parent, start = map(int, line.split())
                tree[pid] = (parent, start)
            except ValueError:
                continue
        live = [s for s in matches if tree.get(s.pid, (0, -1))[1] == s.proc_start]
        if len(live) != 1:
            return Outcome("no_live_session", "No live session")
        session = live[0]
        if session.agent != "claude":
            return Outcome("unsupported_agent", "Unsupported agent")
        if session.waiting_for:
            return Outcome("waiting_at_dialog", "Waiting at a dialog")
        panes = incus.exec(container, _runuser("tmux list-panes -a -F '#{pane_id} #{pane_pid}'"))
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
            return Outcome("no_pane", "No pane")
        pane = found[0]
        screen = incus.exec(container, _runuser(f"tmux capture-pane -p -t {pane}"))
        if not _empty_prompt(screen):
            return Outcome("unsent_terminal_input", "Unsent terminal input or unrecognised prompt")
        buffer = "jailbee-" + uuid.uuid4().hex
        incus.exec_with_input(container, _runuser(f"tmux load-buffer -b {buffer} -"), text)
        try:
            incus.exec(container, _runuser(f"tmux paste-buffer -p -b {buffer} -t {pane}"))
            incus.exec(container, _runuser(f"tmux send-keys -t {pane} Enter"))
        finally:
            incus.exec(container, _runuser(f"tmux delete-buffer -b {buffer}"))
        return Outcome("delivered", "Delivered")
    except IncusError as exc:
        return Outcome("delivery_error", f"Delivery failed: {exc}")
