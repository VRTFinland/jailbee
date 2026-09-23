"""Pure helpers for canonical GitHub repository remotes."""

from __future__ import annotations

import posixpath
import re

# Non-ASCII separator/control characters that Unicode-aware `\s` matches but
# ASCII-only `\s` does not (re.ASCII narrows `\s` to `[ \t\n\r\f\v]`, see
# below). Re-excluded explicitly in the userinfo classes so that switching to
# re.ASCII — needed to stop Unicode case-folding homographs of "github.com"
# (e.g. U+0131 dotless i) — does not also widen what userinfo accepts.
_NON_ASCII_SPACE = r"\x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000"

_GITHUB_REMOTE_RE = re.compile(
    r"^(?P<prefix>https://(?:[^/@\s#?\\" + _NON_ASCII_SPACE + r"]+@)?github\.com/|"
    r"git://github\.com/|"
    r"ssh://(?:[^/@\s" + _NON_ASCII_SPACE + r"]+@)?github\.com/|"
    r"(?:[^@/:\s" + _NON_ASCII_SPACE + r"]+@)?github\.com:)"
    r"(?P<owner>[^/?#\s]+)/(?P<repo>[^/?#\s]+)/?$",
    re.IGNORECASE | re.ASCII,
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

    The result reuses the parent remote's ``prefix``, which may carry
    credentials (e.g. ``https://x-access-token:TOKEN@github.com/``) — a
    future caller must not log or display the returned URL verbatim.
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
