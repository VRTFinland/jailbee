"""A container's agent sessions: the live ones, and separately those it has had.

Live sessions come from the latest state-service snapshot. Past ones come
first from the `container_session` rows the gatherer records, keyed to the
container's Incus instance uuid so a container recreated under the same name
starts with a clean history. Sessions the recorder never saw (run before it
existed, or while the state service was down) are attributed from their own
transcript's metadata by `backfill`. Titles, activity and models come from each
session's own transcript. The live list costs one lookup per live session and
never lists the repository; the history lists it (through the persistent
session info cache) in a request of its own, so it never delays the live list.

Backfill rules (`backfill`), deterministic for one snapshot and one set of
transcripts:

- A session any container's recorder row names (present or gone) belongs
  only where it was recorded.
- A candidate container has a branch label (`ContainerInfo.branch`, the
  branch it was created on) equal to the session's starting `gitBranch`, a
  known creation time at or before the session's start, and the session's
  `cwd` at or below its `repo_dir`. A mount-mode container (no branch label),
  a container without a `repo_dir` label, a container of unknown age, and a
  session naming no branch or no start get no backfill.
- A container recreated under the same name is a new instance with a later
  creation time: sessions older than it stay out of its history.
- A branch renamed inside the container: sessions started after the rename
  name the new branch, which no label matches, so they stay unattributed
  unless the recorder saw them. Likewise a detached HEAD. A session started
  after checking out another container's labelled branch goes to that
  container: the transcript cannot tell them apart, since every container of
  a repository has the same `repo_dir`.
- Several candidates (two containers of one repository on one branch): the
  transcript cannot tell them apart, so the session stays unattributed
  rather than guessed. A container created after the session started is not
  a candidate, so an older-only single candidate still wins.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from jailbee.state_service.session_log import attribution, recorded_keys, recorded_sessions

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from jailbee.dashboard.model import RepoGroup
    from jailbee.lifecycle import ContainerInfo
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope
    from jailbee.state_service.protocol import Snapshot
    from jailbee.web.repo_sessions import RepoSessions, SessionRow


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


class ContainerSessionHistoryView(ContainerSessionListView):
    """One bounded page of earlier sessions, with the matching count."""

    total: int
    limit: int
    offset: int


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


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _under(cwd: str, repo_dir: str) -> bool:
    root = repo_dir.rstrip("/") or "/"
    return cwd == root or cwd.startswith(root.rstrip("/") + "/")


def backfill(
    rows: Iterable[SessionRow],
    containers: Iterable[ContainerInfo],
    recorded: Collection[tuple[str, str]],
) -> dict[tuple[str, str], str]:
    """``(agent, session id)`` → container name for sessions no recorder row
    names, from where and when each started; the rules are in the module
    docstring."""
    eligible = [
        (c.branch, _aware(c.created_at), c)
        for c in containers
        if c.branch is not None and c.created_at is not None and c.repo_dir is not None
    ]
    found: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row.agent, row.session_id)
        if key in recorded or row.git_branch is None or row.cwd is None or row.started is None:
            continue
        started = _aware(row.started)
        candidates = [
            c
            for branch, created, c in eligible
            if branch == row.git_branch
            and created <= started
            and c.repo_dir is not None
            and _under(row.cwd, c.repo_dir)
        ]
        if len(candidates) == 1:
            found[key] = candidates[0].name
    return found


def _newest_first(view: ContainerSessionView) -> float:
    return -_aware(view.last_activity).timestamp() if view.last_activity else float("inf")


def session_history(
    snapshot: Snapshot | None,
    name: str,
    scope: RemoteRepoScope,
    engine: Engine,
    repo_sessions: RepoSessions,
    limit: int = 20,
    offset: int = 0,
    query: str = "",
) -> ContainerSessionHistoryView | None:
    """The sessions the container ran before and is not running now, recorded
    and backfilled, newest activity first; None as `live_sessions`."""
    found = find_container(snapshot, name, scope)
    if found is None:
        return None
    group, c = found
    live = set(_live_keys(c))
    keys: dict[tuple[str, str], None] = {}
    for rec in recorded_sessions(engine, c.name, c.uuid):
        keys.setdefault((rec.agent, rec.session_id), None)
    listed = repo_sessions.list(group.prefix) or []
    attributed = backfill(listed, group.containers, recorded_keys(engine))
    for row in listed:
        if attributed.get((row.agent, row.session_id)) == c.name:
            keys.setdefault((row.agent, row.session_id), None)
    history = [key for key in keys if key not in live]
    views = _views(repo_sessions, group.prefix, history, live=False)
    needle = query.strip().casefold()
    if needle:
        views = [
            view
            for view in views
            if needle in view.title.casefold() or needle in view.session_id.casefold()
        ]
    views.sort(key=lambda view: (_newest_first(view), view.agent, view.session_id))
    return ContainerSessionHistoryView(
        container=c.name,
        repo=group.prefix,
        sessions=views[offset : offset + limit],
        total=len(views),
        limit=limit,
        offset=offset,
    )


def session_containers(
    snapshot: Snapshot | None, prefix: str, engine: Engine, rows: Iterable[SessionRow] = ()
) -> dict[tuple[str, str], str]:
    """``(agent, session id)`` → container name, over the repository's current containers.

    Recorded sessions go to the container that saw them last; ``rows`` the
    recorder never saw are attributed by `backfill`. A session whose
    container is gone, or that matches none, is absent (unattributed).
    """
    if snapshot is None:
        return {}
    for group in snapshot.groups:
        if group.prefix == prefix:
            found = attribution(engine, [(c.name, c.uuid) for c in group.containers])
            for key, owner in backfill(rows, group.containers, recorded_keys(engine)).items():
                found.setdefault(key, owner)
            return found
    return {}
