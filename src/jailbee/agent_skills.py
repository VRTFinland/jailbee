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
import posixpath
import shutil
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from jailbee.config import CONTAINER_USERNAME
from jailbee.tui import warn_plain

if TYPE_CHECKING:
    from jailbee.config import AgentConfig, Config
    from jailbee.config.models_agents import AgentSharedMount


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


def _preset_skill_locations() -> list[tuple[str, str]]:
    """(binary, skills_dir) for every preset that declares a skills directory.

    The host-side detection table, read from the presets so a new skill-capable
    agent needs a preset entry, not a change here.
    """
    from jailbee.agent_presets import AGENT_PRESETS, claude_preset

    presets: dict[str, dict[str, object]] = {**AGENT_PRESETS, "claude": claude_preset()}
    out: list[tuple[str, str]] = []
    for name in sorted(presets):
        entry = presets[name]
        skills_dir = entry.get("skills_dir")
        if isinstance(skills_dir, str) and skills_dir:
            binary = str(entry.get("command", name)).split()[0]
            out.append((binary, skills_dir))
    return out


def host_skill_targets() -> list[Path]:
    """Skills directories of the skill-capable agents installed on this host.

    `shutil.which` on each preset's binary decides: an agent the user has not
    installed on the host gets no directory written. Container-side skills are
    unaffected — they ride the shared mounts, not this.
    """
    return [
        Path(skills_dir).expanduser()
        for binary, skills_dir in _preset_skill_locations()
        if shutil.which(binary)
    ]


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


def install_host_skills(targets: Sequence[Path]) -> list[Path]:
    """Copy the bundled skills into each of ``targets``, returning what was written.

    The counterpart to `sync_agent_skills`, which serves the *containers*: this
    one teaches the agents the user runs on the host about `jailbee` itself.
    Callers pass `host_skill_targets()` — the detection and the opt-in policy
    live in `setup_command`, not here. Installed by `jailbee setup`; it used to
    be `make install-skill`, which meant a PyPI install never got them.

    A target that cannot be written (a path owned by another user, a file where
    a directory is needed) warns and is skipped: `jailbee setup` used to die
    mid-run on the first one, leaving every later agent unwritten.
    """
    written: list[Path] = []
    for dest in targets:
        try:
            written.extend(_copy_skills_into(dest))
        except OSError as exc:
            warn_plain(f"agent skills: cannot write to {dest}: {exc}")
    return written


def sync_agent_skills(cfg: Config) -> None:
    """Copy each bundled skill into every enabled agent's shared skills dir.

    One host-side destination per agent with a `skills_dir` that some `shared`
    mount covers: claude's is ``<shared_dir>/claude/skills``, codex's
    ``<shared_dir>/codex/skills``, and so on. A `skills_dir` no mount covers —
    or one a `private` carve-out would hide, or one escaping the shared dir —
    is a config mistake: warned, skipped, never fatal, and never at the other
    agents' expense. A destination that cannot be written warns the same way,
    for the same reason. A host-side flock serializes concurrent ``jailbee
    new`` runs sharing the mount; see `_copy_skills_into` for the replacement
    semantics.
    """
    assert cfg.shared_dir is not None  # set by load_config
    targets: set[Path] = set()
    for name in sorted(cfg.agents):
        agent = cfg.agents[name]
        if not agent.enabled or not agent.install_jailbee_skills or not agent.skills_dir:
            continue
        host_dir, why = _skills_host_dir(cfg.shared_dir, agent)
        if host_dir is None:
            warn_plain(f"agents.{name}: {why} — skipping jailbee skills for this agent")
            continue
        # Two agents may resolve to the same directory (claude and a custom
        # agent reading ~/.claude/skills, say); they owe the same bytes, so the
        # directory is copied once, never layered.
        targets.add(host_dir)
    if not targets:
        return
    lock_path = cfg.shared_dir / ".jailbee-skills.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            for host_dir in sorted(targets):
                # Per destination, as on the host side: one unwritable
                # directory must not cost every later agent its skills.
                try:
                    _copy_skills_into(host_dir)
                except OSError as exc:
                    warn_plain(f"agent skills: cannot write to {host_dir}: {exc}")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _container_abs(path: str) -> PurePosixPath:
    """A mount path or `skills_dir` in its absolute container-side spelling.

    Both `AgentSharedMount.path` and `AgentConfig.skills_dir` accept a
    `~`-relative *or* an absolute path, and one config may legitimately mix
    the two — `/home/<user>/.mine` mounted, `~/.mine/skills` the skills dir.
    Comparing the raw strings would call that pair unrelated and silently
    skip the agent, so both sides are expanded here first, the same
    substitution `profiles.container_path_env` and `agent_private`'s
    `_container_path` make. Anything neither `~`-relative nor absolute is
    left alone: it cannot match an absolute mount, and the caller's warning
    is the honest answer.
    """
    if path == "~" or path.startswith("~/"):
        path = posixpath.join(f"/home/{CONTAINER_USERNAME}", path[2:])
    return PurePosixPath(posixpath.normpath(path))


def _private_entry_hiding(mount: AgentSharedMount, rel: PurePosixPath) -> str | None:
    """The mount's `private` entry that would hide `rel`, if any.

    A `private` subpath is mounted over with a per-container directory once
    the container is up (`agent_private.attach`), so anything the sync writes
    underneath one is invisible to every agent — the copy would succeed and
    the skills would simply never appear. Only an entry that *contains*
    `rel` hides it; a sibling carve-out (codex's socket directories beside
    its skills dir) is unrelated.
    """
    for entry in mount.private:
        if rel.is_relative_to(PurePosixPath(entry)):
            return entry
    return None


def _skills_host_dir(shared_dir: Path, agent: AgentConfig) -> tuple[Path | None, str]:
    """The host-side copy of `agent.skills_dir`, or `(None, reason)`.

    Walks the agent's `shared` mounts and picks the one whose `path` prefixes
    `skills_dir` most deeply, both normalised by `_container_abs` first. The
    deepest match wins because the narrowest mount shadows the broad ones
    in-container: with ``~`` and ``~/.claude`` both mounted and
    ``~/.claude/skills`` the skills dir, writing under ``~``'s subpath would be
    hidden behind ``~/.claude`` and the agent would never see the skills. The
    remainder maps onto the mount's `subpath` under `<shared_dir>`:
    ``~/.claude/skills`` over ``~/.claude`` (subpath ``claude``) lands at
    ``<shared_dir>/claude/skills``. Matching against the mount list rather
    than a hardcoded subpath is what makes a user's own mount layout work.

    The reason string is the caller's warning body; every rejection names the
    `skills_dir` as the user spelled it.
    """
    spelling = agent.skills_dir or ""
    skills = _container_abs(spelling)
    best: tuple[int, AgentSharedMount, PurePosixPath] | None = None
    for mount in agent.shared:
        if mount.type != "dir":
            continue
        base = _container_abs(mount.path)
        try:
            rel = skills.relative_to(base)
        except ValueError:
            continue
        # Ties keep the first: a strictly deeper base wins.
        depth = len(base.parts)
        if best is None or depth > best[0]:
            best = (depth, mount, rel)
    if best is None:
        return None, f"skills_dir {spelling} matches no shared mount"
    _, mount, rel = best
    hidden_by = _private_entry_hiding(mount, rel)
    if hidden_by is not None:
        return None, (
            f"skills_dir {spelling} sits under the private subpath {hidden_by!r} of "
            f"shared mount {mount.path} — a per-container directory is mounted over "
            "it, so no agent would ever see the skills"
        )
    candidate = (
        shared_dir / mount.subpath
        if not rel.parts
        else shared_dir / mount.subpath / Path(*rel.parts)
    )
    # Defense in depth: a `..` (or an absolute path) in a mount's `subpath`
    # reaches this join without passing through `AgentConfig.skills_dir`
    # validation. A candidate escaping `<shared_dir>` would be `mkdir`d and
    # `rmtree`d by `_copy_skills_into`, so drop it and let the caller warn.
    if not candidate.resolve().is_relative_to(shared_dir.resolve()):
        return None, (
            f"skills_dir {spelling} resolves outside the shared dir via the "
            f"subpath {mount.subpath!r}"
        )
    return candidate, ""
