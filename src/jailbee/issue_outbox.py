"""Host-side orchestration for GitHub issue outbox actions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee import git, submodules
from jailbee.github_repo import github_slug, resolve_submodule_url

if TYPE_CHECKING:
    from jailbee.config import Config


@dataclass(frozen=True)
class RepoTarget:
    """A host-authorized repository an issue manifest may name."""

    path: str
    repo_root: Path
    slug: str


def _parent_target_path(path: str, remote_urls: Mapping[str, str]) -> str:
    parents = [
        candidate
        for candidate in remote_urls
        if candidate != "." and path.startswith(f"{candidate}/")
    ]
    return max(parents, key=len, default=".")


def resolve_repo_targets(cfg: Config) -> Mapping[str, RepoTarget]:
    """Return ``.`` plus safe host-declared submodule targets keyed by path."""
    root_url = git.get_remote_url(cfg.repo_root, cfg.upstream_remote)
    root_slug = github_slug(root_url or "")
    if root_url is None or root_slug is None:
        raise ValueError("superproject upstream remote must resolve to a GitHub repository")

    targets: dict[str, RepoTarget] = {
        ".": RepoTarget(path=".", repo_root=cfg.repo_root, slug=root_slug)
    }
    remote_urls = {".": root_url}
    for declared in submodules.declared_submodule_remotes(cfg.repo_root):
        repo_root = cfg.repo_root / declared.path
        if submodules.host_subrepo_exists(cfg.repo_root, declared.path):
            remote = git.detect_upstream_remote(repo_root)
            if remote is None:
                raise ValueError(f"submodule '{declared.path}' has no detectable upstream remote")
            remote_url = git.get_remote_url(repo_root, remote)
            if remote_url is None:
                raise ValueError(
                    f"submodule '{declared.path}' has no URL for upstream remote '{remote}'"
                )
        else:
            parent_path = _parent_target_path(declared.path, remote_urls)
            remote_url = resolve_submodule_url(remote_urls[parent_path], declared.url)

        slug = github_slug(remote_url or "")
        if remote_url is None or slug is None:
            raise ValueError(
                f"submodule '{declared.path}' remote must resolve to a GitHub repository"
            )
        targets[declared.path] = RepoTarget(declared.path, repo_root, slug)
        remote_urls[declared.path] = remote_url

    return targets
