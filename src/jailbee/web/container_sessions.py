"""A container's agent sessions: the live ones, and separately those it has had.

Live sessions come from the latest state-service snapshot, past ones from the
`container_session` rows the gatherer records. Both are keyed to the
container's Incus instance uuid, so a container recreated under the same name
starts with a clean history. Titles, activity and models come from each
session's own transcript (`RepoSessions.row`), never from listing the whole
repository: the live list costs one lookup per live session, and the history,
however long, is a request of its own that never delays it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from jailbee.state_service.session_log import attribution, recorded_sessions

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from jailbee.dashboard.model import RepoGroup
    from jailbee.lifecycle import ContainerInfo
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope
    from jailbee.state_service.protocol import Snapshot
    from jailbee.web.repo_sessions import RepoSessions


class ContainerSessionView(BaseModel):
    """One session of a container; ``live`` is true while its agent process runs."""

    model_config = ConfigDict(frozen=True)
    session_id: str
    agent: str
    live: bool
    title: str
    last_activity: datetime | None
    models: list[str]


class ContainerSessionListView(BaseModel):
    """`GET /api/containers/{name}/sessions` (the live sessions) and
    `GET /api/containers/{name}/sessions/history` (the recorded, not live ones)."""

    model_config = ConfigDict(frozen=True)
    container: str
    repo: str
    sessions: list[ContainerSessionView]


def find_container(
    snapshot: Snapshot | None, name: str, scope: RemoteRepoScope
) -> tuple[RepoGroup, ContainerInfo] | None:
    """The container in a repository the browser may see; None otherwise."""
    if snapshot is None:
        return None
    for group in snapshot.groups:
        if not scope.allows(group.prefix):
            continue
        for c in group.containers:
            if c.name == name:
                return group, c
    return None


def _live_keys(c: ContainerInfo) -> list[tuple[str, str]]:
    keys: dict[tuple[str, str], None] = {}
    for summary in c.agent_status:
        for sid in summary.session_ids:
            keys.setdefault((summary.agent, sid), None)
    return list(keys)


def _views(
    repo_sessions: RepoSessions, prefix: str, keys: list[tuple[str, str]], live: bool
) -> list[ContainerSessionView]:
    rows = repo_sessions.rows(prefix, keys)
    views: list[ContainerSessionView] = []
    for agent, sid in keys:
        row = rows.get((agent, sid))
        views.append(
            ContainerSessionView(
                session_id=sid,
                agent=agent,
                live=live,
                title=row.title if row else "",
                last_activity=row.last_activity if row else None,
                models=list(row.models) if row else [],
            )
        )
    return views


def live_sessions(
    snapshot: Snapshot | None,
    name: str,
    scope: RemoteRepoScope,
    repo_sessions: RepoSessions,
) -> ContainerSessionListView | None:
    """The container's running sessions; None when the container is unknown or
    its repository excluded."""
    found = find_container(snapshot, name, scope)
    if found is None:
        return None
    group, c = found
    views = _views(repo_sessions, group.prefix, _live_keys(c), live=True)
    return ContainerSessionListView(container=c.name, repo=group.prefix, sessions=views)


def session_history(
    snapshot: Snapshot | None,
    name: str,
    scope: RemoteRepoScope,
    engine: Engine,
    repo_sessions: RepoSessions,
) -> ContainerSessionListView | None:
    """The sessions the container ran before and is not running now, in recorded
    order; None as `live_sessions`."""
    found = find_container(snapshot, name, scope)
    if found is None:
        return None
    group, c = found
    live = set(_live_keys(c))
    keys: dict[tuple[str, str], None] = {}
    for rec in recorded_sessions(engine, c.name, c.uuid):
        if (rec.agent, rec.session_id) not in live:
            keys.setdefault((rec.agent, rec.session_id), None)
    views = _views(repo_sessions, group.prefix, list(keys), live=False)
    return ContainerSessionListView(container=c.name, repo=group.prefix, sessions=views)


def session_containers(
    snapshot: Snapshot | None, prefix: str, engine: Engine
) -> dict[tuple[str, str], str]:
    """``(agent, session id)`` → container name, over the repository's current containers.

    A session whose container is gone, or that was never seen live, is absent
    (unattributed).
    """
    if snapshot is None:
        return {}
    for group in snapshot.groups:
        if group.prefix == prefix:
            return attribution(engine, [(c.name, c.uuid) for c in group.containers])
    return {}
