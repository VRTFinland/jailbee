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
    from jailbee.accounts.adapters.base import AccountAdapter
    from jailbee.accounts.models import AgentActivity, AgentSession
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.orchestration.actions import Node, Outcome
    from jailbee.orchestration.watch import WatchEntry


def advance(
    entry: WatchEntry,
    child: Node | None,
    status: AgentStatus | None,
    *,
    now: datetime,
    has_jailbee: bool,
) -> tuple[WatchEntry | None, Outcome | None]:
    """Pure durable state transition after boot work has finished."""
    from jailbee.orchestration import labels
    from jailbee.orchestration.actions import done, failed

    if child is not None and child.config.get(labels.STATE) == labels.STATE_DONE:
        if entry.confirmed:
            return None, None  # Readiness was already reported; nothing left to say.
        # A fast child can finish before readiness is confirmed; the spawn or
        # restart ref still needs its final status.
        return None, done(f"child `{child.short}` is ready; it already reported done")
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
            if not has_jailbee:
                from jailbee.orchestration.outbox import MISSING_JAILBEE_MESSAGE

                return None, failed(MISSING_JAILBEE_MESSAGE)
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
    waiters_only: bool | None = None
    processes_clear: bool | None = None
    process_snapshot: str | None = None


def _matched_session(
    cfg: Config, name: str, status: AgentStatus
) -> tuple[AccountAdapter, AgentSession] | None:
    from jailbee.accounts.adapters import base
    from jailbee.agent_status import _one_per_process

    if not status.alive or status.pid is None or status.proc_start is None:
        return None
    adapter = base.get_adapter(status.agent)
    sessions = _one_per_process(adapter.read_sessions(adapter.session_home(cfg, name)))
    session = next(
        (s for s in sessions if (s.pid, s.proc_start) == (status.pid, status.proc_start)), None
    )
    return (adapter, session) if session is not None else None


def current_session(cfg: Config, name: str, status: AgentStatus) -> str | None:
    """Find a safe session ID for the prompted process, best effort."""
    from jailbee.orchestration.labels import valid_session_id

    try:
        matched = _matched_session(cfg, name, status)
        sid = matched[1].session_id if matched is not None else None
        return sid if valid_session_id(sid) else None
    except Exception:
        return None


def idle_signal(cfg: Config, name: str, status: AgentStatus, *, now: datetime) -> str | None:
    """Adapter evidence for this prompted process only; unknown is never idle.

    Do not infer idleness from an unchanged pane: long-running tool calls can
    be silent. The adapter owns all agent-specific session/transcript formats.
    """
    try:
        matched = _matched_session(cfg, name, status)
        if matched is None:
            return None
        adapter, session = matched
        if session.state not in ("waiting", "idle"):
            return None
        paths = adapter.locate_activity(adapter.config_home(cfg), session)
        activity = adapter.read_activity(paths, now=now.timestamp()) if paths is not None else None
        # Waiting means input is required, even if background work exists.
        # Idle without readable activity cannot rule out a running subagent.
        if session.state == "idle" and (activity is None or activity.background_clear is not True):
            return None
        return json.dumps(
            [
                session.session_id,
                session.state,
                str(session.since),
                activity.modified if activity else None,
                str(activity.last_event_at) if activity else None,
            ]
        )
    except Exception:
        # Adapter formats are undocumented; a broken reader must not stall supervision.
        return None


@dataclass(frozen=True)
class RestartIdleEvidence:
    status: AgentStatus
    session: AgentSession
    activity: AgentActivity


def restart_idle_evidence(
    cfg: Config, name: str, status: AgentStatus | None, *, now: datetime
) -> RestartIdleEvidence | str:
    """Fail closed unless this exact agent is idle with no background work."""
    if status is None or not status.alive:
        return "unknown agent state"
    if not status.process_snapshot:
        return "process state unknown"
    try:
        matched = _matched_session(cfg, name, status)
        if matched is None:
            return "unreadable session state"
        adapter, session = matched
        if session.state not in ("idle", "shell"):
            return f"session state is {session.state}"
        paths = adapter.locate_activity(adapter.config_home(cfg), session)
        activity = adapter.read_activity(paths, now=now.timestamp()) if paths is not None else None
        if activity is None or activity.background_clear is not True:
            return "subagents running or background activity unknown"
        if session.state == "idle" and status.processes_clear is True:
            return RestartIdleEvidence(status, session, activity)
        if session.state == "shell" and status.waiters_only is True:
            return RestartIdleEvidence(status, session, activity)
        return "background commands running or process state unknown"
    except Exception:
        return "unreadable session or activity state"


def agent_snapshot(
    cfg: Config, name: str, status: AgentStatus, *, now: datetime
) -> dict[str, object]:
    """Structured agent evidence for status display, whatever the session state.

    Unlike `idle_signal` this also covers busy sessions. Times are epoch
    seconds; unknown fields are None. Any adapter failure leaves only `alive`.
    """
    from jailbee.orchestration.child_status import clean_text

    # No pid ever recorded and no exit status: the agent is not launched yet
    # (create or restart still running), which is not the same as dead.
    starting = not status.alive and not status.recorded and status.rc is None
    snap: dict[str, object] = {
        "alive": None if starting else status.alive,
        "state": "starting" if starting else None,
        "since": None,
        "last_event_at": None,
        "last_tool": None,
    }
    try:
        matched = _matched_session(cfg, name, status)
        if matched is None:
            return snap
        adapter, session = matched
        paths = adapter.locate_activity(adapter.config_home(cfg), session)
        activity = adapter.read_activity(paths, now=now.timestamp()) if paths is not None else None
        result = dict(snap)
        result["state"] = session.state
        result["since"] = session.since.timestamp() if session.since else None
        if activity is not None:
            if activity.last_event_at is not None:
                result["last_event_at"] = activity.last_event_at.timestamp()
            if activity.last_tool is not None:
                result["last_tool"] = clean_text(activity.last_tool, 160)
        return result
    except Exception:
        return snap


def advance_idle(
    entry: WatchEntry, token: str | None, *, now: datetime, after: int
) -> tuple[WatchEntry, str | None]:
    """One durable idle episode, with at most one notification per interval."""
    if token is None or entry.last_message_kind == "blocked":
        return replace(entry, idle_since=None, idle_reported_at=None, idle_token=None), None
    if entry.idle_token != token or entry.idle_since is None:
        return replace(
            entry, idle_since=now.isoformat(), idle_reported_at=None, idle_token=token
        ), None
    since = datetime.fromisoformat(entry.idle_since)
    due = datetime.fromisoformat(entry.idle_reported_at) if entry.idle_reported_at else since
    if (now - due).total_seconds() < after:
        return entry, None
    minutes = int((now - since).total_seconds() // 60)
    last = (
        f"{entry.last_message_kind} {entry.last_message_line or ''}"
        if entry.last_message_kind
        else "none"
    )
    body = (
        f"[jailbee] child `{entry.child_full}` has been waiting for input for {minutes}m; "
        f"last message: {last}"
    )
    return replace(entry, idle_reported_at=now.isoformat()), body


# Read /proc identity as well as kill(0): a reused PID or a zombie is not our agent.
_PROBE = r"""
import json, os, pathlib, pwd, re, shlex, sys
runtime, exit_path = map(pathlib.Path, sys.argv[1:3])

def waiter(argv):
    # Match entry points, never a process name or an argument containing 'wait'.
    if not argv:
        return False
    entry = pathlib.Path(argv[0]).name
    args = argv[1:]
    if entry in ('python', 'python3') or entry.startswith('python3.'):
        if args[:1] == ['-P']:
            args = args[1:]
        if len(args) < 2 or args[0] != '-m' or args[1] not in (
            'jailbee', 'jailbee.container_main', 'jailbee.inbox'):
            return False
        args = args[2:]
    elif entry not in ('jailbee', 'jb'):
        return False
    if args[:1] == ['inbox']:
        args = args[1:]
    return args[:1] == ['wait']

def strip_redirects(script):
    return re.sub(r'(?:[ \t]+(?:> ?/dev/null|2>&1|2> ?/dev/null|< ?/dev/null))+$', '', script)

def plain_waiter(script):
    script = strip_redirects(script)
    # A single invocation: no expansion, control operators, or other redirects,
    # even inside quotes. shlex validates quoting but never executes anything.
    if re.fullmatch(r'''[A-Za-z0-9_./: ,='"-]+''', script) is None:
        return False
    return waiter(shlex.split(script))

def statements(script):
    # Retain raw quotes for eval decoding. Split only at unquoted, top-level
    # operators; completed brace/subshell prefixes are opaque, not candidates.
    result, stack = [], []
    quote, start, index = None, 0, 0
    while index < len(script):
        char = script[index]
        if quote:
            if char == quote:
                quote = None
            elif quote == '"' and char in '$`':
                return None
            elif quote == '"' and char == '\\':
                index += 1
        elif char == '\\':
            index += 1
            if index == len(script):
                return None
        elif char in "'\"":
            quote = char
        elif char in '$`#' or script.startswith('<<', index):
            return None
        elif script.startswith(('2>&1', '>&1', '>|'), index):
            redirect = next(value for value in ('2>&1', '>&1', '>|')
                            if script.startswith(value, index))
            index += len(redirect) - 1
        elif char in '{(':
            stack.append('}' if char == '{' else ')')
        elif char in '})':
            if not stack or stack.pop() != char:
                return None
        elif char in '&|;':
            operator = char
            if script.startswith(('&&', '||'), index):
                operator *= 2
                index += 1
            elif char == '&':
                return None
            if not stack:
                result.append((script[start:index + 1 - len(operator)].strip(), operator))
                start = index + 1
        index += 1
    if quote or stack:
        return None
    result.append((script[start:].strip(), ''))
    # Keyword compounds are not parsed; refusing them also prevents an inner
    # waiter from being mistaken for a top-level command after a semicolon.
    reserved = {'while', 'until', 'for', 'select', 'if', 'then', 'elif', 'else',
                'fi', 'do', 'done', 'case', 'esac', 'function', '!'}
    for statement, operator in result:
        words = shlex.split(statement)
        if not words or words[0] in reserved or operator == '|':
            return None
    return result

def waiter_statement(script):
    script = strip_redirects(script)
    if plain_waiter(script):
        return True
    match = re.fullmatch(r'''eval ('(?:[^']|'"'"')*')''', script)
    if match is None:
        return False
    inner = match[1][1:-1].replace("'\"'\"'", "'")
    return plain_waiter(inner)

def builtin_prefix(script):
    script = re.sub(r'\s+\|\|\s+(?:true|false)$', '', script)
    script = strip_redirects(script)
    if script.startswith('{ '):
        if not script.endswith('}'):
            return False
        body = script[2:-1].strip().removesuffix(';').strip()
        parts = statements(body)
        return parts is not None and all(builtin_prefix(part) for part, _ in parts)
    if any(char in script for char in '(){}$`<>'):
        return False
    words = shlex.split(script)
    if not words:
        return False
    if words[0] in ('builtin', 'command'):
        words = words[1:]
    if not words:
        return False
    if words[0] in ('source', '.'):
        # Trust only Claude-generated snapshots, using the exec uid's home.
        home = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
        folder = home / '.claude' / 'shell-snapshots'
        return (len(words) == 2 and pathlib.Path(words[1]).parent == folder
                and re.fullmatch(r'[A-Za-z0-9_.-]+\.sh', pathlib.Path(words[1]).name) is not None)
    return words[0] in {'shopt', 'set', 'unalias', 'unset', 'export', 'declare',
                        'typeset', 'cd', 'true', 'false', ':', 'umask', 'hash', 'alias'}

def claude_waiter(script):
    parts = statements(script)
    if parts is None:
        return False
    candidates = [index for index, (statement, _) in enumerate(parts)
                  if waiter_statement(statement)]
    if len(candidates) != 1:
        return False
    index = candidates[0]
    # Nonbuiltin calls could still be running an earlier waiter with queued
    # work. Only known setup builtins and trusted snapshots can precede it.
    if not all(builtin_prefix(prefix) for prefix, _ in parts[:index]):
        return False
    statement, operator = parts[index]
    if index and parts[index - 1][1] not in ('&&', ';'):
        return False
    if index == len(parts) - 1:
        return operator == ''
    if index != len(parts) - 2 or operator not in ('&&', ';'):
        return False
    tail, final_operator = parts[-1]
    return (not final_operator and re.fullmatch(
        r'''pwd(?: -P)? >\|? (?:[A-Za-z0-9_./-]+|'[A-Za-z0-9_./ -]+')''', tail
    ) is not None)

def waiter_shell(argv):
    # Unknown harness wrappers must refuse rather than hide queued work.
    if (len(argv) != 3 or pathlib.Path(argv[0]).name not in ('bash', 'sh', 'dash')
            or argv[1] != '-c' or '\n' in argv[2] or '\r' in argv[2]):
        return False
    if plain_waiter(argv[2]):
        return True
    return pathlib.Path(argv[0]).name in ('bash', 'sh') and claude_waiter(argv[2])

def process_tree():
    procs = {}
    for path in pathlib.Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            raw = (path / 'stat').read_text()
        except FileNotFoundError:
            continue
        tail = raw.rsplit(')', 1)[1].split()
        if tail[0] not in ('Z', 'X'):
            procs[int(path.name)] = (int(tail[1]), tail[19], raw.split('(', 1)[1].rsplit(')', 1)[0])
    children = {}
    for child, (parent, _, _) in procs.items():
        children.setdefault(parent, set()).add(child)
    return procs, children

def background(pid, start):
    procs, children = process_tree()
    if pid not in procs or procs[pid][1] != start:
        return None, None, None
    roots = children.get(pid, set())
    leaves = []
    commands = {}
    for child in roots:
        descendants = children.get(child, [])
        if descendants:
            if procs[child][2] not in ('bash', 'sh', 'dash', 'zsh', 'fish'):
                return False, False, None
            raw = pathlib.Path('/proc', str(child), 'cmdline').read_bytes()
            if not waiter_shell(raw.rstrip(b'\0').decode('utf-8').split('\0')):
                return False, False, None
            commands[child] = raw
            leaves.extend(descendants)
        else:
            leaves.append(child)
    for child in leaves:
        if children.get(child):
            return False, False, None
        raw = pathlib.Path('/proc', str(child), 'cmdline').read_bytes()
        argv = raw.rstrip(b'\0').decode('utf-8').split('\0')
        if not waiter(argv):
            return False, False, None
        commands[child] = raw
    # Revalidate child sets too: stable identities alone miss a new fork.
    current, current_children = process_tree()
    for child in {pid, *roots, *leaves}:
        if (current.get(child) != procs.get(child)
                or current_children.get(child, set()) != children.get(child, set())):
            return None, None, None
    for child, raw in commands.items():
        if pathlib.Path('/proc', str(child), 'cmdline').read_bytes() != raw:
            return None, None, None
    snapshot = json.dumps([(child, procs[child], sorted(children.get(child, set())),
                            commands[child].hex() if child in commands else None)
                           for child in sorted({pid, *roots, *leaves})])
    return bool(leaves), not roots, snapshot

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
        if sys.argv[3:] == ['--background']:
            try:
                (result['waiters_only'], result['processes_clear'],
                 result['process_snapshot']) = background(pid, data['start'])
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                pass
except (OSError, ValueError, KeyError, TypeError, IndexError):
    pass
try:
    result['rc'] = int(exit_path.read_text())
    result['alive'] = False
except (OSError, ValueError):
    pass
print(json.dumps(result))
"""


def probe(incus: Incus, name: str, *, uid: int, background: bool = False) -> AgentStatus | None:
    """Return None for a transport/protocol error, not a false death report."""
    try:
        raw = incus.exec(
            name,
            ["python3", "-c", _PROBE, AGENT_PID_PATH, AGENT_EXIT_PATH]
            + (["--background"] if background else []),
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
        if any(
            value is not None and (type(value) is not int or value < 0) for value in (pid, start)
        ):
            return None
        waiters_only, processes_clear = data.get("waiters_only"), data.get("processes_clear")
        if any(
            value is not None and type(value) is not bool
            for value in (waiters_only, processes_clear)
        ):
            return None
        snapshot = data.get("process_snapshot")
        if snapshot is not None and not isinstance(snapshot, str):
            return None
        return AgentStatus(
            data["alive"], agent, rc, recorded, pid, start, waiters_only, processes_clear, snapshot
        )
    except (IncusError, ValueError, TypeError):
        return None


def process_identity_alive(
    incus: Incus, name: str, pid: int, start: int, *, uid: int
) -> bool | None:
    script = """import json, pathlib, sys
p = pathlib.Path('/proc', sys.argv[1], 'stat')
try:
    s = p.read_text().rsplit(')', 1)[1].split()
    print(json.dumps(s[0] not in ('Z', 'X') and int(s[19]) == int(sys.argv[2])))
except FileNotFoundError:
    print('false')
except Exception:
    print('null')
"""
    try:
        value = json.loads(
            incus.exec(name, ["python3", "-c", script, str(pid), str(start)], uid=uid, timeout=15)
        )
        return value if isinstance(value, bool) else None
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
