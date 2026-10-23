"""Orphan containers: jailbee-managed containers whose repo cannot be located.

A container is an orphan when its repo prefix (``ContainerInfo.repo``) is
covered by no loadable registered repo and is not the current repo either —
the repo directory was deleted or moved, or its config no longer loads. The
dashboard files such containers under an ``(orphan)`` heading; every ordinary
command resolves names through the current repo's ``container_prefix`` and so
cannot reach them.

This module is the minimal, repo-independent handling: attach (tmux, shell),
start, stop and destroy, addressed by the full container name. Nothing here
reads a repo: there is none to read. In particular :func:`destroy_orphan` does
only the cleanup that needs no repo — guessing one (say, by faking a
``container_prefix`` onto the current config) would point `lifecycle.destroy_container`'s
transfer-ref pruning at the wrong repository.
"""

from __future__ import annotations

import logging
import os
import shlex
from typing import TYPE_CHECKING

from jailbee.config import CONTAINER_USERNAME, load_repo_config
from jailbee.incus import IncusError
from jailbee.lifecycle import list_containers
from jailbee.tui import warn

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.lifecycle import ContainerInfo

log = logging.getLogger(__name__)


def covered_prefixes(roots: Iterable[Path]) -> set[str]:
    """The container prefixes of every root whose config loads.

    An unloadable config covers nothing, exactly as in the dashboard's gather:
    its containers are orphans there too.
    """
    out: set[str] = set()
    for root in roots:
        try:
            out.add(load_repo_config(root).container_prefix)
        except Exception:  # OSError, YAML parse, Pydantic validation, no scratch
            log.debug("config for %s does not load", root, exc_info=True)
    return out


def list_orphans(cfg: Config, incus: Incus, roots: Iterable[Path]) -> list[ContainerInfo]:
    """jailbee-managed containers covered by neither ``roots`` nor ``cfg``.

    ``cfg`` is the caller's own repo (or the synthesized scratch config); it
    only drives the listing and counts as covered.
    """
    covered = covered_prefixes(roots) | {cfg.container_prefix}
    return [
        c
        for c in list_containers(cfg, incus, all_repos=True)
        if c.repo is not None and c.repo not in covered
    ]


def resolve_orphan(cfg: Config, incus: Incus, roots: Iterable[Path], name: str) -> ContainerInfo:
    """The orphan named ``name`` (in full), or ``ValueError`` saying why not."""
    found = next((c for c in list_orphans(cfg, incus, roots) if c.name == name), None)
    if found is not None:
        return found
    owned = next((c for c in list_containers(cfg, incus, all_repos=True) if c.name == name), None)
    if owned is not None:
        raise ValueError(
            f"'{name}' is not an orphan: it belongs to repo '{owned.repo}' — "
            "use the ordinary command from that repo"
        )
    raise ValueError(f"No jailbee container named '{name}'")


def container_ids(incus: Incus, name: str) -> tuple[int, int]:
    """The ``dev`` user's uid/gid inside ``name``.

    The repo that set ``container_user`` is gone, so ask the container. Falls
    back to the host's own ids — the ``container_user`` default.
    """
    try:
        uid = int(incus.exec(name, ["id", "-u", CONTAINER_USERNAME]).strip())
        gid = int(incus.exec(name, ["id", "-g", CONTAINER_USERNAME]).strip())
    except (IncusError, ValueError):
        return os.getuid(), os.getgid()
    return uid, gid


def _repo_dir(incus: Incus, name: str) -> str:
    """The in-container clone from the create-time label, else the dev home."""
    persisted = incus.config_get(name, "user.jailbee.repo_dir")
    if isinstance(persisted, str) and persisted:
        return persisted
    return f"/home/{CONTAINER_USERNAME}"


def _dev_env() -> dict[str, str]:
    home = f"/home/{CONTAINER_USERNAME}"
    return {"HOME": home, "USER": CONTAINER_USERNAME, "LOGNAME": CONTAINER_USERNAME}


def attach_tmux(incus: Incus, name: str) -> int:
    """Attach to the autostart tmux session, creating it on demand.

    Unlike `jailbee tmux` it never picks the agent window: which agents
    autostart was the lost repo's config.
    """
    from jailbee.tmux import SESSION_NAME, ensure_session

    ensure_session(incus, name, start_dir=_repo_dir(incus, name))
    uid, gid = container_ids(incus, name)
    return incus.exec_interactive(
        name,
        ["tmux", "attach", "-t", SESSION_NAME],
        uid=uid,
        gid=gid,
        env=_dev_env(),
        init_groups=True,
    )


def attach_shell(incus: Incus, name: str, user: str) -> int:
    """An interactive login shell as ``user`` (``dev`` or ``root``) in the clone."""
    inner = f"cd {shlex.quote(_repo_dir(incus, name))} 2>/dev/null; exec bash -l"
    if user == "root":
        uid, gid = 0, 0
        env = {"HOME": "/root", "USER": "root", "LOGNAME": "root"}
    else:
        uid, gid = container_ids(incus, name)
        env = _dev_env()
    return incus.exec_interactive(
        name, ["bash", "-c", inner], uid=uid, gid=gid, env=env, init_groups=True
    )


def _state(incus: Incus, name: str) -> str:
    for raw in incus.list_containers():
        if raw["name"] == name:
            return str(raw.get("status", "Stopped"))
    return "Stopped"


def _delete_job_row(name: str) -> None:
    """Drop any background-job row for ``name``; best-effort, like destroy."""
    try:
        from sqlmodel import Session

        from jailbee import background
        from jailbee.db import get_engine

        with Session(get_engine()) as session:
            background.delete_job(session, name)
    except Exception:
        log.debug("could not delete job row for %s", name, exc_info=True)


def destroy_orphan(incus: Incus, name: str, *, force: bool) -> None:
    """Stop (bounded), delete, and do the cleanup that needs no repo.

    Done: the per-container egress ACL and the background-job row. Left on
    purpose, since each needs the lost repo: pooled cache slots (reclaimed by
    the pool's own reconcile once the container is gone), private agent
    subpaths, ``refs/jailbee/*`` transfer refs, and the repo's bridge-extras
    union (`jailbee apply` sweeps orphan ACLs).
    """
    from jailbee import egress_scope
    from jailbee.stopping import stop_container

    if _state(incus, name) == "Running":
        stop_container(incus, name, force=force, force_fallback=True, show_progress=False)
    incus.delete(name, force=force)
    try:
        egress_scope.drop_container_acl(incus, name)
    except IncusError as e:
        warn(f"Could not remove egress ACL for '{name}' (continuing): {e}")
    _delete_job_row(name)
