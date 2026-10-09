"""`jailbee fork`: a new container at another container's committed state.

Only git state is carried over. The source's commits are fetched to the host
(`refs/jailbee/<source>/<branch>`, kept, so the new clone's `--shared`
alternates can always reach them) and the new container is an ordinary
`jailbee new`, pinned to that commit. A filesystem copy was rejected: it would
inherit the source's identity (labels, egress ACL, port devices, the `.local`
share device), which is exactly what must not be shared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


class ForkError(ValueError):
    """A fork that cannot be made; the message is user-facing."""


@dataclass(frozen=True)
class ForkSource:
    full_name: str  # source's full Incus name -> fork_of
    branch: str  # source's checked-out branch
    commit: str  # its HEAD, now reachable on the host -> clone_commit
    base_branch: str | None  # source's user.jailbee.base_branch -> base_branch_label


def prepare_fork(cfg: Config, incus: Incus, source: str) -> ForkSource:
    """Check ``source`` can be forked and bring its HEAD commit to the host.

    Refuses a stopped, mount-mode or clone-less source and one with
    uncommitted changes (tracked or untracked): a fork carries commits only,
    and silently dropping work would surprise. ``git.GitError`` from the fetch
    propagates; the caller reports it.
    """
    from jailbee import sync
    from jailbee.lifecycle import container_repo_dir, short_name

    try:
        full = sync.assert_container_publishable(cfg, incus, source)
        short = short_name(cfg, full)
        repo_dir = container_repo_dir(cfg, incus, full)
        if sync._container_status_dirty(incus, full, repo_dir, uid=cfg.container_user.uid):
            raise ForkError(
                f"uncommitted changes in '{short}': a fork carries commits only. "
                f"Commit or stash them in '{short}' first."
            )
        fetched = sync.fetch_from_container(cfg, incus, short)
    except ForkError:
        raise
    except (sync.SyncError, ValueError) as e:
        # resolve_container_name raises a plain ValueError for an unknown container.
        raise ForkError(str(e)) from e
    base = incus.config_get(full, "user.jailbee.base_branch") or None
    return ForkSource(full, fetched.branch, fetched.new_oid, base)
