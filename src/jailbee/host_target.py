"""Read-only resolution of the host branch used for status comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jailbee import git

TargetSource = Literal["local", "tracking", "unavailable"]
TrackingRelation = Literal["equal", "local-ahead", "tracking-ahead", "diverged", "unavailable"]


@dataclass(frozen=True)
class TargetSnapshot:
    """Resolved host target ref and its relation to the fetched upstream ref."""

    branch: str
    sha: str | None
    source: TargetSource
    upstream_ref: str
    tracking_relation: TrackingRelation


def resolve_target(repo_root: Path, branch: str, upstream_remote: str) -> TargetSnapshot:
    """Resolve a target from the local branch, falling back to its tracking ref.

    The current checkout is deliberately irrelevant: this reads named refs only
    and never contacts the remote or changes repository state.
    """
    local_sha = git.rev_parse(repo_root, f"refs/heads/{branch}")
    upstream_ref = f"refs/remotes/{upstream_remote}/{branch}"
    tracking_sha = git.rev_parse(repo_root, upstream_ref)

    if local_sha is None:
        return TargetSnapshot(
            branch=branch,
            sha=tracking_sha,
            source="tracking" if tracking_sha is not None else "unavailable",
            upstream_ref=upstream_ref,
            tracking_relation="unavailable",
        )

    if tracking_sha is None:
        relation: TrackingRelation = "unavailable"
    elif local_sha == tracking_sha:
        relation = "equal"
    else:
        local_is_ancestor = git.is_ancestor(repo_root, local_sha, tracking_sha)
        tracking_is_ancestor = git.is_ancestor(repo_root, tracking_sha, local_sha)
        if local_is_ancestor is None or tracking_is_ancestor is None:
            relation = "unavailable"
        elif local_is_ancestor:
            relation = "tracking-ahead"
        elif tracking_is_ancestor:
            relation = "local-ahead"
        else:
            relation = "diverged"

    return TargetSnapshot(
        branch=branch,
        sha=local_sha,
        source="local",
        upstream_ref=upstream_ref,
        tracking_relation=relation,
    )


SyncKind = Literal["host", "pr"]


@dataclass(frozen=True)
class SyncTarget:
    """The ref a container's DIFF/↑/↓/MERGE compare against (its sync target).

    Chosen automatically: a PR-bound container syncs with its PR head, anything
    else with its host branch. ``fallback`` marks a host-branch target standing
    in for one that could not be resolved (a PR-bound container whose PR head
    the host has never fetched). ``label`` is what the user reads: the branch
    name, or ``PR #N``; ``ref`` the host ref it came from.

    The last four fields are facts about keeping a PR head current, filled in
    by the state service (`pr_autofetch`): ``autofetch`` is whether the repo
    refetches it on its own, ``fetched_at`` when that last succeeded (epoch
    seconds), ``fetch_error`` and ``fetch_error_at`` the last failure, if the
    latest attempt failed.
    """

    kind: SyncKind
    label: str
    sha: str | None
    ref: str
    fallback: bool = False
    autofetch: bool = True
    fetched_at: float | None = None
    fetch_error: str | None = None
    fetch_error_at: float | None = None


def host_sync_target(host: TargetSnapshot, *, fallback: bool = False) -> SyncTarget:
    """The host branch as a sync target."""
    ref = f"refs/heads/{host.branch}" if host.source != "tracking" else host.upstream_ref
    return SyncTarget("host", host.branch, host.sha, ref, fallback)


def resolve_sync_target(
    repo_root: Path | None, pr_number: int | None, host: TargetSnapshot
) -> SyncTarget:
    """Choose a container's sync target from its PR binding and host branch.

    ``repo_root`` is None when the container's repository cannot be loaded;
    then only the (already unavailable) host snapshot is used. Reads refs
    only, never fetches.
    """
    if pr_number is None:
        return host_sync_target(host)
    from jailbee.pr import pr_head_ref

    ref = pr_head_ref(pr_number)
    sha = git.rev_parse(repo_root, ref) if repo_root is not None else None
    if sha is None:
        return host_sync_target(host, fallback=True)
    return SyncTarget("pr", f"PR #{pr_number}", sha, ref)
