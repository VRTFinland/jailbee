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


def sync_jailbee_skills(cfg: Config) -> None:
    """Copy each bundled skill into ``<shared_dir>/claude/skills/<name>/``.

    No-op unless ``claude.enabled`` and ``claude.install_jailbee_skills``. Each
    managed skill subdirectory is fully replaced so files removed upstream also
    disappear; unrelated skills in the shared directory are left alone. A
    host-side flock serializes concurrent ``jailbee new`` runs sharing the mount.
    """
    if not cfg.claude.enabled or not cfg.claude.install_jailbee_skills:
        return
    assert cfg.shared_dir is not None  # set by load_config
    root = _skills_root()
    if not root.is_dir():
        return
    skills_dir = cfg.shared_dir / "claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.shared_dir / "claude" / ".jailbee-skills.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            for skill in sorted(p for p in root.iterdir() if p.is_dir()):
                target = skills_dir / skill.name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(skill, target)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
