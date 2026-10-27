"""`jailbee fork`: a new container at another container's committed state.

Git state and PR bindings are carried over. Bindings are adopted rather than
attributed to the fork; review bindings retain their original identity. The
source's commits are fetched to the host
(`refs/jailbee/<source>/<branch>`), its submodules' commits into the host
sub-repos (`refs/jailbee-sub/<source>/...`), and the new container is an ordinary
`jailbee new`, pinned to that commit. That source ref is not what keeps the
commit alive: `jailbee destroy <source>` deletes it and the source's next fetch
moves it, while the fork's `--shared` clone borrows its objects from the host
for as long as the fork exists. So the commit is also pinned under the fork's
own name (`refs/jailbee/<fork>/HEAD`, see `pin_fork_commit`), which lives
exactly as long as the fork: `jailbee destroy <fork>` removes it with the rest
of `refs/jailbee/<fork>/`. A filesystem copy was rejected: it would
inherit the source's identity (labels, egress ACL, port devices, the `.local`
share device), which is exactly what must not be shared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    # The source was made by `jailbee new --pr`, so its commits may be a PR
    # author's, whose `.jailbee/config.yaml` autostart must not run unasked
    # -> untrusted_head. Conservative: any PR, not just a cross-repo one,
    # because whether it was cross-repo is not recorded on the container.
    untrusted: bool = False
    pr_labels: dict[str, str] = field(default_factory=dict)


def inherited_pr_labels(incus: Incus, full: str) -> dict[str, str]:
    """Transform source bindings without recording a new PR decision."""
    from jailbee.tui import warn

    result: dict[str, str] = {}
    for prefix in ("user.jailbee.pr", "user.jailbee.stacked_pr"):
        number = incus.config_get(full, prefix)
        branch = incus.config_get(full, prefix + "_branch")
        author = incus.config_get(full, prefix + "_author")
        adopted = incus.config_get(full, prefix + "_adopted")
        if number:
            try:
                int(number)
            except (ValueError, TypeError):
                warn(f"Skipping invalid PR label {prefix}: {number}")
                continue
        if number:
            result[prefix] = number
        if branch:
            result[prefix + "_branch"] = branch
        if (number or branch) and not (number and not author and not adopted):
            result[prefix + "_adopted"] = "1"
        if prefix.endswith("stacked_pr"):
            base = incus.config_get(full, prefix + "_base")
            if base:
                result[prefix + "_base"] = base

    raw = incus.config_get(full, "user.jailbee.sub_pr")
    if raw:
        try:
            entries = json.loads(raw)
            if isinstance(entries, dict) and all(isinstance(v, dict) for v in entries.values()):
                for entry in entries.values():
                    entry.update(author=False, adopted=True)
                result["user.jailbee.sub_pr"] = json.dumps(entries, sort_keys=True)
        except (ValueError, TypeError):
            pass
    return result


def prepare_fork(cfg: Config, incus: Incus, source: str) -> ForkSource:
    """Check ``source`` can be forked and bring its HEAD commit to the host.

    Refuses a stopped, mount-mode or clone-less source and one with
    uncommitted changes (tracked or untracked): a fork carries commits only,
    and silently dropping work would surprise. ``git.GitError`` from the fetch
    propagates; the caller reports it.
    """
    from jailbee import submodules, sync
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
        # The fork inits its submodules from the host sub-repos, so a gitlink
        # committed in the source must reach them first, as on `jailbee git pull`.
        submodules.transport_submodules_to_host(cfg, incus, full, short, repo_dir=repo_dir)
    except ForkError:
        raise
    except (sync.SyncError, ValueError) as e:
        # resolve_container_name raises a plain ValueError for an unknown container.
        raise ForkError(str(e)) from e
    untrusted = bool(incus.config_get(full, "user.jailbee.pr"))
    base = incus.config_get(full, "user.jailbee.base_branch") or None
    return ForkSource(
        full, fetched.branch, fetched.new_oid, base, untrusted, inherited_pr_labels(incus, full)
    )


def fork_pin_ref(cfg: Config, fork_full: str) -> str:
    """The host ref that keeps a fork's starting commit reachable.

    Under `refs/jailbee/<fork-short>/`, the prefix `destroy_container` clears,
    so the pin lives exactly as long as the fork. ``HEAD`` because it can never
    be a branch name, so no `jailbee git fetch` of the fork lands on it.
    """
    from jailbee.lifecycle import short_name

    return f"refs/jailbee/{short_name(cfg, fork_full)}/HEAD"


def pin_fork_commit(cfg: Config, fork_full: str, commit: str) -> str:
    """Point the fork's pin ref at ``commit``; return the ref.

    Raises ``git.GitError`` when git refuses: an unpinned fork's `--shared`
    clone could lose its objects to a host `git gc`, so the caller must stop.
    """
    from jailbee import git

    ref = fork_pin_ref(cfg, fork_full)
    if not git.update_ref(cfg.repo_root, ref, commit):
        raise git.GitError(f"could not pin the fork's commit {commit} as {ref} on the host.")
    return ref
