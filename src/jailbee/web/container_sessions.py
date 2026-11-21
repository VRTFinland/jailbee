"""A container's agent sessions: the live ones, then those it has had.

Live sessions come from the latest state-service snapshot, past ones from the
`container_session` rows the gatherer records. Both are keyed to the
container's Incus instance uuid, so a container recreated under the same name
starts with a clean history. Titles, activity and models come from the
repository's transcripts (`RepoSessions`).
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
    """`GET /api/containers/{name}/sessions`: live sessions first."""

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


def container_sessions(
    snapshot: Snapshot | None,
    name: str,
    scope: RemoteRepoScope,
    engine: Engine,
    repo_sessions: RepoSessions,
) -> ContainerSessionListView | None:
    """None when the container is unknown or its repository excluded."""
    found = find_container(snapshot, name, scope)
    if found is None:
        return None
    group, c = found
    ordered: list[tuple[str, str, bool]] = []
    seen: set[tuple[str, str]] = set()
    for summary in c.agent_status:
        for sid in summary.session_ids:
            if (summary.agent, sid) not in seen:
                seen.add((summary.agent, sid))
                ordered.append((summary.agent, sid, True))
    for rec in recorded_sessions(engine, c.name, c.uuid):
        if (rec.agent, rec.session_id) not in seen:
            seen.add((rec.agent, rec.session_id))
            ordered.append((rec.agent, rec.session_id, False))
    info = {(r.agent, r.session_id): r for r in repo_sessions.list(group.prefix) or []}
    views: list[ContainerSessionView] = []
    for agent, sid, is_live in ordered:
        row = info.get((agent, sid))
        views.append(
            ContainerSessionView(
                session_id=sid,
                agent=agent,
                live=is_live,
                title=row.title if row else "",
                last_activity=row.last_activity if row else None,
                models=list(row.models) if row else [],
            )
        )
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
