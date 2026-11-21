"""A repository's agent sessions, listed through the transcript sources.

Which config homes hold a repository's sessions is the caller's to say (an
`AgentHomes` resolver): the server derives it from the state-service
snapshot, tests point it at a temporary directory.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.transcripts.base import (
    Entry,
    SessionFiles,
    SessionInfo,
    SessionInfoCache,
    TranscriptSource,
    get_source,
)
from jailbee.web.transcript_render import subagent_descriptions

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


@dataclass(frozen=True)
class SessionTranscript:
    """One session's list row and every entry of its transcript."""

    agent: str
    info: SessionInfo
    entries: tuple[Entry, ...]
    offset: int = 0
    """Byte offset the entries were read to: where a live follow resumes."""


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

    def transcript(self, prefix: str, agent: str, session_id: str) -> SessionTranscript | None:
        """One session read whole; None for an unknown repository, agent or session.

        ``agent`` must be one of the repository's own agents, and the source's
        `locate` validates ``session_id``, so neither reaches a path unchecked.
        """
        found = self._locate(prefix, agent, session_id)
        if found is None:
            return None
        source, _home, files, info = found
        read = source.read(files.transcript)
        return SessionTranscript(agent, info, read.entries, read.offset)

    def subagent_transcript(
        self, prefix: str, agent: str, session_id: str, agent_id: str
    ) -> SessionTranscript | None:
        """A subagent's own transcript, titled by what the parent asked of it.

        None for anything `transcript` would not find, and for an ``agent_id``
        the source rejects or that has no transcript.
        """
        found = self._locate(prefix, agent, session_id)
        if found is None:
            return None
        source, home, files, parent_info = found
        path = source.subagent_transcript(home, session_id, agent_id)
        if path is None:
            return None
        info = source.session_info(session_id, path, self._cache)
        if info is None:
            return None
        entries = source.read(files.transcript).entries
        description = subagent_descriptions(entries).get(agent_id)
        read = source.read(path)
        return SessionTranscript(
            agent,
            replace(info, title=description or info.title or parent_info.title),
            read.entries,
            read.offset,
        )

    def transcript_file(
        self, prefix: str, agent: str, session_id: str, subagent_id: str | None = None
    ) -> tuple[TranscriptSource, Path] | None:
        """The file a live follow watches, validated as `transcript` and
        `subagent_transcript` validate; None where they would answer None."""
        found = self._locate(prefix, agent, session_id)
        if found is None:
            return None
        source, home, files, _info = found
        if subagent_id is None:
            return source, files.transcript
        path = source.subagent_transcript(home, session_id, subagent_id)
        return None if path is None else (source, path)

    def _locate(
        self, prefix: str, agent: str, session_id: str
    ) -> tuple[TranscriptSource, Path, SessionFiles, SessionInfo] | None:
        homes = self._homes(prefix)
        if homes is None:
            return None
        for name, home in homes:
            if name != agent:
                continue
            try:
                source = get_source(name)
            except KeyError:
                return None
            files = source.locate(home, session_id)
            if files is None:
                continue
            info = source.session_info(session_id, files.transcript, self._cache)
            if info is None:
                return None
            return source, home, files, info
        return None
