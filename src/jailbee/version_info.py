"""Lightweight version labels shared by host, build hooks and container copies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

BUILD_INFO = "_build_info.json"
PACKAGE_DIR = Path(__file__).resolve().parent


class BuildIdentity(TypedDict):
    commit: str
    dirty: bool


def identity(package_dir: Path | None = None) -> BuildIdentity | None:
    """Resolve only this package's checkout, never the caller's working tree."""
    package = package_dir if package_dir is not None else PACKAGE_DIR
    root = package.parent.parent
    if package.parent.name == "src" and (root / ".git").exists():
        from jailbee.git import source_identity

        return source_identity(root)
    try:
        data = json.loads((package / BUILD_INFO).read_text())
        commit, dirty = data["commit"], data["dirty"]
        if (
            isinstance(commit, str)
            and len(commit) in {40, 64}
            and all(c in "0123456789abcdef" for c in commit)
            and isinstance(dirty, bool)
        ):
            return {"commit": commit, "dirty": dirty}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def display_version(version: str, *, package_dir: Path | None = None) -> str:
    """Keep the package version intact; add provenance only to human output."""
    info = identity(package_dir)
    if info is None:
        return version
    suffix = "-dirty" if info["dirty"] else ""
    return f"{version} ({info['commit'][:7]}{suffix})"
