"""Probe a prompted agent, independently of its shell wrapper's survival."""

from __future__ import annotations

import json
import shlex
import signal
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from jailbee.incus import IncusError
from jailbee.initial_prompt import AGENT_EXIT_PATH, AGENT_PID_PATH

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.orchestration.actions import Node, Outcome
    from jailbee.orchestration.watch import WatchEntry


def advance(
    entry: WatchEntry, child: Node | None, status: AgentStatus | None, *, now: datetime
) -> tuple[WatchEntry | None, Outcome | None]:
    """Pure durable state transition after boot work has finished."""
    from jailbee.orchestration import labels
    from jailbee.orchestration.actions import done, failed

    if child is not None and child.config.get(labels.STATE) == labels.STATE_DONE:
        return None, None
    if child is None or not child.running:
        return None, failed("[jailbee] child stopped or disappeared without accepted done")
    if status is None:
        return entry, None  # Transport failure is not evidence of agent death.
    if entry.ready_since is None:
        entry = replace(entry, ready_since=now.isoformat())
    if status.alive:
        if entry.confirmed:
            return entry, None
        if entry.live_since is None:
            return replace(entry, live_since=now.isoformat()), None
        if (now - datetime.fromisoformat(entry.live_since)).total_seconds() >= 2:
            return replace(entry, confirmed=True), done(
                f"child `{child.short}` is ready; agent is alive"
            )
        return entry, None
    assert entry.ready_since is not None
    expired = (now - datetime.fromisoformat(entry.ready_since)).total_seconds() >= 120
    if entry.confirmed or status.rc is not None or entry.live_since is not None or expired:
        return None, failed(describe(status))
    return entry, None


@dataclass(frozen=True)
class AgentStatus:
    alive: bool
    agent: str = "agent"
    rc: int | None = None
    recorded: bool = False
    pid: int | None = None
    proc_start: int | None = None


def idle_signal(cfg: Config, name: str, status: AgentStatus, *, now: datetime) -> str | None:
    """Adapter evidence for this prompted process only; unknown is never idle.

    Do not infer idleness from an unchanged pane: long-running tool calls can
    be silent. The adapter owns all agent-specific session/transcript formats.
    """
    from jailbee.accounts.adapters import base
    from jailbee.agent_status import _one_per_process

    if not status.alive or status.pid is None or status.proc_start is None:
        return None
    try:
        adapter = base.get_adapter(status.agent)
        sessions = _one_per_process(adapter.read_sessions(adapter.session_home(cfg, name)))
        session = next((s for s in sessions if (s.pid, s.proc_start) == (status.pid, status.proc_start)), None)
        if session is None or session.state not in ("waiting", "idle"):
            return None
        paths = adapter.locate_activity(adapter.config_home(cfg), session)
        activity = adapter.read_activity(paths, now=now.timestamp()) if paths is not None else None
        # Waiting means input is required, even if background work exists.
        # Idle without readable activity cannot rule out a running subagent.
        if session.state == "idle" and (activity is None or activity.background_clear is not True):
            return None
        return json.dumps([session.session_id, session.state, str(session.since),
                           activity.modified if activity else None,
                           str(activity.last_event_at) if activity else None])
    except Exception:
        # Adapter formats are undocumented; a broken reader must not stall supervision.
        return None


def advance_idle(
    entry: WatchEntry, token: str | None, *, now: datetime, after: int
) -> tuple[WatchEntry, str | None]:
    """One durable idle episode, with at most one notification per interval."""
    if token is None or entry.last_message_kind == "blocked":
        return replace(entry, idle_since=None, idle_reported_at=None, idle_token=None), None
    if entry.idle_token != token or entry.idle_since is None:
        return replace(entry, idle_since=now.isoformat(), idle_reported_at=None, idle_token=token), None
    since = datetime.fromisoformat(entry.idle_since)
    due = datetime.fromisoformat(entry.idle_reported_at) if entry.idle_reported_at else since
    if (now - due).total_seconds() < after:
        return entry, None
    minutes = int((now - since).total_seconds() // 60)
    last = f"{entry.last_message_kind} {entry.last_message_line or ''}" if entry.last_message_kind else "none"
    body = f"[jailbee] child `{entry.child_full}` has been waiting for input for {minutes}m; last message: {last}"
    return replace(entry, idle_reported_at=now.isoformat()), body


# Read /proc identity as well as kill(0): a reused PID or a zombie is not our agent.
_PROBE = r"""
import json, pathlib, sys
runtime, exit_path = map(pathlib.Path, sys.argv[1:])
result = dict(alive=False, agent='agent', rc=None, recorded=False)
try:
    data = json.loads(runtime.read_text())
    result['agent'] = data['agent']
    pid = int(data['pid'])
    result['recorded'] = pid > 1
    stat = pathlib.Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    result['alive'] = (pid > 1 and stat[0] not in ('Z', 'X')
                       and stat[19] == data['start'] and boot == data['boot'])
    if result['alive']:
        result['pid'] = pid
        result['proc_start'] = int(stat[19])
except (OSError, ValueError, KeyError, TypeError, IndexError):
    pass
try:
    result['rc'] = int(exit_path.read_text())
    result['alive'] = False
except (OSError, ValueError):
    pass
print(json.dumps(result))
"""


def probe(incus: Incus, name: str, *, uid: int) -> AgentStatus | None:
    """Return None for a transport/protocol error, not a false death report."""
    try:
        raw = incus.exec(
            name,
            ["python3", "-c", _PROBE, AGENT_PID_PATH, AGENT_EXIT_PATH],
            uid=uid,
            timeout=15,
        )
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("alive"), bool):
            return None
        agent = data.get("agent", "agent")
        rc = data.get("rc")
        if not isinstance(agent, str) or (
            rc is not None and (type(rc) is not int or not 0 <= rc <= 255)
        ):
            return None
        recorded = data.get("recorded", False)
        if not isinstance(recorded, bool):
            return None
        pid, start = data.get("pid"), data.get("proc_start")
        if any(value is not None and (type(value) is not int or value < 0) for value in (pid, start)):
            return None
        return AgentStatus(data["alive"], agent, rc, recorded, pid, start)
    except (IncusError, ValueError, TypeError):
        return None


def describe(status: AgentStatus) -> str:
    """Include the shell status and the conventional signal interpretation."""
    reason = "exit status unknown (process disappeared or never started)"
    if status.rc is not None:
        reason = f"exited with status {status.rc}"
        if status.rc > 128:
            number = status.rc - 128
            try:
                sig = signal.Signals(number).name
            except ValueError:
                sig = f"signal {number}"
            reason += f" ({sig})"
    return f"[jailbee] the agent `{status.agent}` {reason} without accepted done"


def output_tail(incus: Incus, name: str, agent: str, *, uid: int) -> str:
    """Best effort: interactive output stays in tmux, never piped through tee."""
    from jailbee.tmux import SESSION_NAME, window_for

    target = shlex.quote(f"{SESSION_NAME}:={window_for(agent)}")
    try:
        return incus.exec(
            name,
            ["bash", "-lc", f"tmux capture-pane -p -S -30 -t {target}"],
            uid=uid,
            timeout=15,
        )[-4096:].strip()
    except IncusError:
        return ""
