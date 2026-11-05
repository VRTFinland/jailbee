"""Discover registered repositories and their remaining Incus resources."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from sqlmodel import select

from jailbee.config import load_repo_config
from jailbee.config.errors import ConfigError
from jailbee.db.models import RegisteredRepo
from jailbee.maintenance import humanize
from jailbee.paths import repo_config_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlmodel import Session

    from jailbee.incus import Incus
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope


@dataclass(frozen=True)
class RepoEntry:
    prefix: str
    repo_root: Path | None
    state: str
    containers: int
    base: str | None


def list_repos(
    incus: Incus, session_factory: Callable[[], Session], *, scope: RemoteRepoScope | None = None
) -> list[RepoEntry]:
    with session_factory() as session:
        rows = session.exec(select(RegisteredRepo)).all()
    registered = {row.container_prefix: row for row in rows}
    prefixes = set(registered)
    prefixes.update(
        profile[:-5]
        for profile in incus.profile_list()
        if profile.endswith("-base") and profile != "default" and profile[:-5]
    )
    instances = incus.list_containers(fast=True)
    counts: dict[str, int] = {}
    names = {raw.get("name") for raw in instances}
    for raw in instances:
        for profile in raw.get("profiles") or []:
            if profile.endswith("-base") and profile != "default":
                prefix = profile[:-5]
                counts[prefix] = counts.get(prefix, 0) + 1
                break
    sizes = {
        alias.get("name"): int(image.get("size", 0) or 0)
        for image in incus.list_images()
        for alias in image.get("aliases", [])
    }
    result = []
    for prefix in sorted(prefixes):
        if scope is not None and not scope.allows(prefix):
            continue
        row = registered.get(prefix)
        root = Path(row.repo_root) if row else None
        state = "leftover"
        alias = f"{prefix}-base"
        if row is not None and root is not None:
            if not root.is_dir():
                state = "dir missing"
            else:
                state = "no config"
                if row.synthetic_config or repo_config_path(root) is not None:
                    try:
                        cfg = load_repo_config(root)
                        if cfg.container_prefix == prefix:
                            alias = cfg.golden.alias
                            state = "ok"
                    except (ConfigError, OSError, ValueError):
                        pass
        base = humanize(sizes[alias]) if alias in sizes else None
        if base is None and f"{alias}--golden" in names:
            base = "instance"
        result.append(RepoEntry(prefix, root, state, counts.get(prefix, 0), base))
    return result
