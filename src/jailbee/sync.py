"""High-level orchestration for `jailbee git fetch / checkout / merge`.

Pulls commits from a container's clone back into the host repo via git's
ext::-transport over `incus exec`. Writes fetched commits to
`refs/jailbee/<short>/<branch>` so the host's `.git/config` stays clean (no
persistent remote) and `git branch -r` is uncluttered.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jailbee import git, submodules
from jailbee.incus import IncusError
from jailbee.retry import confirm_retry_quiet, with_remote_retry

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


SourcePref = Literal["origin", "local"]
"""Which copy of the push source branch to prefer on the host.

``"origin"`` means ``refs/remotes/origin/<source>``, ``"local"`` means
``refs/heads/<source>``. Configured repo-wide via ``push.push_from`` and
overridable per invocation (``jailbee git push --from-origin/--from-local``).
"""


class SyncError(RuntimeError):
    """Raised when fetch/checkout/merge cannot proceed (user-visible)."""


@dataclass(frozen=True)
class FetchResult:
    """Outcome of `fetch_from_container`.

    `base_oid` is the OID used as the comparison base for counting "new"
    commits. When `old_oid` is set, `base_oid == old_oid`. When this is
    the first fetch (`old_oid is None`), `base_oid` is the host's HEAD
    captured *before* any subsequent merge moves it — so callers can
    still report `base_oid..new_oid` correctly after merging.
    """

    branch: str
    old_oid: str | None
    new_oid: str
    base_oid: str | None
    commits_added: int


@dataclass(frozen=True)
class CheckoutResult:
    """Outcome of `checkout_from_container`."""

    fetch: FetchResult
    branch: str
    head_oid: str
    created_new: bool


@dataclass(frozen=True)
class RetargetResult:
    """Outcome of `retarget_container`."""

    old_base: str | None
    new_base: str
    base_oid: str


@dataclass(frozen=True)
class MergeResult:
    """Outcome of `merge_from_container` — fetch + merge only.

    Cleanup (destroy container, delete merged branch) is handled by a
    separate `run_post_merge_cleanup` call so the CLI can print the
    fetch summary and commit log before prompting the user.

    `pre_merge_head` is the into-branch's tip captured before the merge
    (equal to HEAD for the in-place path, or the target branch ref for
    the FF/checkout paths; may be None if the target didn't exist yet).
    Compared against `head_oid` it tells the cleanup step whether the
    merge actually moved the branch — `fetch.commits_added` only reflects
    new commits on the jailbee ref, which misses the case where a prior
    fetch already pulled them but the current host branch was behind.
    """

    fetch: FetchResult
    branch: str
    head_oid: str
    into_branch: str | None
    pre_merge_head: str | None


@dataclass(frozen=True)
class SubmoduleMove:
    """A submodule pointer that moved as part of a host-side merge."""

    path: str
    old_sha: str | None
    new_sha: str | None
    status: str  # "new" | "removed" | "modified"
    commits: int
    ins: int
    dels: int


@dataclass(frozen=True)
class ConflictReport:
    """Structured submodule-merge conflict data for the pull report."""

    resolution: submodules.GitlinkResolution
    nongitlink: list[str]
    branch: str
    location: str


class MergeConflictError(SyncError):
    """A merge that left submodule/superproject conflicts unresolved.

    Carries a ``ConflictReport`` so the CLI can render the submodule
    report block after git's own output.
    """

    def __init__(self, message: str, *, report: ConflictReport) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class CleanupResult:
    """Outcome of `run_post_merge_cleanup`."""

    destroyed: bool
    deleted_branch: bool
    cleanup_error: str | None
    skipped_reason: str | None


@dataclass(frozen=True)
class PushResult:
    """Outcome of `push_to_container` — transport only.

    `source` is the user-facing branch name. `source_ref` is the concrete
    host ref pushed from — `refs/remotes/origin/<source>` or
    `refs/heads/<source>`, in the order set by the effective
    `SourcePref` (see `_resolve_host_source_ref`). `container_ref` is the
    destination inside the container (`refs/jailbee/host/<source>`).
    `old_oid` reflects the destination ref's value inside the container
    before the push, or None if it didn't exist.

    The remaining fields let the CLI report what the source resolution
    did: `fetched` is True when the host `git fetch origin <source>` ran
    and succeeded, `fetch_error` carries git's stderr when that
    best-effort fetch failed, and `local_only_commits` counts commits on
    `refs/heads/<source>` that the pushed remote-tracking ref does not
    contain (0 whenever the local ref is itself what got pushed).
    """

    source: str
    source_ref: str
    container_ref: str
    old_oid: str | None
    new_oid: str
    fetched: bool = False
    fetch_error: str | None = None
    local_only_commits: int = 0


@dataclass(frozen=True)
class PublishResult:
    """Outcome of publish_branch_from_container — branch now on origin.

    `fetch` is the underlying fetch-from-container outcome. `dirty` reports an
    uncommitted container tree. `publish_name` is the branch name actually
    pushed to origin (the external name; may differ from the container branch).
    `forced` is True when the push used --force-with-lease.
    """

    fetch: FetchResult
    dirty: bool
    publish_name: str
    forced: bool


@dataclass(frozen=True)
class MergeInContainerResult:
    """Outcome of `push_and_merge` — push followed by git merge in container."""

    push: PushResult
    container_branch: str
    fast_forward_only: bool
    head_oid: str


@dataclass(frozen=True)
class RebaseInContainerResult:
    """Outcome of `push_and_rebase` — push followed by git rebase in container."""

    push: PushResult
    container_branch: str
    head_oid: str


@dataclass(frozen=True)
class ResetInContainerResult:
    """Outcome of `push_and_reset` — push followed by git reset --hard in container."""

    push: PushResult
    container_branch: str
    head_oid: str
    discarded_commits: int
    old_branch_oid: str | None


@dataclass(frozen=True)
class RefSummary:
    """One side of a bridge operation: what it is called and where it points.

    ``label`` is the user-facing name (``origin/main``, ``feat/foo``), not the
    full ref. ``oid`` and ``subject`` are None when the ref does not resolve —
    a host branch that does not exist yet, or a container read that failed.
    """

    label: str
    oid: str | None
    subject: str | None


@dataclass(frozen=True)
class BridgePlan:
    """What a bridge command is about to do, before it does it.

    Built by ``plan_push`` / ``plan_pull`` / ``plan_checkout`` and rendered by
    ``tui.render_bridge_plan``. Purely descriptive: constructing one mutates
    nothing, and every field degrades (``None``, or a note) rather than raising,
    because a preview must never be the reason a command fails.

    ``source`` is the host side for a push and the container side for a
    pull/checkout; ``target`` is the other one. ``incoming`` is the number of
    commits the operation would bring across, or None when it is not
    computable. ``notes`` carries anything the user should read before saying
    yes — a dirty container tree, a failed host fetch, host commits that will
    not travel.
    """

    direction: Literal["push", "pull", "checkout"]
    container_short: str
    container_full: str
    container_state: str
    source: RefSummary
    target: RefSummary
    action: str
    incoming: int | None
    notes: tuple[str, ...]


def _build_ext_url(cfg: Config, incus: Incus, container: str) -> str:
    """Build the ext::-transport URL for fetching from `container`.

    Uses `incus exec --user <uid>` so `git upload-pack` runs as the
    container's dev user (avoiding git's `safe.directory` warning when
    the clone is owned by the dev user).
    """
    from jailbee.lifecycle import container_repo_dir

    repo_dir = container_repo_dir(cfg, incus, container)
    return (
        f"ext::incus exec --user {cfg.container_user.uid} {container} -- git upload-pack {repo_dir}"
    )


def _build_receive_url(cfg: Config, incus: Incus, container: str) -> str:
    """Build the ext::-transport URL for pushing into `container`.

    Mirror of `_build_ext_url`: uses `git receive-pack` instead of
    `git upload-pack` so the host can push refs into the container's
    clone via `git push ext::...`.
    """
    from jailbee.lifecycle import container_repo_dir

    repo_dir = container_repo_dir(cfg, incus, container)
    return (
        f"ext::incus exec --user {cfg.container_user.uid} {container} "
        f"-- git receive-pack {repo_dir}"
    )


def _container_ref_oid(
    incus: Incus,
    full_name: str,
    repo_dir: str,
    ref: str,
    *,
    uid: int,
) -> str | None:
    """Return the OID of `ref` inside the container, or None if it doesn't resolve."""
    try:
        out = incus.exec(
            full_name,
            ["git", "-C", repo_dir, "rev-parse", "--verify", "--quiet", ref],
            uid=uid,
        )
    except IncusError:
        return None
    oid = out.strip()
    return oid or None


def _container_status_dirty(incus: Incus, full_name: str, repo_dir: str, *, uid: int) -> bool:
    """Return True if `git status --porcelain` in the container has output."""
    try:
        out = incus.exec(
            full_name,
            ["git", "-C", repo_dir, "status", "--porcelain"],
            uid=uid,
        )
    except IncusError as exc:
        raise SyncError(f"Failed to inspect container working tree at {repo_dir}: {exc}") from exc
    return bool(out.strip())


def _container_has_merge_in_progress(incus: Incus, full_name: str, repo_dir: str) -> bool:
    try:
        incus.exec(full_name, ["test", "-f", f"{repo_dir}/.git/MERGE_HEAD"])
    except IncusError:
        return False
    return True


def _container_has_rebase_in_progress(incus: Incus, full_name: str, repo_dir: str) -> bool:
    for subdir in (".git/rebase-merge", ".git/rebase-apply"):
        try:
            incus.exec(full_name, ["test", "-d", f"{repo_dir}/{subdir}"])
        except IncusError:
            continue
        return True
    return False


def _container_current_branch(
    incus: Incus, full_name: str, repo_dir: str, *, uid: int
) -> str | None:
    """Return the container's current branch name, or None on detached HEAD / error."""
    try:
        out = incus.exec(
            full_name,
            ["git", "-C", repo_dir, "symbolic-ref", "--short", "HEAD"],
            uid=uid,
        )
    except IncusError:
        return None
    branch = out.strip()
    return branch or None


def _container_branch_names(
    incus: Incus, full_name: str, repo_dir: str, *, uid: int
) -> tuple[str, ...] | None:
    """Return the container clone's local branch names, or None if unknown.

    None means "could not be determined" — the exec failed, or the output was
    empty (a clone with zero branches tells us nothing we can act on). Callers
    use it to *validate* a branch name, so an unreadable list must never be
    mistaken for "the branch does not exist".
    """
    try:
        out = incus.exec(
            full_name,
            [
                "git",
                "-C",
                repo_dir,
                "for-each-ref",
                "--format=%(refname:short)",
                "refs/heads",
            ],
            uid=uid,
        )
    except IncusError:
        return None
    names = tuple(line.strip() for line in out.splitlines() if line.strip())
    return names or None


def _container_head_oid(incus: Incus, full_name: str, repo_dir: str, *, uid: int) -> str:
    out = incus.exec(full_name, ["git", "-C", repo_dir, "rev-parse", "HEAD"], uid=uid)
    oid = out.strip()
    if not oid:
        raise SyncError("Could not resolve container HEAD after operation.")
    return oid


def _container_commit_subject(
    incus: Incus, full_name: str, repo_dir: str, ref: str, *, uid: int
) -> str | None:
    """Subject line of `ref` inside the container, or None on any failure."""
    try:
        out = incus.exec(
            full_name,
            ["git", "-C", repo_dir, "log", "-1", "--format=%s", ref],
            uid=uid,
        )
    except IncusError:
        return None
    subject = out.strip()
    return subject or None


def _container_commit_count(
    incus: Incus, full_name: str, repo_dir: str, range_spec: str, *, uid: int
) -> int | None:
    """`git rev-list --count <range_spec>` inside the container, or None."""
    try:
        out = incus.exec(
            full_name,
            ["git", "-C", repo_dir, "rev-list", "--count", range_spec],
            uid=uid,
        )
    except IncusError:
        return None
    raw = out.strip()
    return int(raw) if raw.isdigit() else None


def _container_dirty_quiet(incus: Incus, full_name: str, repo_dir: str, *, uid: int) -> bool | None:
    """Like `_container_status_dirty`, but None instead of raising.

    Plan builders describe; they do not fail. An unreadable working tree just
    means the plan says nothing about it.
    """
    try:
        return _container_status_dirty(incus, full_name, repo_dir, uid=uid)
    except SyncError:
        return None


def _short_ref_label(ref: str) -> str:
    """'refs/remotes/origin/main' -> 'origin/main'; 'refs/heads/main' -> 'main'."""
    for prefix in ("refs/remotes/", "refs/heads/"):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def _host_ref_summary(cfg: Config, ref: str, *, label: str) -> RefSummary:
    """Summarise a host ref. Unresolvable refs yield oid=None, subject=None."""
    return RefSummary(
        label=label,
        oid=git.rev_parse(cfg.repo_root, ref),
        subject=git.commit_subject(cfg.repo_root, ref),
    )


def _run_container_preflights(
    incus: Incus,
    full_name: str,
    repo_dir: str,
    *,
    uid: int,
) -> str:
    """Run all preflights needed for --merge / --rebase. Returns current branch.

    Raises SyncError when the container is not in a clean state for a
    merge or rebase to be applied.
    """
    if _container_status_dirty(incus, full_name, repo_dir, uid=uid):
        raise SyncError("Container working tree is dirty. Commit or stash before --merge/--rebase.")
    if _container_has_merge_in_progress(incus, full_name, repo_dir):
        raise SyncError(
            "Container has merge in progress. Resolve it first inside 'jailbee shell <short>'."
        )
    if _container_has_rebase_in_progress(incus, full_name, repo_dir):
        raise SyncError(
            "Container has rebase in progress. Resolve it first inside 'jailbee shell <short>'."
        )
    branch = _container_current_branch(incus, full_name, repo_dir, uid=uid)
    if branch is None:
        raise SyncError("Container has detached HEAD. Check out a branch first.")
    return branch


def _resolve_host_source_ref(cfg: Config, source: str, *, prefer: SourcePref) -> str | None:
    """Return the host ref to push from for `source`, or None if neither exists.

    Both candidates are always accepted; `prefer` only sets the order:

    * ``"origin"`` — `refs/remotes/origin/<source>` first. A host
      `refs/heads/<base>` advances only on `git pull`, so for a branch the
      user does not check out it is stale exactly when they just fetched.
      Falls back to the local ref for branches with no upstream copy.
    * ``"local"`` — `refs/heads/<source>` first, then the remote-tracking
      ref, so a branch that was fetched but never checked out still works.
    """
    local = f"refs/heads/{source}" if git.local_branch_exists(cfg.repo_root, source) else None
    origin = (
        f"refs/remotes/{cfg.upstream_remote}/{source}"
        if git.remote_ref_exists(cfg.repo_root, cfg.upstream_remote, source)
        else None
    )
    if prefer == "origin":
        return origin or local
    return local or origin


def host_source_ref(cfg: Config, source: str, *, prefer: SourcePref) -> str | None:
    """Return the host ref to push from for `source`, or None if neither exists.

    Public wrapper over `_resolve_host_source_ref` for callers outside this
    module that need to check whether a source ref is available on the host.
    See `_resolve_host_source_ref` for the resolution logic and `prefer`
    semantics.
    """
    return _resolve_host_source_ref(cfg, source, prefer=prefer)


def _count_local_only_commits(cfg: Config, source: str, pushed_ref: str) -> int:
    """Commits on `refs/heads/<source>` that `pushed_ref` does not contain.

    Non-zero means the host has local work the push left behind — the one
    way origin-preference can surprise the user, so the CLI surfaces it.
    Returns 0 when the local branch is absent or the count is unavailable.
    """
    if not git.local_branch_exists(cfg.repo_root, source):
        return 0
    ok, out = git.run_capture(
        str(cfg.repo_root),
        ["rev-list", "--count", f"{pushed_ref}..refs/heads/{source}"],
    )
    if not ok:
        return 0
    raw = out.strip()
    return int(raw) if raw.isdigit() else 0


def prefetch_push_source(
    cfg: Config,
    *,
    source: str,
    prefer: SourcePref,
    fetch: bool | None,
) -> tuple[bool, str | None]:
    """Run push's best-effort host fetch ahead of the push itself.

    Same policy as the block inside `push_to_container` (origin mode plus
    `push.autofetch`), hoisted so a confirmation prompt can show the tip the
    push will actually send rather than the stale one the fetch exists to
    replace. Callers MUST pass `fetch=False` to the follow-up push so the fetch
    runs exactly once.

    Returns `(fetched, fetch_error)`. Never raises: a failed fetch is reported,
    not fatal — the source may be a ref no remote has.
    """
    do_fetch = cfg.push.autofetch if fetch is None else fetch
    if prefer != "origin" or not do_fetch:
        return (False, None)
    try:
        git.fetch_remote_ref(cfg.repo_root, cfg.upstream_remote, source)
    except git.GitFetchError as exc:
        return (False, exc.stderr.strip() or str(exc))
    return (True, None)


def plan_push(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    source: str,
    action: str,
    prefer_ref: SourcePref | None = None,
    fetch_note: tuple[bool, str | None] = (False, None),
    source_ref: str | None = None,
) -> BridgePlan:
    """Describe a pending `jailbee git push` without mutating anything.

    `source` and `action` are the already-resolved values the CLI will pass to
    the push. `source_ref`, when given, is the exact host ref that push will
    send (a PR head) and short-circuits branch-name resolution here too, so
    the plan describes the same ref the push does. `fetch_note` is the
    `(fetched, fetch_error)` pair from `prefetch_push_source`; it is reported
    here because a push invoked with `fetch=False` can no longer report it
    itself.

    Every *read* this function does degrades to `None` or a note rather than
    raising. Resolving the container itself does not: `resolve_container_name`
    raises `ValueError` for an unknown `short`, and `_container_is_running`'s
    `incus.list_containers()` call can raise `IncusError`. A plan is useless
    without a resolved container, so that failure is not swallowed here —
    callers must treat building the plan itself as best-effort and skip the
    confirmation prompt if it raises.
    """
    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    full_name = resolve_container_name(cfg, incus, short)
    repo_dir = container_repo_dir(cfg, incus, full_name)
    uid = cfg.container_user.uid
    prefer: SourcePref = prefer_ref if prefer_ref is not None else cfg.push.push_from

    host_ref = (
        source_ref
        if source_ref is not None
        else _resolve_host_source_ref(cfg, source, prefer=prefer)
    )
    source_summary = (
        _host_ref_summary(cfg, host_ref, label=_short_ref_label(host_ref))
        if host_ref is not None
        else RefSummary(label=source, oid=None, subject=None)
    )

    branch = _container_current_branch(incus, full_name, repo_dir, uid=uid)
    target_summary = RefSummary(
        label=branch or "(detached HEAD)",
        oid=_container_ref_oid(incus, full_name, repo_dir, "HEAD", uid=uid),
        subject=_container_commit_subject(incus, full_name, repo_dir, "HEAD", uid=uid),
    )

    # The container's refs/jailbee/host/<source> was written BY a previous push from
    # this host, so its OID is an object the host still has — which makes the
    # count exact. Absent (first push) it stays None rather than guessing.
    incoming: int | None = None
    # "plain" only writes refs/jailbee/host/<source> — it applies nothing to the
    # container's branch (`target` here) — so a commit count would read as
    # "N commit(s) to apply" against a branch nothing is applied to.
    if host_ref is not None and action != "plain":
        anchor = _container_ref_oid(
            incus, full_name, repo_dir, f"refs/jailbee/host/{source}", uid=uid
        )
        if anchor is not None:
            ok, out = git.run_capture(
                str(cfg.repo_root), ["rev-list", "--count", f"{anchor}..{host_ref}"]
            )
            raw = out.strip()
            incoming = int(raw) if ok and raw.isdigit() else None

    notes: list[str] = []
    fetched, fetch_error = fetch_note
    # Mirrors the gate in cli._print_push_summary: a failed fetch only matters
    # when the origin-tracking ref is what travelled. When resolution fell
    # back to refs/heads/<source> (branch not on origin at all — the normal
    # stacked-PR case), the failure had no bearing on what the push will send,
    # and warning would be noise.
    if fetch_error and host_ref is not None and host_ref.startswith("refs/remotes/"):
        notes.append(f"host fetch of origin/{source} failed: {fetch_error}")
    elif fetched:
        notes.append(f"fetched origin/{source} first")
    if host_ref is None:
        notes.append(f"'{source}' does not exist on the host — the push will fail")
    elif host_ref.startswith("refs/remotes/"):
        local_only = _count_local_only_commits(cfg, source, host_ref)
        if local_only:
            notes.append(f"{local_only} local commit(s) on refs/heads/{source} will NOT travel")
    if branch is None:
        notes.append("container HEAD is detached")
    if action in ("merge", "rebase", "force"):
        dirty = _container_dirty_quiet(incus, full_name, repo_dir, uid=uid)
        if dirty:
            notes.append(f"container working tree is dirty — --{action} will refuse")

    return BridgePlan(
        direction="push",
        container_short=short,
        container_full=full_name,
        container_state="Running" if _container_is_running(incus, full_name) else "Stopped",
        source=source_summary,
        target=target_summary,
        action=action,
        incoming=incoming,
        notes=tuple(notes),
    )


_CONTAINER_BASE_REF_CANDIDATES = (
    "refs/jailbee/base/{base}",
    "refs/remotes/origin/{base}",
    "refs/heads/{base}",
)


def _container_base_anchor(
    incus: Incus, full_name: str, repo_dir: str, base: str | None, *, uid: int
) -> str | None:
    """First base ref that resolves inside the container, or None.

    Same cascade as the `jailbee ls` status probe (`git_status.py:201-223`): the
    pinned `refs/jailbee/base/<base>` anchor, then the origin-tracking copy, then a
    local branch. Returning None when none resolve makes the commit count
    unavailable rather than plausible-but-wrong.
    """
    if not base:
        return None
    for template in _CONTAINER_BASE_REF_CANDIDATES:
        ref = template.format(base=base)
        if _container_ref_oid(incus, full_name, repo_dir, ref, uid=uid) is not None:
            return ref
    return None


def _plan_container_side(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    branch: str | None,
) -> tuple[str, str, str | None, str | None, str | None, RefSummary, int | None]:
    """Shared container-side reads for pull and checkout plans.

    Returns ``(full_name, state, current_branch, resolved_branch, base, summary,
    ahead)``:

    * ``current_branch`` is the container's *actual* checked-out branch (via
      `_container_current_branch`) — None on detached HEAD, regardless of
      ``branch`` or the ``user.jailbee.branch`` label. Callers use this to decide
      whether to note a detached container HEAD.
    * ``resolved_branch`` is `_resolve_branch`'s result — ``branch`` (explicit)
      → the container's current branch → the ``user.jailbee.branch`` label — the
      exact precedence `fetch_from_container` uses to pick what gets fetched.
      ``summary`` and ``ahead`` describe *this* branch's tip inside the
      container (``refs/heads/<resolved_branch>``), not literal `HEAD`, so an
      explicit ``-b`` or a label-only fallback on a detached HEAD both report
      the ref that will actually be read — never the checked-out commit under
      a name that doesn't belong to it.
    * ``base`` is the ``user.jailbee.base_branch`` label (None when unset).
    * ``ahead`` counts commits since the base anchor — the same number
      `jailbee ls` shows in its AHEAD column.

    Every *read* this function does degrades to `None` rather than raising.
    Resolving the container itself does not: `resolve_container_name` raises
    `ValueError` for an unknown `short`, and `_container_is_running`'s
    `incus.list_containers()` call can raise `IncusError`. A plan is useless
    without a resolved container, so that failure is not swallowed here —
    callers (`plan_pull`, `plan_checkout`) inherit this contract and must
    treat building the plan itself as best-effort, skipping the confirmation
    prompt if it raises. See `plan_push` for the same contract on the push side.
    """
    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    full_name = resolve_container_name(cfg, incus, short)
    repo_dir = container_repo_dir(cfg, incus, full_name)
    uid = cfg.container_user.uid

    current_branch = _container_current_branch(incus, full_name, repo_dir, uid=uid)
    resolved_branch = _resolve_branch(incus, full_name, repo_dir, branch, uid=uid)

    source_ref = f"refs/heads/{resolved_branch}" if resolved_branch is not None else "HEAD"
    summary = RefSummary(
        label=resolved_branch or "(detached HEAD)",
        oid=_container_ref_oid(incus, full_name, repo_dir, source_ref, uid=uid),
        subject=_container_commit_subject(incus, full_name, repo_dir, source_ref, uid=uid),
    )

    base_label = incus.config_get(full_name, "user.jailbee.base_branch")
    base = base_label if isinstance(base_label, str) and base_label else None
    anchor = _container_base_anchor(incus, full_name, repo_dir, base, uid=uid)
    ahead = (
        _container_commit_count(incus, full_name, repo_dir, f"{anchor}..{source_ref}", uid=uid)
        if anchor is not None
        else None
    )

    state = "Running" if _container_is_running(incus, full_name) else "Stopped"
    return (full_name, state, current_branch, resolved_branch, base, summary, ahead)


def plan_pull(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    branch: str | None,
    into: str | None,
    ff_only: bool,
) -> BridgePlan:
    """Describe a pending `jailbee git pull` without mutating anything.

    The target resolution mirrors `merge_from_container`: `into` wins, then the
    container's `user.jailbee.base_branch` label, then the host's current branch as
    the legacy fallback. See `_plan_container_side`'s docstring for the
    best-effort-vs-raises contract this shares with `plan_push`.
    """
    full_name, state, current_branch, _resolved_branch, base, source_summary, ahead = (
        _plan_container_side(cfg, incus, short, branch=branch)
    )

    current = git.get_current_branch(cfg.repo_root)
    target = into if into is not None else base
    if target is None:
        target = current

    target_summary = (
        _host_ref_summary(cfg, f"refs/heads/{target}", label=target)
        if target is not None
        else RefSummary(label="(detached HEAD)", oid=None, subject=None)
    )

    notes: list[str] = []
    if target is None:
        notes.append("host is in detached HEAD and the container has no base label")
    elif target != current:
        # merge_from_container fast-forwards refs/heads/<target> IN PLACE when
        # it isn't the checked-out branch — it does not need (or perform) a
        # checkout. --checkout only comes into play if that fast-forward is
        # rejected, i.e. the histories have diverged.
        notes.append(
            f"target '{target}' is not the checked-out branch ('{current}') — it "
            f"will be fast-forwarded in place; --checkout is needed only if it "
            f"has diverged."
        )
    if current_branch is None:
        notes.append("container HEAD is detached")

    return BridgePlan(
        direction="pull",
        container_short=short,
        container_full=full_name,
        container_state=state,
        source=source_summary,
        target=target_summary,
        action="ff-only" if ff_only else "merge",
        incoming=ahead,
        notes=tuple(notes),
    )


def plan_checkout(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    branch: str | None,
    as_name: str | None = None,
) -> BridgePlan:
    """Describe a pending `jailbee git checkout` without mutating anything.

    The host target mirrors `checkout_from_container` exactly: an explicit
    `as_name` wins, then the container's `user.jailbee.pr_branch` label (a
    PR-review or `jailbee pr --as` container updates its own PR head, whatever
    branch is checked out inside it), otherwise the resolved container branch
    is used for both sides. See `_plan_container_side`'s docstring for the
    best-effort-vs-raises contract this shares with `plan_push` and `plan_pull`.
    """
    full_name, state, current_branch, resolved_branch, _base, source_summary, ahead = (
        _plan_container_side(cfg, incus, short, branch=branch)
    )
    pr_branch = _container_pr_branch(incus, full_name)
    resolved = as_name or pr_branch or resolved_branch

    target_summary = (
        _host_ref_summary(cfg, f"refs/heads/{resolved}", label=resolved)
        if resolved is not None
        else RefSummary(label="(unknown)", oid=None, subject=None)
    )

    notes: list[str] = []
    if resolved is None:
        notes.append("container branch could not be determined")
    elif target_summary.oid is None:
        notes.append(f"'{resolved}' will be created on the host")
    if current_branch is None and branch is None:
        notes.append("container HEAD is detached")

    return BridgePlan(
        direction="checkout",
        container_short=short,
        container_full=full_name,
        container_state=state,
        source=source_summary,
        target=target_summary,
        action="ff-only",
        incoming=ahead,
        notes=tuple(notes),
    )


def _container_is_running(incus: Incus, full_name: str) -> bool:
    for raw in incus.list_containers():
        if raw["name"] == full_name:
            return raw.get("status") == "Running"
    return False


def _verify_clone_exists(incus: Incus, full_name: str, repo_dir: str) -> bool:
    """Run `test -d <repo_dir>` inside the container. False on missing."""
    try:
        incus.exec(full_name, ["test", "-d", repo_dir])
    except IncusError:
        return False
    return True


def _resolve_branch(
    incus: Incus,
    full_name: str,
    repo_dir: str,
    explicit: str | None,
    *,
    uid: int,
) -> str | None:
    """Return the branch to fetch.

    Order: explicit → container's HEAD symbolic-ref → user.jailbee.branch.

    The container's *current* HEAD branch is preferred over the persisted
    label because the user may have switched branches inside the container
    after `jailbee new`; the label only reflects the branch the container was
    created from. Falls back to the label when HEAD is detached (no symbolic
    ref) or the exec fails. ``uid`` is forwarded to ``incus exec --user``
    so the git command runs as the container's dev user (avoiding git's
    `dubious ownership` check on a dev-owned repo).
    """
    if explicit is not None:
        return explicit
    try:
        head = incus.exec(
            full_name,
            ["git", "-C", repo_dir, "symbolic-ref", "--short", "HEAD"],
            uid=uid,
        )
        branch = head.strip()
        if branch:
            return branch
    except IncusError:
        pass
    meta = incus.config_get(full_name, "user.jailbee.branch")
    if meta:
        return meta
    return None


def assert_container_publishable(cfg: Config, incus: Incus, short: str) -> str:
    """Preflight the mount/stopped/clone checks for publishing a container branch.

    Returns the resolved full container name. Raises `SyncError` (with the same
    messages `fetch_from_container` has always used) when the container is in
    mount mode, is not running, or has no clone. Callers that do expensive work
    before fetching (e.g. `jailbee pr`'s AI generation + name prompt) call this
    first so they fail fast instead of after the work.
    """
    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    full_name = resolve_container_name(cfg, incus, short)

    mode = incus.config_get(full_name, "user.jailbee.mode")
    if mode == "mount":
        raise SyncError(
            f"container '{short}' is in mount mode — host and container share "
            f"the working tree, so fetch/checkout/merge are not applicable. "
            f"Use git on the host directly."
        )

    if not _container_is_running(incus, full_name):
        raise SyncError(f"Container '{short}' is not running. Start it with: jailbee start {short}")

    repo_dir = container_repo_dir(cfg, incus, full_name)
    if not _verify_clone_exists(incus, full_name, repo_dir):
        raise SyncError(f"Container '{short}' has no clone at {repo_dir}. Cannot fetch.")

    return full_name


_BRANCH_LIST_LIMIT = 20


def _assert_container_has_branch(
    incus: Incus,
    full_name: str,
    repo_dir: str,
    resolved: str,
    *,
    short: str,
    explicit: bool,
    uid: int,
) -> None:
    """Raise `SyncError` when `resolved` is not a branch in the container clone.

    Without this, a wrong branch name reaches `git fetch` and comes back as a
    bare `GitError("git fetch failed (exit 128)")` under git's own "couldn't
    find remote ref" — which says nothing about *which* side is missing it or
    what the branch names really are. Skipped when the branch list can't be
    read (see `_container_branch_names`): unknown is not "missing".
    """
    names = _container_branch_names(incus, full_name, repo_dir, uid=uid)
    if names is None or resolved in names:
        return

    shown = ", ".join(names[:_BRANCH_LIST_LIMIT])
    if len(names) > _BRANCH_LIST_LIMIT:
        shown += f", … ({len(names)} total)"
    hint = (
        "--branch/-b selects which branch to read inside the container; "
        "it does not name the branch written on the host."
        if explicit
        else "Pick the branch to read with --branch <name>."
    )
    raise SyncError(
        f"Container '{short}' has no branch '{resolved}'.\n"
        f"Branches in the container: {shown}\n{hint}"
    )


def fetch_from_container(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    branch: str | None = None,
) -> FetchResult:
    """Fetch commits from container `short` into `refs/jailbee/<short>/<branch>`.

    Raises `SyncError` for user-visible problems (stopped container, no
    clone, unresolvable branch, a branch the container doesn't have). git
    failures bubble up as `GitError`.
    """
    from jailbee.lifecycle import container_repo_dir

    full_name = assert_container_publishable(cfg, incus, short)

    repo_dir = container_repo_dir(cfg, incus, full_name)
    resolved = _resolve_branch(incus, full_name, repo_dir, branch, uid=cfg.container_user.uid)
    if resolved is None:
        raise SyncError(
            f"Cannot determine branch for container '{short}'. Use --branch <name> to specify."
        )
    _assert_container_has_branch(
        incus,
        full_name,
        repo_dir,
        resolved,
        short=short,
        explicit=branch is not None,
        uid=cfg.container_user.uid,
    )

    url = _build_ext_url(cfg, incus, full_name)
    ref = f"refs/jailbee/{short}/{resolved}"
    refspec = f"+refs/heads/{resolved}:{ref}"

    old_oid = git.rev_parse(cfg.repo_root, ref)
    git.fetch_url(cfg.repo_root, url, refspec)
    new_oid = git.rev_parse(cfg.repo_root, ref)
    if new_oid is None:
        raise SyncError(f"fetch succeeded but {ref} did not resolve")

    # Pick a base for counting "new" commits. If we had a prior fetch,
    # the previous ref OID is the obvious base. Otherwise fall back to
    # the host's HEAD so the count reflects "commits not yet on HEAD",
    # not the entire history reachable from new_oid.
    if old_oid is not None:
        base_oid: str | None = old_oid
    else:
        base_oid = git.rev_parse(cfg.repo_root, "HEAD")

    if base_oid == new_oid:
        commits_added = 0
    elif base_oid is None:
        commits_added = len(git.log_oneline(cfg.repo_root, new_oid))
    else:
        commits_added = len(git.log_oneline(cfg.repo_root, f"{base_oid}..{new_oid}"))

    return FetchResult(
        branch=resolved,
        old_oid=old_oid,
        new_oid=new_oid,
        base_oid=base_oid,
        commits_added=commits_added,
    )


def _container_pr_branch(incus: Incus, full_name: str) -> str | None:
    """Return the container's external PR branch name (user.jailbee.pr_branch), or None."""
    value = incus.config_get(full_name, "user.jailbee.pr_branch")
    return value if isinstance(value, str) and value else None


def publish_branch_from_container(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    branch: str | None = None,
    publish_name: str | None = None,
    force: bool = False,
) -> PublishResult:
    """Fetch container `short`'s branch and push it to the GitHub origin.

    Pushes `refs/jailbee/<short>/<branch>` to `refs/heads/<publish_name>` on origin
    (defaulting `publish_name` to the container branch). With `force=True`, the
    push uses --force-with-lease anchored on the current origin sha; without it,
    git's fast-forward rule rejects a diverged remote branch (a SyncError).

    A failed push is offered as a retry on a TTY (see `retry.with_remote_retry`);
    only the push is re-run, never the container fetch or the lease anchor.
    """
    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    fetch = fetch_from_container(cfg, incus, short, branch=branch)

    full_name = resolve_container_name(cfg, incus, short)
    repo_dir = container_repo_dir(cfg, incus, full_name)
    dirty = _container_status_dirty(incus, full_name, repo_dir, uid=cfg.container_user.uid)

    dest = publish_name or fetch.branch
    remote = cfg.upstream_remote
    lease = git.remote_branch_sha(cfg.repo_root, remote, dest) if force else None

    src_ref = f"refs/jailbee/{short}/{fetch.branch}"
    try:
        # Retry only the push. Re-fetching from the container or recomputing the
        # --force-with-lease anchor would be wrong: the lease must stay pinned to
        # the remote state the user was shown before the first attempt.
        with_remote_retry(
            lambda: git.push_to_remote(
                cfg.repo_root, remote, src_ref, dest, force_with_lease=lease
            ),
            label=f"pushing '{dest}' to {remote}",
            catch=git.GitError,
            # git's output is inherited, so its error is already on the terminal
            # above the prompt and the SyncError below repeats it on a decline.
            confirm=confirm_retry_quiet,
        )
    except git.GitError as exc:
        hint = (
            (
                f"If you rebased or amended this branch, re-run with --force to "
                f"update the PR head (uses --force-with-lease).\n"
                f"If '{dest}' is an unrelated branch that already exists on "
                f"{remote}, pick a different name with --as instead of forcing."
            )
            if not force
            else "The remote moved since jailbee last checked; re-run to pick up "
            "the change, or reconcile manually — jailbee never blindly overwrites."
        )
        raise SyncError(f"Pushing '{dest}' to {remote} failed: {exc}\n{hint}") from exc

    return PublishResult(fetch=fetch, dirty=dirty, publish_name=dest, forced=bool(lease))


def checkout_from_container(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    branch: str | None = None,
    as_name: str | None = None,
) -> CheckoutResult:
    """Fetch + check out the container's branch on the host.

    `branch` selects what is read *from* the container; `as_name` names the
    branch written *on the host* (default: the container's `user.jailbee.pr_branch`
    label when set, else the container branch's own name). The two are
    independent — `--as` never changes which ref gets fetched.

    - If the branch doesn't exist on the host, create it from
      `refs/jailbee/<short>/<branch>` and set tracking to `origin/<branch>`
      when that remote-tracking ref exists.
    - If the branch exists and is the current HEAD, fast-forward it.
    - If the branch exists but isn't current, check it out then
      fast-forward.
    - On non-ff (divergence), raise `SyncError` pointing at `jailbee git pull`.

    Returns a `CheckoutResult` so the CLI can print a post-op summary.
    """
    fetch_result = fetch_from_container(cfg, incus, short, branch=branch)

    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    full_name = resolve_container_name(cfg, incus, short)
    repo_dir = container_repo_dir(cfg, incus, full_name)
    submodules.transport_submodules_to_host(cfg, incus, full_name, short, repo_dir=repo_dir)

    container_branch = fetch_result.branch
    fetched_ref = f"refs/jailbee/{short}/{container_branch}"
    target = as_name or _container_pr_branch(incus, full_name) or container_branch

    if not git.local_branch_exists(cfg.repo_root, target):
        track = (
            f"{cfg.upstream_remote}/{target}"
            if git.remote_ref_exists(cfg.repo_root, cfg.upstream_remote, target)
            else None
        )
        git.create_branch(cfg.repo_root, target, start_point=fetched_ref, track=track)
        head_oid = git.rev_parse(cfg.repo_root, "HEAD")
        if head_oid is None:
            raise SyncError(f"checkout succeeded but HEAD did not resolve on branch '{target}'")
        submodules.update_submodules_on_host(cfg.repo_root, branch=target)
        return CheckoutResult(
            fetch=fetch_result, branch=target, head_oid=head_oid, created_new=True
        )

    current = git.get_current_branch(cfg.repo_root)
    if current != target:
        git.checkout_branch(cfg.repo_root, target)

    try:
        git.merge_ref(
            cfg.repo_root,
            fetched_ref,
            message=None,
            no_ff=False,
            ff_only=True,
        )
    except git.GitError as exc:
        raise SyncError(
            f"Branch '{target}' on host has diverged from container. "
            f"Use 'jailbee git pull {short}' to merge, or rebase manually."
        ) from exc

    head_oid = git.rev_parse(cfg.repo_root, "HEAD")
    if head_oid is None:
        raise SyncError(f"checkout succeeded but HEAD did not resolve on branch '{target}'")
    submodules.update_submodules_on_host(cfg.repo_root, branch=target)
    return CheckoutResult(fetch=fetch_result, branch=target, head_oid=head_oid, created_new=False)


def checkout_submodules_on_host(
    cfg: Config, *, branch: str | None = None
) -> tuple[str, list[tuple[str, str | None]]]:
    """Place the host repo's submodules on ``branch`` (or the host's current
    branch), recursively, then return ``(resolved branch, per-submodule report)``.

    Purely local — moves no objects between host and container. Raises
    ``SyncError`` when the host is in detached HEAD and no ``branch`` override
    is given.
    """
    resolved = branch if branch is not None else git.get_current_branch(cfg.repo_root)
    if resolved is None:
        raise SyncError(
            "Host is in detached HEAD; pass -b <branch> to name the branch to place submodules on."
        )
    submodules.update_submodules_on_host(cfg.repo_root, branch=resolved)
    report = submodules.report_submodule_branches(git.run_capture, str(cfg.repo_root))
    return resolved, report


def checkout_submodules_in_container(
    cfg: Config, incus: Incus, short: str, *, branch: str | None = None
) -> tuple[str, list[tuple[str, str | None]]]:
    """Place a container's submodules on its branch (or ``branch`` override),
    recursively, and return ``(resolved branch, report)``.

    Purely local (no host<->container transport). Refuses mount mode and a
    stopped container.
    """
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    full_name = resolve_container_name(cfg, incus, short)

    mode = incus.config_get(full_name, "user.jailbee.mode")
    if mode == "mount":
        raise SyncError(
            f"container '{short}' is in mount mode — host and container share "
            f"the working tree, so submodule checkout is not applicable."
        )
    if not _container_is_running(incus, full_name):
        raise SyncError(f"Container '{short}' is not running. Start it with: jailbee start {short}")

    resolved = branch if branch is not None else incus.config_get(full_name, "user.jailbee.branch")
    if not resolved:
        raise SyncError(
            f"container '{short}' has no recorded branch (user.jailbee.branch); pass -b <branch>."
        )

    repo_dir = container_repo_dir(cfg, incus, full_name)
    uid = cfg.container_user.uid
    git_env = {
        "HOME": f"/home/{CONTAINER_USERNAME}",
        "USER": CONTAINER_USERNAME,
        "LOGNAME": CONTAINER_USERNAME,
    }
    submodules.update_submodules_in_container(
        incus, full_name, repo_dir=repo_dir, uid=uid, env=git_env, branch=resolved
    )
    run = submodules._container_runner(incus, full_name, uid=uid, env=git_env)
    report = submodules.report_submodule_branches(run, repo_dir)
    return resolved, report


def _stdin_is_interactive() -> bool:
    """Return True if stdin is a TTY (and JAILBEE_NONINTERACTIVE is unset)."""
    return sys.stdin.isatty() and not os.environ.get("JAILBEE_NONINTERACTIVE")


def _maybe_refresh_base(
    cfg: Config,
    incus: Incus,
    full_name: str,
    base_branch: str | None,
    into_branch: str | None,
) -> None:
    """Refresh the container's base ref iff the merge landed in its base branch."""
    if base_branch is not None and into_branch == base_branch:
        refresh_container_base(cfg, incus, full_name, base_branch=base_branch)


def merge_from_container(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    branch: str | None = None,
    ff_only: bool = False,
    into: str | None = None,
    allow_checkout: bool = False,
) -> MergeResult:
    """Fetch + merge the container's branch into its base branch.

    The merge target defaults to the container's base branch
    (``user.jailbee.base_branch``), overridable with ``into``. When the target
    is the host's current HEAD the merge runs in place (a ``--no-ff`` merge
    commit, or ``--ff-only`` when ``ff_only``). When the target is a
    different branch, the target ref is fast-forwarded without a checkout;
    on a non-fast-forward this raises ``SyncError`` unless ``allow_checkout``
    is set, in which case the target is checked out and merged, leaving host
    HEAD on the target.

    Cleanup is handled separately by ``run_post_merge_cleanup``.
    """
    from jailbee.lifecycle import resolve_container_name

    fetch_result = fetch_from_container(cfg, incus, short, branch=branch)
    container_branch = fetch_result.branch
    fetched_ref = f"refs/jailbee/{short}/{container_branch}"

    from jailbee.lifecycle import container_repo_dir

    full_name = resolve_container_name(cfg, incus, short)
    repo_dir = container_repo_dir(cfg, incus, full_name)
    submodules.transport_submodules_to_host(cfg, incus, full_name, short, repo_dir=repo_dir)

    base_label = incus.config_get(full_name, "user.jailbee.base_branch")
    base_branch = base_label if isinstance(base_label, str) and base_label else None
    target = into if into is not None else base_branch
    current = git.get_current_branch(cfg.repo_root)
    if target is None:
        target = current  # legacy fallback: merge into HEAD

    if target == current:
        # In-place path: HEAD IS the target branch.
        pre_merge_head = git.rev_parse(cfg.repo_root, "HEAD")
        if ff_only:
            git.merge_ref(cfg.repo_root, fetched_ref, message=None, no_ff=False, ff_only=True)
        else:
            try:
                git.merge_ref(
                    cfg.repo_root,
                    fetched_ref,
                    message=f"Merge branch '{container_branch}' from container {short}",
                    no_ff=True,
                    ff_only=False,
                )
            except git.GitError:
                _resolve_gitlinks_and_commit_host(
                    cfg, branch=container_branch, location=f"cd {cfg.repo_root}"
                )
        head_oid = git.rev_parse(cfg.repo_root, "HEAD")
        if head_oid is None:
            raise SyncError("merge succeeded but HEAD did not resolve")
        submodules.update_submodules_on_host(cfg.repo_root, branch=target)
        result = MergeResult(
            fetch=fetch_result,
            branch=container_branch,
            head_oid=head_oid,
            into_branch=target,
            pre_merge_head=pre_merge_head,
        )
        _maybe_refresh_base(cfg, incus, full_name, base_branch, result.into_branch)
        return result

    # target != current here; both-None is caught by target == current above
    assert target is not None, "both target and current are None — should have merged in place"

    if allow_checkout and git.host_tree_dirty(cfg.repo_root):
        raise SyncError(
            f"Host working tree is dirty; --checkout needs a clean tree to "
            f"check out '{target}'. Commit or stash first."
        )

    # Capture the target branch's OLD tip before any operation moves it.
    pre_merge_head = git.rev_parse(cfg.repo_root, f"refs/heads/{target}")

    if git.fast_forward_branch(cfg.repo_root, target, fetched_ref):
        head_oid = git.rev_parse(cfg.repo_root, fetched_ref)
        if head_oid is None:
            raise SyncError("fast-forward succeeded but ref did not resolve")
        if allow_checkout:
            git.checkout_branch(cfg.repo_root, target)
            submodules.update_submodules_on_host(cfg.repo_root, branch=target)
        result = MergeResult(
            fetch=fetch_result,
            branch=container_branch,
            head_oid=head_oid,
            into_branch=target,
            pre_merge_head=pre_merge_head,
        )
        _maybe_refresh_base(cfg, incus, full_name, base_branch, result.into_branch)
        return result

    if not allow_checkout:
        raise SyncError(
            f"Base branch '{target}' has diverged from container '{short}' and "
            f"is not the checked-out branch, so it can't be fast-forwarded in "
            f"place. Re-run with --checkout to check it out and merge, or "
            f"check it out yourself and run 'jailbee git pull {short}'."
        )

    result = _merge_via_checkout(
        cfg, fetch_result, short, container_branch, fetched_ref, target, pre_merge_head
    )
    _maybe_refresh_base(cfg, incus, full_name, base_branch, result.into_branch)
    return result


def _merge_via_checkout(
    cfg: Config,
    fetch_result: FetchResult,
    short: str,
    container_branch: str,
    fetched_ref: str,
    target: str,
    pre_merge_head: str | None,
) -> MergeResult:
    """Check out ``target``, merge ``fetched_ref``, and stay on ``target``.

    The caller has already verified the working tree is clean. On a merge
    conflict the host is left on ``target`` in merge state so the user can
    resolve it; a ``SyncError`` carries the hint.
    """
    git.checkout_branch(cfg.repo_root, target)
    try:
        git.merge_ref(
            cfg.repo_root,
            fetched_ref,
            message=f"Merge branch '{container_branch}' from container {short}",
            no_ff=True,
            ff_only=False,
        )
    except git.GitError:
        # Auto-resolve submodule gitlink pointers; on remaining conflicts the host
        # is left on `target` in merge state (SyncError tells the user how to finish).
        _resolve_gitlinks_and_commit_host(
            cfg,
            branch=container_branch,
            location=f"cd {cfg.repo_root}  # on '{target}' in merge state; resolve, commit",
        )
    head_oid = git.rev_parse(cfg.repo_root, "HEAD")
    if head_oid is None:
        raise SyncError("merge succeeded but HEAD did not resolve")
    submodules.update_submodules_on_host(cfg.repo_root, branch=target)
    return MergeResult(
        fetch=fetch_result,
        branch=container_branch,
        head_oid=head_oid,
        into_branch=target,
        pre_merge_head=pre_merge_head,
    )


def _refresh_submodule_base_anchors(
    cfg: Config, incus: Incus, full_name: str, *, base_branch: str
) -> None:
    """Re-pin submodule base anchors inside the container after the superproject
    base ref moved. Best-effort — a refresh problem must not fail the caller."""
    from jailbee.lifecycle import container_repo_dir

    try:
        branch = incus.config_get(full_name, "user.jailbee.branch")
        if not branch:
            return
        repo_dir = container_repo_dir(cfg, incus, full_name)
        run = submodules._container_runner(incus, full_name, uid=cfg.container_user.uid)
        submodules.seed_submodule_base_anchors(
            run, repo_dir, base_branch=base_branch, container_branch=branch
        )
    except (IncusError, git.GitError):
        pass


def refresh_container_base(cfg: Config, incus: Incus, full_name: str, *, base_branch: str) -> bool:
    """Sync the host's `base_branch` tip into the container's `refs/jailbee/base/<base_branch>`.

    This is the ref the `jailbee ls` probe prefers when computing AHEAD ±/↑, so
    advancing it after the host base moves makes a container that has been
    integrated into the host base read 0 ahead. Unlike `refs/remotes/origin/*`,
    the `refs/jailbee/*` namespace is untouched by an in-container `git fetch`, so
    the number stays correct.

    Best-effort: returns True if the ref was pushed, False if the host base
    does not resolve or the transport fails. Never raises — a refresh problem
    must not fail the surrounding pull/push/new.
    """
    if git.rev_parse(cfg.repo_root, f"refs/heads/{base_branch}") is None:
        return False
    try:
        url = _build_receive_url(cfg, incus, full_name)
        git.push_url(
            cfg.repo_root, url, f"+refs/heads/{base_branch}:refs/jailbee/base/{base_branch}"
        )
    except (git.GitError, IncusError):
        return False
    _refresh_submodule_base_anchors(cfg, incus, full_name, base_branch=base_branch)
    return True


def retarget_container(
    cfg: Config,
    incus: Incus,
    short: str,
    new_base: str,
) -> RetargetResult:
    """Re-point container ``short`` at a new base branch.

    Used in stacked-PR chains: when the parent PR (e.g. ``feat/a``) merges
    to ``main``, the dependent container's base flips to ``main``. Pushes
    ``refs/jailbee/base/<new_base>`` into the container from the host branch,
    deletes the stale ``refs/jailbee/base/<old>`` ref, then updates the
    ``user.jailbee.base_branch`` label (label last, so a transport failure
    leaves it pointing at the old, still-seeded base). Does NOT merge the
    new base into the container's branch — that is ``jailbee git push --merge``.
    """
    from jailbee import background
    from jailbee.lifecycle import (
        container_repo_dir,
        lookup_background_job,
        resolve_container_name,
    )

    full_name = resolve_container_name(cfg, incus, short)

    mode = incus.config_get(full_name, "user.jailbee.mode")
    if mode == "mount":
        raise SyncError(
            f"container '{short}' is in mount mode — host and container share "
            f"the working tree, so retarget is not applicable."
        )
    op = lookup_background_job(cfg, short)
    # A dead row (terminal phase, or its worker gone) no longer blocks retarget
    # — that was the branch's central scenario: autostart failed, the user
    # fixed it by hand, and kept working, so a stale `failed` row must not
    # refuse retarget forever. Only a genuinely live job blocks.
    if op is not None and not background.clearable(op.phase, op.pid):
        label = background.job_label(op.phase, op.pid, kind=op.op_kind)
        raise SyncError(
            f"Container '{short}' has a live background job ({label}); wait for it to finish."
        )
    if not _container_is_running(incus, full_name):
        raise SyncError(f"Container '{short}' is not running. Start it with: jailbee start {short}")

    base_oid = git.rev_parse(cfg.repo_root, f"refs/heads/{new_base}")
    if base_oid is None:
        raise SyncError(
            f"Branch '{new_base}' does not exist on host (refs/heads/{new_base}). "
            f"Create or fetch it first."
        )

    old_label = incus.config_get(full_name, "user.jailbee.base_branch")
    old_base = old_label if isinstance(old_label, str) and old_label else None
    if old_base == new_base:
        raise SyncError(f"Container '{short}' already targets base branch '{new_base}'.")

    url = _build_receive_url(cfg, incus, full_name)
    refspecs = [f"+refs/heads/{new_base}:refs/jailbee/base/{new_base}"]
    if old_base is not None:
        refspecs.append(f":refs/jailbee/base/{old_base}")  # empty source = delete
    git.push_url_multi(cfg.repo_root, url, refspecs)

    _refresh_submodule_base_anchors(cfg, incus, full_name, base_branch=new_base)
    if old_base is not None:
        try:
            run = submodules._container_runner(incus, full_name, uid=cfg.container_user.uid)
            repo_dir = container_repo_dir(cfg, incus, full_name)
            submodules.delete_submodule_base_anchors(run, repo_dir, old_base)
        except (IncusError, git.GitError):
            pass

    incus.config_set(full_name, "user.jailbee.base_branch", new_base)

    return RetargetResult(old_base=old_base, new_base=new_base, base_oid=base_oid)


def push_to_container(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    source: str | None = None,
    prefer_ref: SourcePref | None = None,
    fetch: bool | None = None,
    source_ref: str | None = None,
) -> PushResult:
    """Push host's `source` branch into container `short` as refs/jailbee/host/<source>.

    Transport only — does not run merge or rebase inside the container.
    Raises `SyncError` for user-visible problems (stopped container, mount
    mode, missing source branch). git failures bubble up as `GitError`.

    `prefer_ref` decides which host copy of `source` is authoritative and
    defaults to `cfg.push.push_from`; `fetch` refreshes the remote-tracking
    ref first and defaults to `cfg.push.autofetch`. Both only matter in
    `"origin"` mode — with `"local"` the fetch is skipped, since its result
    could not be used. The fetch is best-effort: callers that hold a ref no
    remote has (an unpushed local branch) must still be able to push, so a
    failure is reported through `PushResult.fetch_error` rather than raised.

    `source_ref` overrides that resolution with an exact host ref, and
    `source` degrades to a label for the container-side `refs/jailbee/host/<source>`
    destination. A PR head lives in jailbee's own `refs/jailbee/pr/<N>/head` (see
    `pr.pr_head_ref`) and deliberately in no branch at all, so nothing on the
    host is looked up or fetched — a same-named local branch, stale or ahead,
    must not decide what a `--pr` push sends.
    """
    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    full_name = resolve_container_name(cfg, incus, short)

    mode = incus.config_get(full_name, "user.jailbee.mode")
    if mode == "mount":
        raise SyncError(
            f"container '{short}' is in mount mode — host and container share "
            f"the working tree, so push is not applicable. Use git on the host directly."
        )

    if not _container_is_running(incus, full_name):
        raise SyncError(f"Container '{short}' is not running. Start it with: jailbee start {short}")

    resolved_source = (
        source
        if source is not None
        else git.detect_default_branch(cfg.repo_root, cfg.upstream_remote)
    )

    host_ref: str | None
    if source_ref is not None:
        host_ref = source_ref
        fetched, fetch_error = (False, None)
        local_only = 0
    else:
        prefer: SourcePref = prefer_ref if prefer_ref is not None else cfg.push.push_from
        fetched, fetch_error = prefetch_push_source(
            cfg, source=resolved_source, prefer=prefer, fetch=fetch
        )

        host_ref = _resolve_host_source_ref(cfg, resolved_source, prefer=prefer)
        if host_ref is None:
            raise SyncError(
                f"Source branch '{resolved_source}' does not exist on host "
                f"(neither refs/heads/{resolved_source} nor "
                f"refs/remotes/{cfg.upstream_remote}/{resolved_source})."
            )
        local_only = (
            _count_local_only_commits(cfg, resolved_source, host_ref)
            if host_ref.startswith("refs/remotes/")
            else 0
        )

    new_oid = git.rev_parse(cfg.repo_root, host_ref)
    if new_oid is None:
        raise SyncError(f"Source ref '{host_ref}' did not resolve on host.")

    container_ref = f"refs/jailbee/host/{resolved_source}"
    repo_dir = container_repo_dir(cfg, incus, full_name)
    old_oid = _container_ref_oid(
        incus, full_name, repo_dir, container_ref, uid=cfg.container_user.uid
    )

    url = _build_receive_url(cfg, incus, full_name)
    host_refspec = f"+{host_ref}:{container_ref}"

    base_label = incus.config_get(full_name, "user.jailbee.base_branch")
    base_branch = base_label if isinstance(base_label, str) and base_label else None
    if base_branch is not None and resolved_source == base_branch:
        # Pushing the container's base branch — also advance the jailbee-managed
        # base ref so `jailbee ls` reflects the fresh base.
        git.push_url_multi(
            cfg.repo_root,
            url,
            [host_refspec, f"+{host_ref}:refs/jailbee/base/{base_branch}"],
        )
    else:
        git.push_url(cfg.repo_root, url, host_refspec)

    return PushResult(
        source=resolved_source,
        source_ref=host_ref,
        container_ref=container_ref,
        old_oid=old_oid,
        new_oid=new_oid,
        fetched=fetched,
        fetch_error=fetch_error,
        local_only_commits=local_only,
    )


def _sub_merge_message(branch: str) -> str:
    """Commit message used for an auto-created submodule merge commit."""
    return f"Merge for superproject merge of '{branch}'"


def compute_submodule_moves(
    repo_root: Path, old: str | None, new: str | None
) -> list[SubmoduleMove]:
    """Gitlinks that moved between superproject commits ``old`` and ``new``.

    Commit counts and shortstats come from the host sub-repo
    (``repo_root/<path>``); objects are present after submodule transport.
    Best-effort: any sub-repo query failure degrades to zero counts.
    """
    from jailbee.git_status import _shortstat_ints

    if not old or not new or old == new:
        return []
    ok, raw = git.run_capture(str(repo_root), ["diff", "--raw", "--abbrev=40", f"{old}..{new}"])
    if not ok:
        return []
    moves: list[SubmoduleMove] = []
    for line in raw.splitlines():
        if not line.strip() or "\t" not in line:
            continue
        meta, _, path = line.partition("\t")
        path = path.split("\t")[0]  # rename: take the old path; rare, accepted
        parts = meta.lstrip(":").split()
        if len(parts) < 4:
            continue
        om, nm, os_sha, ns_sha = parts[0], parts[1], parts[2], parts[3]
        if om != "160000" and nm != "160000":
            continue
        sub = str(repo_root / path)
        old_zero = set(os_sha) == {"0"}
        new_zero = set(ns_sha) == {"0"}
        if old_zero:
            # New submodule: the commit count would walk its entire history,
            # which is both misleading and expensive.  The pull report renders
            # "new → <sha>" with no count, so 0 is both cheaper and consistent
            # with the jailbee-ls probe (git_status.py), which also reports 0 here.
            status, commits, ins, dels = "new", 0, 0, 0
            old_out, new_out = None, ns_sha
        elif new_zero:
            status, commits, ins, dels = "removed", 0, 0, 0
            old_out, new_out = os_sha, None
        else:
            status = "modified"
            commits = _count(sub, f"{os_sha}..{ns_sha}")
            ok2, ss = git.run_capture(sub, ["diff", "--shortstat", f"{os_sha}..{ns_sha}"])
            ins, dels = _shortstat_ints(ss) if ok2 else (0, 0)
            old_out, new_out = os_sha, ns_sha
        moves.append(SubmoduleMove(path, old_out, new_out, status, commits, ins, dels))
    return moves


def _count(sub: str, rev: str) -> int:
    ok, out = git.run_capture(sub, ["rev-list", "--count", rev])
    return int(out.strip()) if ok and out.strip().isdigit() else 0


_REPORT_RULE = "── Submodules " + "─" * 22


def render_submodule_report(
    *,
    moves: list[SubmoduleMove] | None = None,
    conflict: ConflictReport | None = None,
) -> str | None:
    """Render the delimited submodule report block, or None if empty.

    Used by ``jailbee pull`` after git's own output so it is not buried.
    """
    if conflict is not None:
        return _render_conflict_report(conflict)
    if not moves:
        return None
    lines = [_REPORT_RULE]
    for m in moves:
        if m.status == "new":
            detail = f"new → {(m.new_sha or '')[:7]}"
        elif m.status == "removed":
            detail = f"{(m.old_sha or '')[:7]} → removed"
        else:
            detail = (
                f"{(m.old_sha or '')[:7]}..{(m.new_sha or '')[:7]}  "
                f"({m.commits} commits, +{m.ins} -{m.dels})"
            )
        lines.append(f"  ✓ {m.path}  {detail}")
    return "\n".join(lines)


# What each unresolved reason means, in the user's terms rather than the
# resolver's. Keys are `submodules.UnresolvedSub.reason` values.
_UNRESOLVED_LABELS = {
    "content-conflict": "file conflicts",
    "nested-conflict": "nested submodule conflict",
    "dirty": "working tree dirty — commit or stash, then re-run",
    "deleted-side": "gitlink on one side only — pick a side by hand",
}


def _unresolved_lines(subs: list[submodules.UnresolvedSub], marker: str) -> list[str]:
    """Aligned ``<marker> <path>  <label>`` lines, each followed by its git output."""
    width = max(len(u.path) for u in subs)
    lines: list[str] = []
    for u in subs:
        label = _UNRESOLVED_LABELS.get(u.reason, u.reason)
        lines.append(f"    {marker} {u.path.ljust(width)}  {label}")
        lines.extend(f"        {outline}" for outline in u.output.splitlines())
    return lines


def _render_conflict_report(conflict: ConflictReport) -> str:
    """Group the outcome by what the user has to do about it.

    Auto-merged submodules need nothing; the ones git left mid-merge need
    resolving and committing; the skipped ones were never touched and need a
    different fix, so they are deliberately kept out of the resolve list.
    """
    r = conflict.resolution
    in_merge = [u for u in r.unresolved if u.in_merge_state]
    skipped = [u for u in r.unresolved if not u.in_merge_state]

    lines = [_REPORT_RULE]
    if r.resolved:
        lines.append(f"  auto-merged ({len(r.resolved)}):")
        lines.extend(f"    ✓ {path}" for path in r.resolved)
    if in_merge:
        lines.append(f"  in merge state — resolve these ({len(in_merge)}):")
        lines.extend(_unresolved_lines(in_merge, "✗"))
    if skipped:
        lines.append(f"  skipped, not touched ({len(skipped)}):")
        lines.extend(_unresolved_lines(skipped, "•"))
    if conflict.nongitlink:
        lines.append(
            f"  superproject also has non-submodule conflicts: {', '.join(conflict.nongitlink)}"
        )
    lines.append("  superproject left in merge state")
    lines.append("")
    lines.append("  to finish:")
    lines.extend(f"    {line}" for line in conflict.location.splitlines())
    if in_merge:
        lines.append("    # in each submodule above: resolve, then  git add -A && git commit")
    lines.append("    git add <paths> && git commit")
    return "\n".join(lines)


def _resolve_gitlinks_and_commit_host(cfg: Config, *, branch: str, location: str) -> None:
    """After a host superproject merge conflict, auto-merge submodule gitlinks.

    On a clean resolution, finalize the merge commit. Otherwise raise
    ``MergeConflictError`` (a ``SyncError`` subclass) carrying a
    ``ConflictReport``, leaving the host in merge state for the user.
    """
    top = str(cfg.repo_root)
    report = submodules.resolve_gitlink_conflicts(
        git.run_capture, top, message=_sub_merge_message(branch)
    )
    if submodules._has_unmerged(git.run_capture, top):
        raise MergeConflictError(
            f"Merge of '{branch}' hit conflicts — see the submodule report below.",
            report=ConflictReport(
                resolution=report,
                nongitlink=submodules._nongitlink_unmerged_paths(git.run_capture, top),
                branch=branch,
                location=location,
            ),
        )
    ok, _ = git.run_capture(top, ["commit", "--no-edit"])
    if not ok:
        raise SyncError("Submodule gitlinks resolved, but finalizing the merge commit failed.")


def push_and_merge(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    source: str | None = None,
    prefer_ref: SourcePref | None = None,
    fetch: bool | None = None,
    source_ref: str | None = None,
) -> MergeInContainerResult:
    """Push host's `source` into container and merge it into the current branch.

    Preflights (dirty tree, in-progress merge/rebase, detached HEAD) run
    inside the container before the push so a failure does not leave a
    partially-populated refs/jailbee/host/* ref. When the container's current
    branch matches `source`, the merge uses --ff-only. Conflicts raise
    SyncError with a hint pointing at `jailbee shell <short>`; the container
    is left in merge state for manual resolution.

    `prefer_ref` / `fetch` / `source_ref` are forwarded to `push_to_container`.
    """
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    full_name = resolve_container_name(cfg, incus, short)

    mode = incus.config_get(full_name, "user.jailbee.mode")
    if mode == "mount":
        raise SyncError(
            f"container '{short}' is in mount mode — host and container share "
            f"the working tree, so push is not applicable. Use git on the host directly."
        )
    if not _container_is_running(incus, full_name):
        raise SyncError(f"Container '{short}' is not running. Start it with: jailbee start {short}")

    repo_dir = container_repo_dir(cfg, incus, full_name)
    uid = cfg.container_user.uid
    container_branch = _run_container_preflights(incus, full_name, repo_dir, uid=uid)

    submodules.transport_submodules_to_container(cfg, incus, full_name, repo_dir=repo_dir)
    push_result = push_to_container(
        cfg,
        incus,
        short,
        source=source,
        prefer_ref=prefer_ref,
        fetch=fetch,
        source_ref=source_ref,
    )

    fast_forward_only = container_branch == push_result.source
    merge_cmd = ["git", "-C", repo_dir, "merge"]
    if fast_forward_only:
        merge_cmd.append("--ff-only")
    else:
        merge_cmd.extend(["-m", f"Merge '{push_result.source}' from host"])
    merge_cmd.append(push_result.container_ref)

    # `incus exec --user UID` doesn't derive HOME/USER/LOGNAME from
    # /etc/passwd. Git needs HOME to find the bind-mounted ~/.gitconfig
    # for user.name / user.email — without it, the merge commit fails
    # with "Committer identity unknown". See _attach_shell in cli.py.
    git_env = {
        "HOME": f"/home/{CONTAINER_USERNAME}",
        "USER": CONTAINER_USERNAME,
        "LOGNAME": CONTAINER_USERNAME,
    }

    try:
        incus.exec(full_name, merge_cmd, uid=uid, env=git_env)
    except IncusError as exc:
        if not _container_has_merge_in_progress(incus, full_name, repo_dir):
            raise SyncError(f"git merge failed in container '{short}': {exc}") from exc
        run = submodules._container_runner(incus, full_name, uid=uid, env=git_env)
        report = submodules.resolve_gitlink_conflicts(
            run, repo_dir, message=_sub_merge_message(push_result.source)
        )
        if submodules._has_unmerged(run, repo_dir):
            raise MergeConflictError(
                f"Merge of '{push_result.source}' in container '{short}' hit "
                f"conflicts — see the submodule report below.",
                report=ConflictReport(
                    resolution=report,
                    nongitlink=submodules._nongitlink_unmerged_paths(run, repo_dir),
                    branch=push_result.source,
                    location=f"jailbee shell {short}\ncd {repo_dir}",
                ),
            ) from exc
        # All conflicts were gitlink pointers the resolver staged — finalize.
        incus.exec(
            full_name,
            ["git", "-C", repo_dir, "commit", "--no-edit"],
            uid=uid,
            env=git_env,
        )
        # Fall through to the post-merge tail (update_submodules_in_container + head).

    submodules.update_submodules_in_container(
        incus, full_name, repo_dir=repo_dir, uid=uid, env=git_env
    )

    head_oid = _container_head_oid(incus, full_name, repo_dir, uid=uid)
    return MergeInContainerResult(
        push=push_result,
        container_branch=container_branch,
        fast_forward_only=fast_forward_only,
        head_oid=head_oid,
    )


def push_and_rebase(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    source: str | None = None,
    prefer_ref: SourcePref | None = None,
    fetch: bool | None = None,
    source_ref: str | None = None,
) -> RebaseInContainerResult:
    """Push host's `source` into container and rebase the current branch onto it.

    Same preflight regime as `push_and_merge`. Conflicts raise SyncError
    with a hint pointing at `jailbee shell <short>`; the container is left
    in rebase state for manual resolution. Same-branch is not treated
    specially — `git rebase` itself handles the no-op case.

    `prefer_ref` / `fetch` / `source_ref` are forwarded to `push_to_container`.
    """
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    full_name = resolve_container_name(cfg, incus, short)

    mode = incus.config_get(full_name, "user.jailbee.mode")
    if mode == "mount":
        raise SyncError(
            f"container '{short}' is in mount mode — host and container share "
            f"the working tree, so push is not applicable. Use git on the host directly."
        )
    if not _container_is_running(incus, full_name):
        raise SyncError(f"Container '{short}' is not running. Start it with: jailbee start {short}")

    repo_dir = container_repo_dir(cfg, incus, full_name)
    uid = cfg.container_user.uid
    container_branch = _run_container_preflights(incus, full_name, repo_dir, uid=uid)

    submodules.transport_submodules_to_container(cfg, incus, full_name, repo_dir=repo_dir)
    push_result = push_to_container(
        cfg,
        incus,
        short,
        source=source,
        prefer_ref=prefer_ref,
        fetch=fetch,
        source_ref=source_ref,
    )

    rebase_cmd = ["git", "-C", repo_dir, "rebase", push_result.container_ref]
    # See push_and_merge for why HOME/USER/LOGNAME must be set explicitly.
    git_env = {
        "HOME": f"/home/{CONTAINER_USERNAME}",
        "USER": CONTAINER_USERNAME,
        "LOGNAME": CONTAINER_USERNAME,
    }
    try:
        incus.exec(full_name, rebase_cmd, uid=uid, env=git_env)
    except IncusError as exc:
        if _container_has_rebase_in_progress(incus, full_name, repo_dir):
            raise SyncError(
                f"Conflict during rebase in container '{short}'.\n"
                f"Working tree left in rebase state.\n"
                f"\n"
                f"To resolve:\n"
                f"  jailbee shell {short}\n"
                f"  cd {repo_dir}\n"
                f"  # ...resolve conflicts...\n"
                f"  git rebase --continue   # or 'git rebase --abort'"
            ) from exc
        raise SyncError(f"git rebase failed in container '{short}': {exc}") from exc

    submodules.update_submodules_in_container(
        incus, full_name, repo_dir=repo_dir, uid=uid, env=git_env
    )

    head_oid = _container_head_oid(incus, full_name, repo_dir, uid=uid)
    return RebaseInContainerResult(
        push=push_result,
        container_branch=container_branch,
        head_oid=head_oid,
    )


def push_and_reset(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    source: str | None = None,
    prefer_ref: SourcePref | None = None,
    fetch: bool | None = None,
    source_ref: str | None = None,
) -> ResetInContainerResult:
    """Push host's `source` into container and hard-reset the current branch to it.

    Same preflight regime as `push_and_merge`/`push_and_rebase`. Refuses
    when the container's current branch differs from `source` — a
    force-reset only makes sense when both are the same logical branch.
    Non-fast-forward replacement is the whole point, so divergence is NOT
    refused; instead the result reports how many container-only commits
    were discarded.

    `prefer_ref` / `fetch` / `source_ref` are forwarded to `push_to_container`.
    """
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    full_name = resolve_container_name(cfg, incus, short)

    mode = incus.config_get(full_name, "user.jailbee.mode")
    if mode == "mount":
        raise SyncError(
            f"container '{short}' is in mount mode — host and container share "
            f"the working tree, so push is not applicable. Use git on the host directly."
        )
    if not _container_is_running(incus, full_name):
        raise SyncError(f"Container '{short}' is not running. Start it with: jailbee start {short}")

    repo_dir = container_repo_dir(cfg, incus, full_name)
    uid = cfg.container_user.uid
    container_branch = _run_container_preflights(incus, full_name, repo_dir, uid=uid)

    submodules.transport_submodules_to_container(cfg, incus, full_name, repo_dir=repo_dir)
    push_result = push_to_container(
        cfg,
        incus,
        short,
        source=source,
        prefer_ref=prefer_ref,
        fetch=fetch,
        source_ref=source_ref,
    )

    if container_branch != push_result.source:
        raise SyncError(
            f"container '{short}' is on '{container_branch}', not "
            f"'{push_result.source}'; --force only replaces the same "
            f"logical branch. Check out '{push_result.source}' in the "
            f"container first, or push without --force."
        )

    old_branch_oid = _container_head_oid(incus, full_name, repo_dir, uid=uid)
    # The discarded-commit count is purely informational, so never let a
    # rev-list hiccup (exec failure or non-numeric output) abort the reset.
    try:
        count_out = incus.exec(
            full_name,
            [
                "git",
                "-C",
                repo_dir,
                "rev-list",
                "--count",
                f"{push_result.container_ref}..{old_branch_oid}",
            ],
            uid=uid,
        )
        discarded_commits = int(count_out.strip() or "0")
    except (IncusError, ValueError):
        discarded_commits = 0

    # `git reset --hard` creates no commit, but update_submodules_in_container
    # below runs in-container git that needs HOME to find ~/.gitconfig. See
    # push_and_merge for why HOME/USER/LOGNAME must be set explicitly.
    git_env = {
        "HOME": f"/home/{CONTAINER_USERNAME}",
        "USER": CONTAINER_USERNAME,
        "LOGNAME": CONTAINER_USERNAME,
    }
    reset_cmd = ["git", "-C", repo_dir, "reset", "--hard", push_result.container_ref]
    try:
        incus.exec(full_name, reset_cmd, uid=uid, env=git_env)
    except IncusError as exc:
        raise SyncError(f"git reset --hard failed in container '{short}': {exc}") from exc

    # `reset --hard` moves the superproject gitlink but leaves submodule
    # working trees at their old commits — check them out to match, exactly
    # as push_and_merge/push_and_rebase do after their apply step.
    submodules.update_submodules_in_container(
        incus, full_name, repo_dir=repo_dir, uid=uid, env=git_env
    )

    head_oid = _container_head_oid(incus, full_name, repo_dir, uid=uid)
    return ResetInContainerResult(
        push=push_result,
        container_branch=container_branch,
        head_oid=head_oid,
        discarded_commits=discarded_commits,
        old_branch_oid=old_branch_oid,
    )


def _warn_before_container_destroy(cfg: Config, incus: Incus, full_name: str, short: str) -> bool:
    """Probe ``full_name``'s git status and guard its destroy; True to proceed.

    Mirrors `jailbee destroy`'s single-name guard shape: one `incus exec`, the
    same 3s timeout `jailbee ls` uses. The "gather and assess" step lives here,
    right beside the one call site that needs it (`run_post_merge_cleanup`
    doesn't already have a `ContainerInfo`, unlike the CLI's `--all`/picker
    paths); only the "print the summary and confirm" step is shared, via
    `tui.confirm_destroy_risk`.
    """
    from jailbee import lifecycle as _lifecycle
    from jailbee.destroy_guard import assess, status_is_unknown
    from jailbee.git_status import probe_container_git
    from jailbee.tui import confirm_destroy_risk

    ci = next(
        (c for c in _lifecycle.list_containers(cfg, incus) if c.name == full_name),
        None,
    )
    if ci is None:
        # Vanished from the listing between resolve and here — nothing to
        # probe, but silence is never safety: say so explicitly.
        return confirm_destroy_risk([short], [])

    if ci.state == "Running" and ci.mode != "mount" and ci.repo_dir:
        ci.git_status = probe_container_git(
            incus,
            ci.name,
            ci.repo_dir,
            ci.base_branch,
            cfg.default_branch,
            uid=cfg.container_user.uid,
            host_head=git.get_head_sha(cfg.repo_root),
        )

    unknown = [short] if status_is_unknown(ci) else []
    summary = assess(cfg, ci)
    return confirm_destroy_risk(unknown, [summary] if summary is not None else [])


def run_post_merge_cleanup(
    cfg: Config,
    incus: Incus,
    short: str,
    merge_result: MergeResult,
    *,
    destroy_policy: Literal["prompt", "always", "never"],
    branch_policy: Literal["prompt", "always", "never"],
) -> CleanupResult:
    """Optionally destroy the container and delete the merged host branch.

    Called by the CLI *after* it has printed the fetch summary and
    commit log, so the user sees what was merged before any prompt is
    shown.

    Each step has its own policy:
    - ``"always"`` → do it, no prompt.
    - ``"never"``  → skip it, no prompt.
    - ``"prompt"`` → ask interactively in TTY; skip silently in non-TTY.

    If the merge did not move HEAD (``pre_merge_head == head_oid``) the
    cleanup is skipped entirely *regardless of the policies* — a no-op
    merge can mean the user forgot to commit inside the container, so
    destroying it then would lose uncommitted work. The skip reason is
    returned so the CLI can warn the user.

    Note: ``fetch.commits_added == 0`` is *not* a reliable signal here.
    A prior ``jailbee git fetch``/``merge``/``checkout`` may have already
    populated the jailbee ref, so this fetch adds zero — yet the current
    host branch can still be behind that ref, in which case the merge
    does bring real changes into HEAD.

    Cleanup failures never raise; they're recorded in
    ``CleanupResult.cleanup_error`` so the merge stays the headline
    event.
    """
    from jailbee import lifecycle as _lifecycle

    if (
        merge_result.pre_merge_head is not None
        and merge_result.pre_merge_head == merge_result.head_oid
    ):
        return CleanupResult(
            destroyed=False,
            deleted_branch=False,
            cleanup_error=None,
            skipped_reason=("merge did not move HEAD — container kept in case of uncommitted work"),
        )

    target = merge_result.branch
    current = merge_result.into_branch
    full_name = _lifecycle.resolve_container_name(cfg, incus, short)

    destroyed = False
    cleanup_error: str | None = None
    # `merge_from_container` only fetches the container's *committed*
    # history (`fetch_from_container`) and never inspects its working
    # tree — unlike the push direction's dirty-tree preflight
    # (`_run_container_preflights` / `_container_status_dirty`), nothing
    # above already knows whether uncommitted edits remain on top of the
    # merged commits. The "HEAD didn't move" skip only catches a
    # wholesale no-op merge, not that case. So this destroy is guarded too.
    should_destroy = _should_run_cleanup_step(
        prompt=f"Destroy container '{short}'? [y/N] ",
        policy=destroy_policy,
    )
    # Only the interactive "prompt" policy needs the second guard prompt:
    # "always" is this call's --force equivalent (must never block, exactly
    # like `jailbee destroy --force`), and "never" or a declined/non-TTY
    # "prompt" already leave `should_destroy` False — nothing to probe for.
    if should_destroy and destroy_policy == "prompt":
        should_destroy = _warn_before_container_destroy(cfg, incus, full_name, short)
    if should_destroy:
        try:
            _lifecycle.destroy_container(cfg, incus, full_name, force=True)
            destroyed = True
        except Exception as exc:
            cleanup_error = str(exc)

    deleted_branch = False
    if (
        current is not None
        and git.local_branch_exists(cfg.repo_root, target)
        and target != current
        and git.is_merged_into(cfg.repo_root, target, current)
        and _should_run_cleanup_step(
            prompt=(
                f"Local branch '{target}' is fully merged into '{current}', "
                f"so deleting it loses no commits.\n"
                f"Delete merged local branch '{target}'? [y/N] "
            ),
            policy=branch_policy,
        )
    ):
        try:
            git.delete_branch(cfg.repo_root, target)
            deleted_branch = True
        except Exception as exc:
            if cleanup_error is None:
                cleanup_error = str(exc)
            else:
                cleanup_error = f"{cleanup_error}; {exc}"

    return CleanupResult(
        destroyed=destroyed,
        deleted_branch=deleted_branch,
        cleanup_error=cleanup_error,
        skipped_reason=None,
    )


def _should_run_cleanup_step(
    *,
    prompt: str,
    policy: Literal["prompt", "always", "never"],
) -> bool:
    """Decide whether to run a cleanup step.

    Rules:
    - ``policy="always"`` → run, no prompt.
    - ``policy="never"``  → skip, no prompt.
    - ``policy="prompt"`` + non-TTY → skip silently.
    - ``policy="prompt"`` + TTY     → ask ``[y/N]``, default "no".
    """
    if policy == "always":
        return True
    if policy == "never":
        return False
    if not _stdin_is_interactive():
        return False
    answer = input(prompt).strip().lower()
    return answer in ("y", "yes")


_DIFF_STAT_SNIPPET = r"""
set +e
cd "$REPO_DIR" 2>/dev/null || exit 0
CF=""
[ "$COLOR" = "1" ] && CF="--color=always"

emit_committed() {
  SUPER=$(git diff --stat --ignore-submodules=all $CF "${BASE}...HEAD" 2>/dev/null)
  SUBS=$(
    IFS_TAB="$(printf '\t')"
    git diff --raw --abbrev=40 "${BASE}...HEAD" 2>/dev/null \
      | while IFS="$IFS_TAB" read -r meta sub_path; do
      [ -z "$sub_path" ] && continue
      set -- $meta
      om=${1#:}; nm=$2; os=$3; ns=$4
      [ "$om" = "160000" ] || [ "$nm" = "160000" ] || continue
      case "$os" in *[!0]*) ;; *) continue ;; esac
      case "$ns" in *[!0]*) ;; *) continue ;; esac
      out=$(git -C "$sub_path" diff --stat $CF "$os".."$ns" 2>/dev/null)
      [ -n "$out" ] && printf '=== %s ===\n%s\n' "$sub_path" "$out"
    done
  )
  _render
}

emit_wt() {
  SUPER=$(git diff --stat --ignore-submodules=all $CF HEAD 2>/dev/null)
  SUB_CMD='out=$(git diff --stat '"$CF"' HEAD 2>/dev/null)
[ -n "$out" ] && printf "=== %s ===\n%s\n" "$displaypath" "$out"; :'
  SUBS=$(git submodule foreach --recursive --quiet "$SUB_CMD" 2>/dev/null)
  _render
}

_render() {
  if [ -n "$SUBS" ]; then
    [ -n "$SUPER" ] && printf '=== superproject ===\n%s\n' "$SUPER"
    printf '%s\n' "$SUBS"
  else
    [ -n "$SUPER" ] && printf '%s\n' "$SUPER"
  fi
}

case "$MODE" in
  wt) emit_wt ;;
  committed) emit_committed ;;
  all) emit_wt; printf '\n=== committed (vs base) ===\n'; emit_committed ;;
esac
"""


def diff_from_container(
    cfg: Config,
    incus: Incus,
    short: str,
    *,
    branch: str | None = None,
    mode: Literal["committed", "wt", "all"] = "committed",
    stat_only: bool = False,
    color: bool = True,
) -> str:
    """Stream a `git diff` from inside container `short`.

    - ``mode="committed"``: ``git diff <base>...HEAD`` (patch) or the
      per-submodule stat snippet (when ``stat_only=True``) where
      ``<base>`` is resolved as: ``refs/jailbee/base/<base_branch>`` →
      ``origin/<base_branch>`` → ``refs/heads/<base_branch>`` →
      ``origin/<default_branch>``.
      Raises ``SyncError`` if none resolves.
    - ``mode="wt"``: ``git diff HEAD`` (patch) or per-submodule stat
      snippet (when ``stat_only=True``) — working tree vs HEAD.
      ``branch`` is ignored.
    - ``mode="all"``: ``"wt"`` output followed by ``"committed"`` output,
      joined by a separator line.

    Patch modes pass ``--submodule=diff`` so changes *inside* submodules
    appear inline. Stat mode (``stat_only=True``) uses ``_DIFF_STAT_SNIPPET``
    which groups per-submodule stats under ``=== <path> ===`` headers when
    submodule changes are present; otherwise returns plain stat output.

    Refuses mount-mode and stopped containers. Returns the diff as a
    single string (the caller can write straight to stdout).
    """
    # `branch` is accepted for parity with fetch/checkout/merge so callers
    # can pass --branch through unconditionally. The current container
    # working tree is what we sniff regardless; if the user wants a diff
    # against a different branch's tip, they can run `jailbee git fetch -b
    # <branch> <short>` first and then `git diff` on the host.
    del branch

    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    full_name = resolve_container_name(cfg, incus, short)

    if incus.config_get(full_name, "user.jailbee.mode") == "mount":
        raise SyncError(
            f"container '{short}' is in mount mode — host and container share "
            f"the working tree, so diff is not applicable. Use git on the host "
            f"directly."
        )

    if not _container_is_running(incus, full_name):
        raise SyncError(f"Container '{short}' is not running. Start it with: jailbee start {short}")

    repo_dir = container_repo_dir(cfg, incus, full_name)
    uid = cfg.container_user.uid

    def _run_diff(args: list[str]) -> str:
        cmd = ["git", "-C", repo_dir, "diff", "--submodule=diff"]
        if color:
            cmd.append("--color=always")
        cmd += args
        return incus.exec(full_name, cmd, uid=uid)

    def _run_stat(snippet_mode: str, resolved_base: str) -> str:
        return incus.exec(
            full_name,
            ["bash", "-c", _DIFF_STAT_SNIPPET],
            env={
                "REPO_DIR": repo_dir,
                "BASE": resolved_base,
                "MODE": snippet_mode,
                "COLOR": "1" if color else "0",
            },
            uid=uid,
        )

    if mode == "wt":
        if stat_only:
            return _run_stat("wt", "")
        return _run_diff(["HEAD"])

    base_label = incus.config_get(full_name, "user.jailbee.base_branch")
    base_branch = base_label if isinstance(base_label, str) and base_label else None

    def _resolves(ref: str) -> bool:
        try:
            check = incus.exec(
                full_name,
                [
                    "git",
                    "-C",
                    repo_dir,
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    f"{ref}^{{commit}}",
                ],
                uid=uid,
            )
        except IncusError:
            return False
        return bool(check.strip())

    candidates: list[str] = []
    if base_branch:
        candidates.append(f"refs/jailbee/base/{base_branch}")
        candidates.append(f"refs/remotes/origin/{base_branch}")
        candidates.append(f"refs/heads/{base_branch}")
    candidates.append(f"refs/remotes/origin/{cfg.default_branch}")
    base: str | None = None
    for ref in candidates:
        if _resolves(ref):
            base = ref
            break
    if base is None:
        raise SyncError(
            f"Cannot resolve base in container '{short}': none of {', '.join(candidates)} found."
        )

    if stat_only:
        return _run_stat(mode, base)

    committed = _run_diff([f"{base}...HEAD"])

    if mode == "committed":
        return committed

    wt = _run_diff(["HEAD"])
    sep = "\n=== committed (vs base) ===\n"
    return wt + sep + committed
