"""A repository's agent sessions, listed through the transcript sources.

Which config homes hold a repository's sessions is the caller's to say (an
`AgentHomes` resolver): the server derives it from the state-service
snapshot, tests point it at a temporary directory.
"""

from __future__ import annotations

import builtins
import threading
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from jailbee.transcripts.base import (
    Entry,
    SessionFiles,
    SessionInfo,
    SessionInfoCache,
    SessionInfoStore,
    TranscriptSource,
    get_source,
)
from jailbee.web.transcript_render import subagent_descriptions

if TYPE_CHECKING:
    from jailbee.state_service.protocol import Snapshot

AgentHomes = Callable[[str], Sequence[tuple[str, Path]] | None]
"""Repo prefix → ``(agent, config home)`` pairs, or None for an unknown repo."""

RepoDirs = Callable[[str], Collection[str] | None]
"""Repo prefix → the in-container paths its sessions are started in, or None
when unknown (then every session in its homes is listed)."""


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


def snapshot_repo_dirs(latest: Callable[[], Snapshot | None]) -> RepoDirs:
    """Resolve the repository's in-container paths from the dashboard snapshot.

    Each container's `repo_dir` label, plus where a container of the repo
    would have it without one (`/home/<user>/<prefix>`, and the legacy
    `/home/<user>/<repo root name>`), so a repository whose containers are
    gone still lists its sessions. Several repositories may share a config
    home by mistake; these paths keep each list to its own sessions.
    """
    from jailbee.config.common import CONTAINER_USERNAME

    def resolve(prefix: str) -> Collection[str] | None:
        snapshot = latest()
        if snapshot is None:
            return None
        for group in snapshot.groups:
            if group.prefix == prefix:
                dirs = {c.repo_dir for c in group.containers if c.repo_dir}
                dirs.add(f"/home/{CONTAINER_USERNAME}/{prefix}")
                if group.repo_root:
                    dirs.add(f"/home/{CONTAINER_USERNAME}/{PurePosixPath(group.repo_root).name}")
                return dirs
        return None

    return resolve


@dataclass(frozen=True)
class SessionRow:
    session_id: str
    agent: str
    title: str
    last_activity: datetime | None
    models: tuple[str, ...]
    started: datetime | None = None
    cwd: str | None = None
    git_branch: str | None = None

    @classmethod
    def of(cls, agent: str, info: SessionInfo) -> SessionRow:
        return cls(
            info.session_id,
            agent,
            info.title,
            info.last_activity,
            info.models,
            info.started,
            info.cwd,
            info.git_branch,
        )


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

    def __init__(
        self,
        homes: AgentHomes,
        store: SessionInfoStore | None = None,
        repo_dirs: RepoDirs | None = None,
    ) -> None:
        self._homes = homes
        self._repo_dirs = repo_dirs
        self._cache = SessionInfoCache(store)

    def known(self, prefix: str) -> bool:
        return self._homes(prefix) is not None

    def list(self, prefix: str) -> list[SessionRow] | None:
        """Newest first across agents; None when the repository is unknown."""
        homes = self._homes(prefix)
        if homes is None:
            return None
        return self._rows(homes, self._repo_dirs(prefix) if self._repo_dirs else None)

    def warm(
        self,
        prefixes: Iterable[str],
        homes: AgentHomes,
        repo_dirs: RepoDirs | None,
        stop: threading.Event,
    ) -> None:
        """Read every listed repository's session info into the cache.

        Run once at start-up, in a thread of its own, so the first list a
        browser asks for finds the rows already read. ``homes`` and
        ``repo_dirs`` answer as the server's resolvers would, but from the
        snapshot the warm-up started with. ``stop`` is checked between
        repositories. The cache first forgets the rows of deleted transcripts.
        """
        self._cache.prune()
        for prefix in prefixes:
            if stop.is_set():
                return
            found = homes(prefix)
            if found is not None:
                self._rows(found, repo_dirs(prefix) if repo_dirs else None)

    def _rows(
        self, homes: Sequence[tuple[str, Path]], cwds: Collection[str] | None
    ) -> builtins.list[SessionRow]:
        rows: builtins.list[SessionRow] = []
        for agent, home in homes:
            try:
                source = get_source(agent)
            except KeyError:
                continue
            rows.extend(
                SessionRow.of(agent, i) for i in source.list_sessions(home, self._cache, cwds)
            )
        self._cache.flush()
        rows.sort(
            key=lambda r: r.last_activity.timestamp() if r.last_activity else 0.0, reverse=True
        )
        return rows

    def rows(
        self, prefix: str, keys: Iterable[tuple[str, str]]
    ) -> dict[tuple[str, str], SessionRow]:
        """These ``(agent, session id)`` sessions' list rows, each found by its id
        alone, without listing the repository. A key `transcript` would not
        find is absent."""
        found: dict[tuple[str, str], SessionRow] = {}
        for agent, session_id in keys:
            located = self._locate(prefix, agent, session_id, flush=False)
            if located is not None:
                found[(agent, session_id)] = SessionRow.of(agent, located[3])
        self._cache.flush()
        return found

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
        self._cache.flush()
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
        self, prefix: str, agent: str, session_id: str, *, flush: bool = True
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
            if flush:
                self._cache.flush()
            if info is None:
                return None
            return source, home, files, info
        return None
