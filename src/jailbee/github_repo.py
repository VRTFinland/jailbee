"""Pure helpers for canonical GitHub repository remotes."""

from __future__ import annotations

import posixpath
import re

_GITHUB_REMOTE_RE = re.compile(
    r"^(?P<prefix>https://github\.com/|ssh://(?:[^/@\s]+@)?github\.com/|"
    r"(?:[^@/:\s]+@)?github\.com:)"
    r"(?P<owner>[^/?#\s]+)/(?P<repo>[^/?#\s]+)/?$",
    re.IGNORECASE,
)


def _github_remote_parts(url: str) -> tuple[str, str, str] | None:
    match = _GITHUB_REMOTE_RE.fullmatch(url)
    if match is None:
        return None
    owner = match.group("owner")
    repo = match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if owner in {"", ".", ".."} or repo in {"", ".", ".."}:
        return None
    return match.group("prefix"), owner, repo


def github_slug(url: str) -> str | None:
    """Extract an ``owner/name`` slug from a supported GitHub remote URL."""
    parts = _github_remote_parts(url)
    if parts is None:
        return None
    _prefix, owner, repo = parts
    return f"{owner}/{repo}"


def resolve_submodule_url(parent_url: str, declared_url: str) -> str | None:
    """Resolve a Git-style relative submodule URL against its parent remote.

    Absolute supported GitHub URLs are returned unchanged. Relative URLs must
    use Git's explicit ``./`` or ``../`` form and must resolve to another
    two-component GitHub repository path.
    """
    if _github_remote_parts(declared_url) is not None:
        return declared_url
    if not declared_url.startswith(("./", "../")):
        return None

    parent = _github_remote_parts(parent_url)
    if parent is None:
        return None
    prefix, owner, repo = parent
    parent_repo = (
        f"{owner}/{repo}.git" if parent_url.rstrip("/").endswith(".git") else f"{owner}/{repo}"
    )
    resolved = posixpath.normpath(f"{parent_repo}/{declared_url}")
    parts = resolved.split("/")
    if len(parts) != 2 or any(part in {"", ".", ".."} for part in parts):
        return None
    return f"{prefix}{resolved}"
