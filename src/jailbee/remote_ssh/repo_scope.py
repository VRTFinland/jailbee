"""Immutable repository visibility policy for an SSH session."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlmodel import Session, select

from jailbee.db import get_engine
from jailbee.db.models import RegisteredRepo
from jailbee.remote_ssh.session import SSH_EXCLUDED_REPOS_ENV, is_ssh_session

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.engine import Engine

_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class RepoScopeError(ValueError):
    """An SSH child has no trustworthy repository policy snapshot."""


@dataclass(frozen=True)
class RemoteRepoScope:
    excluded: frozenset[str]

    def allows(self, prefix: str | None) -> bool:
        return prefix is None or prefix not in self.excluded


@dataclass(frozen=True)
class RepoChoice:
    prefix: str
    root: Path


def registered_repos(
    *, engine: Engine | None = None, scope: RemoteRepoScope | None = None
) -> list[RepoChoice]:
    """Return registered repositories whose host directories still exist."""
    with Session(engine or get_engine()) as session:
        rows = session.exec(select(RegisteredRepo)).all()
    return sorted(
        [
            RepoChoice(row.container_prefix, Path(row.repo_root))
            for row in rows
            if Path(row.repo_root).is_dir()
            and (scope is None or scope.allows(row.container_prefix))
        ],
        key=lambda repo: repo.prefix,
    )


def scope_for_session(environ: Mapping[str, str] | None = None) -> RemoteRepoScope:
    """Read the server-created snapshot, requiring one for every SSH child."""
    env = os.environ if environ is None else environ
    if not is_ssh_session(env):
        return RemoteRepoScope(frozenset())
    raw = env.get(SSH_EXCLUDED_REPOS_ENV)
    try:
        values = json.loads(raw) if raw is not None else None
    except (json.JSONDecodeError, TypeError) as exc:
        raise RepoScopeError("Invalid SSH repository policy snapshot") from exc
    if (
        not isinstance(values, list)
        or any(not isinstance(value, str) or not _PREFIX_RE.fullmatch(value) for value in values)
        or len(values) != len(set(values))
    ):
        raise RepoScopeError("Invalid SSH repository policy snapshot")
    return RemoteRepoScope(frozenset(values))
