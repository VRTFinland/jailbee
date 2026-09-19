"""Sync jailbee's bundled agent skills into the shared skills directories.

Each enabled agent's config home (``~/.claude``, ``~/.codex``, …) is a shared
bind mount under ``<shared_dir>`` common to every container of a repo, and
``raw.idmap`` is 1:1, so files the host dev user writes into e.g.
``<shared_dir>/claude/skills/`` appear correctly owned inside every container.
Writing them here once therefore updates the in-container view for all
containers — no ``incus exec`` or byte-transfer needed.
"""

from __future__ import annotations

import fcntl
import importlib.resources
import shutil
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from jailbee.tui import warn

if TYPE_CHECKING:
    from jailbee.config import AgentConfig, Config


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
    # agent_skills.py -> jailbee -> src -> repo root
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

    The counterpart to `sync_agent_skills`, which serves the *containers*:
    this one teaches the Claude the user runs on the host about `jailbee`
    itself. Installed by `jailbee setup`; it used to be `make install-skill`,
    which meant a PyPI install never got them.
    """
    return _copy_skills_into(host_skills_dir())


def sync_agent_skills(cfg: Config) -> None:
    """Copy each bundled skill into every enabled agent's shared skills dir.

    One host-side destination per agent with a `skills_dir` that some `shared`
    mount covers: claude's is ``<shared_dir>/claude/skills``, codex's
    ``<shared_dir>/codex/skills``, and so on. A `skills_dir` no mount covers is
    a config mistake — warned, skipped, never fatal. A host-side flock
    serializes concurrent ``jailbee new`` runs sharing the mount; see
    `_copy_skills_into` for the replacement semantics.
    """
    assert cfg.shared_dir is not None  # set by load_config
    targets: dict[Path, str] = {}
    for name in sorted(cfg.agents):
        agent = cfg.agents[name]
        if not agent.enabled or not agent.install_jailbee_skills or not agent.skills_dir:
            continue
        host_dir = _skills_host_dir(cfg.shared_dir, agent)
        if host_dir is None:
            warn(
                f"agents.{name}: skills_dir {agent.skills_dir} matches no shared "
                "mount — skipping jailbee skills for this agent"
            )
            continue
        # First declaration wins: two agents resolving to the same directory
        # (claude and a custom agent reading ~/.claude/skills, say) owe the
        # same bytes, so the second copy is skipped, not layered.
        targets.setdefault(host_dir, name)
    if not targets:
        return
    lock_path = cfg.shared_dir / ".jailbee-skills.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            for host_dir in sorted(targets):
                _copy_skills_into(host_dir)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _skills_host_dir(shared_dir: Path, agent: AgentConfig) -> Path | None:
    """The host-side copy of `agent.skills_dir`, or None when nothing covers it.

    Walks the agent's `shared` mounts and picks the first whose `path` prefixes
    `skills_dir` (both `~`-relative by convention, so the match is textual).
    The remainder maps onto the mount's `subpath` under `<shared_dir>`:
    ``~/.claude/skills`` over ``~/.claude`` (subpath ``claude``) lands at
    ``<shared_dir>/claude/skills``. Matching against the mount list rather
    than a hardcoded subpath is what makes a user's own mount layout work.
    """
    skills = PurePosixPath(agent.skills_dir or "")
    for mount in agent.shared:
        if mount.type != "dir":
            continue
        base = PurePosixPath(mount.path)
        try:
            rel = skills.relative_to(base)
        except ValueError:
            continue
        if not rel.parts:
            return shared_dir / mount.subpath
        return shared_dir / mount.subpath / Path(*rel.parts)
    return None
