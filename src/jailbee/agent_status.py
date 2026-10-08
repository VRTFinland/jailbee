"""Which agent sessions are live: the input to the AGENT column.

An agent's session files say what state a session is in. They do not say
whether it is still running. Each container records its sessions in its own
private overlay (`AccountAdapter.session_home`), and a killed session leaves
its file behind: one real `sessions/` directory held 79 files, 3 of them live.
A file is therefore believed only when a process of *that* container matches
it on both its innermost namespace pid and its start time, which rejects a
stale file and a recycled pid alike. Another container's files are never
consulted, so its sessions cannot be mistaken for, or forged as, this one's.

The matching half is pure and imports nothing from jailbee but the models, so
it is tested without a /proc. `read_sessions` is the one function that
reaches the adapters, and it imports their registry lazily.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.accounts.models import AgentActivity, AgentSession

log = logging.getLogger(__name__)

URGENCY: tuple[str, ...] = ("waiting", "busy", "shell", "idle")
"""Known states, most urgent first. Any other state ranks after all of them.

`shell` is Claude Code's idle with a background shell job still running: the
agent wants nothing from you yet, but it is not finished either.
"""


type ActivityLookup = Callable[[str, AgentSession, int], AgentActivity | None]
"""`(container, session, host pid)` → what that session is doing, or None."""


@dataclass(frozen=True)
class AgentSummary:
    """One agent's live sessions in one container, as the AGENT cell shows them.

    `state`, `since` and `waiting_for` belong to the most urgent session;
    `count` is how many live sessions this agent has in the container.
    `activity` comes from the live session with the most recently written readable transcript.
    """

    agent: str
    state: str
    since: datetime | None
    waiting_for: str | None
    count: int
    activity: AgentActivity | None = None


def _rank(session: AgentSession) -> tuple[int, bool, float]:
    """Most urgent first; undated last among equals.

    Among equal `waiting` states the longest wait comes first: it is the one
    to answer. Among any other equal states the latest change comes first, so
    a session idle since yesterday does not hide one that just finished.
    """
    try:
        urgency = URGENCY.index(session.state)
    except ValueError:
        urgency = len(URGENCY)
    since = session.since
    if since is None:
        return (urgency, True, 0.0)
    stamp = since.timestamp()
    return (urgency, False, stamp if session.state == "waiting" else -stamp)


def _one_per_process(sessions: Iterable[AgentSession]) -> list[AgentSession]:
    """When two files claim one (pid, start), the newer `updated_at` wins."""
    best: dict[tuple[int, int], AgentSession] = {}
    for session in sessions:
        key = (session.pid, session.proc_start)
        held = best.get(key)
        if held is None or _updated(session) > _updated(held):
            best[key] = session
    return list(best.values())


def _updated(session: AgentSession) -> int:
    return -1 if session.updated_at is None else session.updated_at


def summarize(
    live: Iterable[AgentSession],
    activity_for: Callable[[Sequence[AgentSession]], AgentActivity | None] | None = None,
) -> tuple[AgentSummary, ...]:
    """One summary per agent, most urgent first, agent name as the tiebreak.

    `activity_for` receives all live sessions of an agent, ordered by urgency.
    """
    by_agent: dict[str, list[AgentSession]] = {}
    for session in live:
        by_agent.setdefault(session.agent, []).append(session)
    ranked: list[tuple[tuple[int, bool, float], str, AgentSummary]] = []
    for agent, items in by_agent.items():
        ordered = sorted(items, key=_rank)
        top = ordered[0]
        summary = AgentSummary(
            agent=agent,
            state=top.state,
            since=top.since,
            waiting_for=top.waiting_for,
            count=len(items),
            activity=None if activity_for is None else activity_for(ordered),
        )
        ranked.append((_rank(top), agent, summary))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(summary for _, _, summary in ranked)


def _lookup_for(
    container: str, host_pids: Mapping[tuple[int, int], int], activity: ActivityLookup | None
) -> Callable[[Sequence[AgentSession]], AgentActivity | None] | None:
    if activity is None:
        return None

    def lookup(sessions: Sequence[AgentSession]) -> AgentActivity | None:
        chosen: AgentActivity | None = None
        for session in sessions:
            found = activity(container, session, host_pids[(session.pid, session.proc_start)])
            if found is None:
                continue
            # Strict comparison preserves rank order for equal or unknown mtimes.
            if chosen is None or (
                found.modified is not None
                and (chosen.modified is None or found.modified > chosen.modified)
            ):
                chosen = replace(found, state=session.state, since=session.since)
        return chosen

    return lookup


def match_sessions(
    sessions: Mapping[str, Sequence[AgentSession]],
    processes: Mapping[str, Mapping[int, int]],
    nspid: Callable[[int], int | None],
    activity: ActivityLookup | None = None,
) -> dict[str, tuple[AgentSummary, ...]]:
    """Container name → its live agents, most urgent first.

    `sessions` maps each container to what its own session homes recorded;
    a container is matched against its own sessions only. `processes` maps
    each container to its host pids and their start times
    (`procstat.ProcSample.starttime`). `nspid` turns a host pid into the pid
    the process has in its own namespace, and is called only for a process
    whose start time one of the container's sessions claims: about one read
    per live session, not one per process. Every container in `processes`
    gets an entry. `activity`, when given, is asked about every live session
    with the container name and the session's host pid;
    it must not raise.
    """
    out: dict[str, tuple[AgentSummary, ...]] = {}
    for name, procs in processes.items():
        by_start: dict[int, list[AgentSession]] = {}
        for session in _one_per_process(sessions.get(name, ())):
            by_start.setdefault(session.proc_start, []).append(session)
        live: dict[tuple[int, int], AgentSession] = {}
        host_pids: dict[tuple[int, int], int] = {}
        for host_pid, start in procs.items():
            claims = by_start.get(start)
            if not claims:
                continue
            inner = nspid(host_pid)
            for session in claims:
                if session.pid == inner:
                    live[(session.pid, session.proc_start)] = session
                    host_pids[(session.pid, session.proc_start)] = host_pid
        out[name] = summarize(live.values(), _lookup_for(name, host_pids, activity))
    return out


def read_sessions(homes: Iterable[tuple[str, str, Path]]) -> dict[str, list[AgentSession]]:
    """Each container's recorded sessions, from `(container, agent, session home)`.

    Each distinct triple is read once. Only containers with at least one
    session appear. An agent with no adapter contributes nothing. An adapter
    that raises contributes nothing either: its contract says it never does,
    but it parses an undocumented format, and a bug there must not end the
    dashboard's refresh tick.
    """
    from jailbee.accounts.adapters import base

    by_container: dict[str, list[AgentSession]] = {}
    for container, name, home in dict.fromkeys(homes):
        try:
            adapter = base.get_adapter(name)
        except KeyError:
            continue
        try:
            found = adapter.read_sessions(home)
        except Exception:  # see the docstring: never fail the tick over this
            log.debug("reading %s sessions under %s failed", name, home, exc_info=True)
            continue
        if found:
            by_container.setdefault(container, []).extend(found)
    return by_container
