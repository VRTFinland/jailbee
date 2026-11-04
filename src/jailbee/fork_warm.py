"""Git-ignored output selection and best-effort warm fork seeding."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence


def validate_pattern(pattern: str) -> str:
    """Validate a repository-relative glob without shell interpretation."""
    value = pattern.strip().rstrip("/")
    parts = value.split("/")
    if (
        not value
        or pattern.strip().startswith("/")
        or not re.fullmatch(r"[A-Za-z0-9._@+*?\-/\[\]]+", value)
        or any(p in {"", ".", ".."} for p in parts)
        or parts[0] == ".git"
    ):
        raise ValueError(f"Invalid warm path pattern: {pattern!r}; use repo-relative globs")
    return value


def _matches(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return _matches(pattern[1:], path) or bool(path and _matches(pattern, path[1:]))
    return bool(path and fnmatch.fnmatchcase(path[0], pattern[0]) and _matches(pattern[1:], path[1:]))


def match_patterns(patterns: Sequence[str], ignored: Sequence[str]) -> tuple[list[str], list[str]]:
    """Select ignored entries in listing order, coalescing descendants."""
    normalized = [validate_pattern(p) for p in patterns]
    matched: list[str] = []
    found: set[str] = set()
    for entry in ignored:
        path = entry.rstrip("/")
        for pattern in normalized:
            if _matches(pattern.split("/"), path.split("/")):
                found.add(pattern)
                if path not in matched:
                    matched.append(path)
    selected = set(matched)
    return (
        [p for p in matched if not any("/".join(p.split("/")[:i]) in selected for i in range(1, len(p.split("/"))))],
        [p for p in normalized if p not in found],
    )
