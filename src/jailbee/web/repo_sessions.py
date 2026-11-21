"""A repository's agent sessions, listed through the transcript sources.

Which config homes hold a repository's sessions is the caller's to say (an
`AgentHomes` resolver): the server derives it from the state-service
snapshot, tests point it at a temporary directory.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.transcripts.base import SessionInfoCache, get_source

if TYPE_CHECKING:
    from jailbee.state_service.protocol import Snapshot

AgentHomes = Callable[[str], Sequence[tuple[str, Path]] | None]
"""Repo prefix → ``(agent, config home)`` pairs, or None for an unknown repo."""


def snapshot_homes(latest: Callable[[], Snapshot | None]) -> AgentHomes:
    """Resolve homes from the dashboard snapshot (`RepoGroup.agent_config_homes`)."""

    def resolve(prefix: str) -> Sequence[tuple[str, Path]] | None:
        snapshot = latest()
        if snapshot is None:
            return None
        for group in snapshot.groups:
            if group.prefix == prefix:
                # Every container of a repo shares one home per agent.
                return list(
                    dict.fromkeys((agent, home) for _, agent, home in group.agent_config_homes)
                )
        return None

    return resolve


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    agent: str
    title: str
    last_activity: datetime | None
    models: tuple[str, ...]


class RepoSessions:
    """Lists sessions across a repository's agents; owns the info cache."""

    def __init__(self, homes: AgentHomes) -> None:
        self._homes = homes
        self._cache = SessionInfoCache()

    def known(self, prefix: str) -> bool:
        return self._homes(prefix) is not None

    def list(self, prefix: str) -> list[SessionRow] | None:
        """Newest first across agents; None when the repository is unknown."""
        homes = self._homes(prefix)
        if homes is None:
            return None
        rows: list[SessionRow] = []
        for agent, home in homes:
            try:
                source = get_source(agent)
            except KeyError:
                continue
            rows.extend(
                SessionRow(i.session_id, agent, i.title, i.last_activity, i.models)
                for i in source.list_sessions(home, self._cache)
            )
        rows.sort(
            key=lambda r: r.last_activity.timestamp() if r.last_activity else 0.0, reverse=True
        )
        return rows
