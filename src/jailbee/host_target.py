"""Read-only resolution of the host branch used for status comparisons."""

from __future__ import annotations

from dataclasses import dataclass, replace
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


SyncKind = Literal["host", "pr", "parent"]


@dataclass(frozen=True)
class SyncTarget:
    """The ref a container's DIFF/↑/↓/MERGE compare against (its sync target).

    Chosen automatically: a child or fork syncs with its parent container, a
    PR-bound container with its PR head, anything else with its host branch.
    ``fallback`` marks a host-branch target standing in for one that could not
    be resolved (a PR head the host has never fetched; a parent that is
    stopped, missing or not yet seen by a gather pass). ``label`` is what the
    user reads: the branch name, ``PR #N`` or the parent's short name; ``ref``
    the host ref it came from (empty for a parent).

    ``parent`` is the parent's short name, set on a parent target and on the
    host-branch fallback standing in for one. A parent target's ``sha`` is the
    parent's HEAD as the previous gather pass saw it, ``seen_at`` when that
    pass ran (epoch seconds); ``ahead_sha`` is the newest commit the parent
    certainly has of the child's (its last import, else the fork pin), from
    which ↑ counts. None leaves ↑ unknown.

    The four fetch fields (``autofetch`` to ``fetch_error_at``) are facts about
    keeping a PR head current, filled in
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
    parent: str | None = None
    ahead_sha: str | None = None
    seen_at: float | None = None


def host_sync_target(host: TargetSnapshot, *, fallback: bool = False) -> SyncTarget:
    """The host branch as a sync target."""
    ref = f"refs/heads/{host.branch}" if host.source != "tracking" else host.upstream_ref
    return SyncTarget("host", host.branch, host.sha, ref, fallback)


@dataclass(frozen=True)
class ParentHead:
    """A parent container's HEAD as one gather pass saw it, and when (epoch seconds)."""

    sha: str
    seen_at: float


@dataclass(frozen=True)
class SyncInputs:
    """What a container's sync target is chosen from, besides its host branch.

    ``parent`` is the short name of the container it was spawned by or forked
    from; ``parent_head`` that parent's HEAD from the previous gather pass,
    None when the parent was stopped, missing or not yet seen. ``last_import``
    is the orchestration label of the child's last imported commit, and
    ``fork_pin_ref`` the host ref pinning a fork's starting commit: the ↑ base
    is the first of the two that resolves.
    """

    pr_number: int | None = None
    parent: str | None = None
    parent_head: ParentHead | None = None
    last_import: str | None = None
    fork_pin_ref: str | None = None


def resolve_sync_target(
    repo_root: Path | None, inputs: SyncInputs, host: TargetSnapshot
) -> SyncTarget:
    """Choose a container's sync target: its parent, else its PR head, else its host branch.

    ``repo_root`` is None when the container's repository cannot be loaded;
    then no host ref is read. A parent that cannot be compared with gives the
    host branch marked as its fallback, never the PR head: the parent stays
    where the child's work flows. Reads refs only, never fetches.
    """
    if inputs.parent is not None:
        head = inputs.parent_head
        if head is None:
            return replace(host_sync_target(host, fallback=True), parent=inputs.parent)
        ahead = inputs.last_import
        if ahead is None and inputs.fork_pin_ref is not None and repo_root is not None:
            ahead = git.rev_parse(repo_root, inputs.fork_pin_ref)
        return SyncTarget(
            "parent",
            inputs.parent,
            head.sha,
            "",
            parent=inputs.parent,
            ahead_sha=ahead,
            seen_at=head.seen_at,
        )
    if inputs.pr_number is None:
        return host_sync_target(host)
    from jailbee.pr import pr_head_ref

    ref = pr_head_ref(inputs.pr_number)
    sha = git.rev_parse(repo_root, ref) if repo_root is not None else None
    if sha is None:
        return host_sync_target(host, fallback=True)
    return SyncTarget("pr", f"PR #{inputs.pr_number}", sha, ref)
