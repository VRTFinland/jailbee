"""Sync jailbee's bundled Claude Code skills into the shared ``~/.claude/skills``.

``~/.claude`` is a shared bind mount (``<shared_dir>/claude``) common to every
container of a repo, and ``raw.idmap`` is 1:1, so files the host dev user writes
into ``<shared_dir>/claude/skills/`` appear correctly owned inside every
container. Writing them here once therefore updates the in-container view for
all containers — no ``incus exec`` or byte-transfer needed.
"""

from __future__ import annotations

import fcntl
import importlib.resources
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.config import Config


def _skills_root() -> Path:
    """Locate the bundled skills directory.

    Wheel installs carry the skills as package data (``jailbee/skills``,
    force-included from ``docs/skills`` at build time). Editable/dev installs run
    from ``src/`` where that directory does not exist, so fall back to the repo's
    ``docs/skills``. Both locations hold byte-identical content.
    """
    packaged = Path(str(importlib.resources.files("jailbee"))) / "skills"
    if packaged.is_dir():
        return packaged
    # claude_skills.py -> jailbee -> src -> repo root
    return Path(__file__).resolve().parents[2] / "docs" / "skills"


def bundled_skill_names() -> list[str]:
    """Names of the skills this install ships, sorted. Empty if none are found."""
    root = _skills_root()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def host_skills_dir() -> Path:
    """Where Claude Code on the *host* reads user skills from.

    Resolved on each call, not at import: tests point ``HOME`` elsewhere.
    """
    return Path.home() / ".claude" / "skills"


def _copy_skills_into(dest: Path) -> list[Path]:
    """Replace each bundled skill under ``dest``, returning what was written.

    Each managed skill subdirectory is removed first, so files dropped
    upstream disappear instead of lingering; unrelated skills in ``dest``
    are left alone.
    """
    root = _skills_root()
    if not root.is_dir():
        return []
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for skill in sorted(p for p in root.iterdir() if p.is_dir()):
        target = dest / skill.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill, target)
        written.append(target)
    return written


def install_host_skills() -> list[Path]:
    """Install the bundled skills for the host's own Claude Code.

    The counterpart to `sync_jailbee_skills`, which serves the *containers*:
    this one teaches the Claude the user runs on the host about `jailbee`
    itself. Installed by `jailbee setup`; it used to be `make install-skill`,
    which meant a PyPI install never got them.
    """
    return _copy_skills_into(host_skills_dir())


def sync_jailbee_skills(cfg: Config) -> None:
    """Copy each bundled skill into ``<shared_dir>/claude/skills/<name>/``.

    No-op unless ``claude.enabled`` and ``claude.install_jailbee_skills``. A
    host-side flock serializes concurrent ``jailbee new`` runs sharing the
    mount; see `_copy_skills_into` for the replacement semantics.
    """
    if not cfg.claude.enabled or not cfg.claude.install_jailbee_skills:
        return
    assert cfg.shared_dir is not None  # set by load_config
    lock_path = cfg.shared_dir / "claude" / ".jailbee-skills.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            _copy_skills_into(cfg.shared_dir / "claude" / "skills")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
