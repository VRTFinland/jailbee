"""Reading what live agent sessions are doing, and wording it.

`ActivityReader` is the one stateful piece: it remembers where each live
session's transcript is, so the directory search happens once per session and
not once per tick, and forgets a session the tick after it stops being live.
It is driven by `jailbee.dashboard.model.sample_activity` inside the state service's
`Gatherer`; the pure matching in `agent_status` only receives its `lookup_for`
callable.

The transcript itself is read every tick, bounded to its last 64 KiB: a
busy session changes it every tick anyway, and an idle one costs a single
small read.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from jailbee.lifecycle import format_duration_short
from jailbee.procstat import count_children

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from jailbee.accounts.models import ActivityPaths, AgentActivity, AgentSession
    from jailbee.agent_status import ActivityLookup, AgentSummary
    from jailbee.procstat import ProcSample

log = logging.getLogger(__name__)


class ActivityReader:
    """Finds and reads live sessions' transcripts. Not thread-safe: one tick at a time."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._paths: dict[tuple[str, str], ActivityPaths] = {}
        self._seen: set[tuple[str, str]] = set()

    @property
    def cached(self) -> int:
        """How many sessions' transcript locations are held."""
        return len(self._paths)

    def begin(self) -> None:
        """Start a tick: nothing has been seen live yet."""
        self._seen = set()

    def finish(self) -> None:
        """End a tick: forget every session that was not looked up in it."""
        self._paths = {key: paths for key, paths in self._paths.items() if key in self._seen}

    def lookup_for(
        self,
        config_homes: Mapping[tuple[str, str], Path],
        processes: Callable[[str], Mapping[int, ProcSample]],
    ) -> ActivityLookup:
        """An `ActivityLookup` over `config_homes` (container, agent → shared config home).

        `processes` answers a container's host pid → sample, for the shell
        count. The returned callable never raises: a transcript format that
        changed, or an agent with no adapter, is "no activity" for that
        session and nothing else.
        """

        def lookup(container: str, session: AgentSession, host_pid: int) -> AgentActivity | None:
            home = config_homes.get((container, session.agent))
            if home is None or session.session_id is None:
                return None
            key = (container, session.session_id)
            self._seen.add(key)
            try:
                activity = self._read(key, home, session)
                if activity is None:
                    return None
                return replace(activity, shells=count_children(processes(container), host_pid))
            except Exception:  # an undocumented format: never fail the tick over it
                log.debug("reading %s activity failed", session.agent, exc_info=True)
                return None

        return lookup

    def _read(
        self, key: tuple[str, str], home: Path, session: AgentSession
    ) -> AgentActivity | None:
        from jailbee.accounts.adapters import base

        adapter = base.get_adapter(session.agent)
        paths = self._paths.get(key)
        if paths is None:
            paths = adapter.locate_activity(home, session)
            if paths is None:  # not cached: the transcript may appear on the next message
                return None
            self._paths[key] = paths
        activity = adapter.read_activity(paths, now=self._clock())
        if activity is None:  # the file may have moved or been rotated: locate it again
            del self._paths[key]
        return activity


@dataclass(frozen=True)
class ActivityText:
    """The activity lines, as plain text. Renderers escape and style them."""

    head: str
    tool: str | None
    message: str | None

    def lines(self) -> tuple[str, ...]:
        """Head, then the tool and message lines that exist."""
        out = [self.head]
        if self.tool is not None:
            out.append(f"↳ {self.tool}")
        if self.message is not None:
            out.append(f"“{self.message}”")
        return tuple(out)


def _counted(count: int, noun: str, *, estimate: bool = False) -> str:
    return f"{'~' if estimate else ''}{count} {noun}{'' if count == 1 else 's'}"


def describe(summary: AgentSummary, now: datetime) -> ActivityText | None:
    """How `summary`'s activity reads, or None when it has none.

    A count of zero or unknown (`None`) is left out. Subagents carry a `~`:
    their liveness is an estimate.
    """
    activity = summary.activity
    if activity is None:
        return None
    # A supplied state identifies the selected session, even when it has no date.
    state, since = (
        (summary.state, summary.since)
        if activity.state is None
        else (activity.state, activity.since)
    )
    head = state
    if since is not None and since <= now:
        head += f" {format_duration_short(now - since)}"
    if activity.subagents:
        head += f" · {_counted(activity.subagents, 'subagent', estimate=True)}"
    if activity.shells:
        head += f" · {_counted(activity.shells, 'shell')}"
    tool = None if state == "idle" else activity.last_tool
    return ActivityText(head, tool, activity.last_message)
