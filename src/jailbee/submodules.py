"""Submodule orchestration for jailbee: offline init on `jailbee new`, plus object
transport + working-tree update across `jailbee push`/`jailbee pull`/`jailbee git
checkout`.

This is the single module that knows how submodules move between the host
repo and a container's clone. It calls `incus.exec` for container-side git
and the `git` helpers for host-side git. It deliberately does NOT import
`sync` (which imports this module) — the ext:: URL builders are duplicated
here as small helpers to avoid a cycle.

Offline by design: submodule objects come from the read-only host bind mount
(`/mnt/host-source`) on `jailbee new`, and from the peer over the existing ext::
transport on push/pull. Failures are hard (`SubmoduleError`) — the spec
chooses loud failure over silent drift.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee import git
from jailbee.incus import IncusError

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

# (cwd, git-args-without-"git") -> (exit_ok, stdout). Lets the placement
# routine run identically against the host (subprocess) and a container
# (incus.exec).
GitRun = Callable[[str, list[str]], tuple[bool, str]]


def _container_runner(
    incus: Incus,
    container: str,
    *,
    uid: int,
    gid: int | None = None,
    env: dict[str, str] | None = None,
) -> GitRun:
    """Build a `GitRun` that executes git inside `container` via `incus.exec`."""

    def run(cwd: str, args: list[str]) -> tuple[bool, str]:
        try:
            out = incus.exec(container, ["git", "-C", cwd, *args], uid=uid, gid=gid, env=env)
        except IncusError:
            return (False, "")
        return (True, out)

    return run


def _warn(message: str) -> None:
    """Print a non-fatal placement warning (yellow), lazily importing the console."""
    from jailbee.tui import console

    console.print(f"[yellow]⚠ {message}[/yellow]")


def _is_dirty(run: GitRun, sub: str) -> bool:
    ok, out = run(sub, ["status", "--porcelain"])
    return ok and bool(out.strip())


def _local_branch_exists(run: GitRun, sub: str, branch: str) -> bool:
    ok, _ = run(sub, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    return ok


def _is_ancestor(run: GitRun, sub: str, ancestor: str, descendant: str) -> bool:
    ok, _ = run(sub, ["merge-base", "--is-ancestor", ancestor, descendant])
    return ok


def _short_sha(run: GitRun, sub: str, rev: str) -> str:
    ok, out = run(sub, ["rev-parse", "--short", rev])
    short = out.strip()
    return short if ok and short else rev


def _place_one(run: GitRun, sub: str, branch: str) -> None:
    """Best-effort: put submodule `sub` on `branch` near its current gitlink (HEAD).

    Guards: refuse on a dirty working tree. For the gitlink-vs-branch relationship:

    - branch absent, or gitlink at/ahead of branch -> (re)point `branch` at the
      gitlink and check it out (fast-forward; never rewinds the branch).
    - gitlink a strict *ancestor* of an existing branch (the branch is ahead —
      typically a skipped superproject gitlink bump) -> keep the newer branch
      checked out instead of detaching to the stale gitlink, and warn actionably.
    - genuine divergence (neither is an ancestor of the other) -> conservatively
      leave the working tree on the detached gitlink and warn.

    Never raises.
    """
    if _is_dirty(run, sub):
        _warn(f"submodule '{sub}': working tree dirty — left on detached HEAD")
        return
    ok, sha_out = run(sub, ["rev-parse", "HEAD"])
    sha = sha_out.strip()
    if not ok or not sha:
        return
    # New branch, or gitlink at/ahead of the branch: point branch at the gitlink
    # and check it out. This is always a fast-forward — it never rewinds branch.
    if not _local_branch_exists(run, sub, branch) or _is_ancestor(run, sub, branch, sha):
        run(sub, ["checkout", "-B", branch, sha])
        return
    # Branch exists and the gitlink is NOT at/ahead of it.
    if _is_ancestor(run, sub, sha, branch):
        # Benign: the branch is strictly ahead of the gitlink (the recorded
        # superproject pointer is stale — e.g. a submodule commit was published
        # without bumping the gitlink). Keep the newer branch checked out rather
        # than detaching/rewinding to the stale gitlink; warn with the fix.
        short_branch = _short_sha(run, sub, branch)
        short_gitlink = _short_sha(run, sub, sha)
        _warn(
            f"submodule '{sub}': local '{branch}' ({short_branch}) is ahead of the "
            f"superproject gitlink ({short_gitlink}) — the gitlink is stale. Bump it "
            f"in the superproject ('git add {sub} && git commit') or pushes of "
            f"'{branch}' may be rejected (non-fast-forward). Leaving '{branch}' "
            f"checked out."
        )
        run(sub, ["checkout", branch])
        return
    # Genuine divergence: neither side is an ancestor of the other.
    _warn(f"submodule '{sub}': gitlink and local '{branch}' have diverged — left on detached HEAD")


def _gitmodules_paths(run: GitRun, top_dir: str) -> list[tuple[str, str]]:
    """Parse `top_dir/.gitmodules` -> list of (name, path). [] if absent.

    Intentionally separate from `_list_gitmodules`: that function calls
    `incus.exec` directly with a uid argument; this one uses the `GitRun`
    executor abstraction so it works for both host-side and container-side callers.
    """
    ok, out = run(
        top_dir,
        ["config", "-f", f"{top_dir}/.gitmodules", "--get-regexp", r"^submodule\..*\.path$"],
    )
    if not ok:
        return []
    result: list[tuple[str, str]] = []
    for line in out.splitlines():
        key, _, path = line.strip().partition(" ")
        if key.startswith("submodule.") and key.endswith(".path") and path:
            name = key[len("submodule.") : -len(".path")]
            result.append((name, path))
    return result


def _gitmodules_branch(run: GitRun, top_dir: str, name: str) -> str | None:
    """Return `submodule.<name>.branch` from `top_dir/.gitmodules`, or None."""
    ok, out = run(top_dir, ["config", "-f", f"{top_dir}/.gitmodules", f"submodule.{name}.branch"])
    if not ok:
        return None
    return out.strip() or None


def declared_branch_for_path(run: GitRun, parent_dir: str, leaf: str) -> str | None:
    """The branch `.gitmodules` declares for `leaf`, or None.

    `.` is treated as undeclared: it means "track the superproject's branch",
    which is not a branch name a PR can target.
    """
    for name, path in _gitmodules_paths(run, parent_dir):
        if path != leaf:
            continue
        declared = _gitmodules_branch(run, parent_dir, name)
        return declared if declared and declared != "." else None
    return None


def declared_branch_for_top_relative_path(run: GitRun, repo_root: str, subpath: str) -> str | None:
    """The branch `.gitmodules` declares for a *top-relative* `subpath`, or None.

    `declared_branch_for_path` needs the immediate declaring level already
    known; this descends the `.gitmodules` chain from `repo_root` to find it.
    The naive approach — split `subpath` on its last `/` and treat everything
    before it as the declaring level's directory — is wrong for the common
    case: a top-level submodule at `libs/foo` is declared by
    `repo_root/.gitmodules` with `path = libs/foo`, a single entry, not by
    some `repo_root/libs/.gitmodules`. A path component boundary is not
    necessarily a `.gitmodules` level boundary.

    Instead, at each level read that level's `.gitmodules`: an entry whose
    `path` equals the remaining subpath is the declaring entry (delegate the
    final lookup to `declared_branch_for_path`, which also handles the `.`
    convention); an entry whose `path` is a strict prefix of the remaining
    subpath names an intermediate submodule to descend into, with the
    remainder re-checked against *its* `.gitmodules`. Returns None when no
    level in the chain accounts for the full `subpath`.
    """
    level_dir = repo_root
    remaining = subpath
    while True:
        for _name, path in _gitmodules_paths(run, level_dir):
            if path == remaining:
                return declared_branch_for_path(run, level_dir, remaining)
            if remaining.startswith(f"{path}/"):
                level_dir = f"{level_dir}/{path}"
                remaining = remaining[len(path) + 1 :]
                break
        else:
            return None


def _current_branch(run: GitRun, sub: str) -> str | None:
    """The submodule's current branch, or None when on a detached HEAD."""
    ok, out = run(sub, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    name = out.strip()
    return name if ok and name else None


def report_submodule_branches(run: GitRun, top_dir: str) -> list[tuple[str, str | None]]:
    """Recursively report each submodule's ``(relative path, current branch)``.

    Read-only. ``branch`` is None for a detached submodule. Nested submodule
    paths are prefixed by their parent's path (``lib/inner``).
    """
    result: list[tuple[str, str | None]] = []
    for _name, path in _gitmodules_paths(run, top_dir):
        sub = f"{top_dir}/{path}"
        result.append((path, _current_branch(run, sub)))
        for rel, cur in report_submodule_branches(run, sub):
            result.append((f"{path}/{rel}", cur))
    return result


def _place_submodule_branches(run: GitRun, top_dir: str, branch: str | None = None) -> None:
    """Recursively place submodules on a working branch.

    When ``branch`` is given (container callers), place *every* submodule
    recursively on that branch — the container's branch — regardless of any
    ``.gitmodules`` ``branch`` declaration. When ``branch`` is None (the host
    caller), preserve the legacy behaviour: place only submodules that declare
    ``submodule.<name>.branch`` (and not ``.``) on that declared branch; leave
    the rest on their detached HEAD. Nested submodules use their immediate
    parent's ``.gitmodules``. Best-effort — never raises.
    """
    for name, path in _gitmodules_paths(run, top_dir):
        sub = f"{top_dir}/{path}"
        if branch is not None:
            _place_one(run, sub, branch)
        else:
            declared = _gitmodules_branch(run, top_dir, name)
            if declared and declared != ".":
                _place_one(run, sub, declared)
        _place_submodule_branches(run, sub, branch)


def _gitlink_at(run: GitRun, repo_dir: str, commit: str, path: str) -> str | None:
    """Return the submodule (gitlink) commit recorded for ``path`` in tree
    ``commit``. ``commit`` is resolved in ``repo_dir``'s object store. None when
    ``path`` is not a gitlink there, or the lookup fails.
    """
    ok, out = run(repo_dir, ["ls-tree", commit, "--", path])
    if not ok:
        return None
    # "<mode> <type> <sha>\t<path>" -> ["160000", "commit", "<sha>", "<path>"]
    parts = out.split()
    if len(parts) >= 3 and parts[0] == "160000" and parts[1] == "commit":
        return parts[2]
    return None


def _detect_submodule_default(run: GitRun, parent_dir: str, sub: str, name: str) -> str:
    """Detect a submodule's default branch name.

    Order: ``submodule.<name>.branch`` in the parent's ``.gitmodules`` (unless
    ``.``) -> the submodule's ``origin/HEAD`` target -> ``main``.

    The literal ``origin`` is correct here and must not be parameterised: both
    callers run this through a *container* `GitRun`, and a container sub-repo's
    remote is one jailbee created itself (see `_create_container_subrepo`).
    """
    declared = _gitmodules_branch(run, parent_dir, name)
    if declared and declared != ".":
        return declared
    ok, out = run(sub, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    ref = out.strip() if ok else ""
    if ref:
        return ref.split("/", 1)[1] if ref.startswith("origin/") else ref
    return "main"


def _walk_submodule_dirs(run: GitRun, top_dir: str) -> list[str]:
    """Absolute container dirs of every submodule under ``top_dir``, recursively."""
    dirs: list[str] = []
    for _name, path in _gitmodules_paths(run, top_dir):
        sub = f"{top_dir}/{path}"
        dirs.append(sub)
        dirs.extend(_walk_submodule_dirs(run, sub))
    return dirs


def delete_submodule_base_anchors(run: GitRun, top_dir: str, base_branch: str) -> None:
    """Delete ``refs/jailbee/base/<base_branch>`` in every submodule, recursively.

    Used on retarget when the old base branch's anchor is stale. Best-effort.
    """
    for sub in _walk_submodule_dirs(run, top_dir):
        run(sub, ["update-ref", "-d", f"refs/jailbee/base/{base_branch}"])


def seed_submodule_base_anchors(
    run: GitRun, top_dir: str, *, base_branch: str, container_branch: str
) -> None:
    """Pin, in every submodule (recursively), a base anchor at the submodule
    commit recorded at the superproject's ``refs/jailbee/base/<base_branch>``.

    Two anchors per submodule, both at that commit (``B_sub``):

    * ``refs/jailbee/base/<base_branch>`` — jailbee's fetch-proof anchor, named after the
      superproject base branch for consistency. Always force-set (unconditional
      ``update-ref``).
    * a pinned local default branch (``refs/heads/<default>``) named after the
      submodule's own default branch, so the user can ``git diff <default>``
      inside the submodule. Skipped when ``<default>`` equals
      ``container_branch`` (that is the checked-out working branch). Pinned
      fast-forward-only: created when absent, advanced only when ``B_sub`` is
      a fast-forward of the current branch tip — never rewound.

    Best-effort — never raises. A submodule whose ``B_sub`` cannot be resolved
    (missing object, no base ref, added/removed gitlink) is skipped.
    """
    _seed_level(
        run,
        top_dir,
        f"refs/jailbee/base/{base_branch}",
        base_branch=base_branch,
        container_branch=container_branch,
    )


def _seed_level(
    run: GitRun,
    top_dir: str,
    base_commit: str,
    *,
    base_branch: str,
    container_branch: str,
) -> None:
    for name, path in _gitmodules_paths(run, top_dir):
        sub = f"{top_dir}/{path}"
        b_sub = _gitlink_at(run, top_dir, base_commit, path)
        if b_sub is None:
            continue
        run(sub, ["update-ref", f"refs/jailbee/base/{base_branch}", b_sub])
        default = _detect_submodule_default(run, top_dir, sub, name)
        if default and default != container_branch:
            # Pinned, fast-forward-only: create the local default branch when
            # absent, else advance it to b_sub only when that is a fast-forward
            # (b_sub at/ahead of the current tip). Never rewind — mirrors the
            # never-rewind guard in _place_one — so a migrating legacy container
            # cannot orphan submodule commits sitting on this branch.
            if not _local_branch_exists(run, sub, default) or _is_ancestor(
                run, sub, default, b_sub
            ):
                run(sub, ["update-ref", f"refs/heads/{default}", b_sub])
        _seed_level(run, sub, b_sub, base_branch=base_branch, container_branch=container_branch)


def _unmerged_entries(run: GitRun, top_dir: str) -> dict[str, dict[int, tuple[str, str]]]:
    """Map ``path -> {stage: (mode, sha)}`` for every unmerged index entry.

    Parses ``git ls-files -u`` (``<mode> <sha> <stage>\\t<path>``). Returns ``{}``
    when the tree is clean or the command fails.
    """
    ok, out = run(top_dir, ["ls-files", "-u"])
    entries: dict[str, dict[int, tuple[str, str]]] = {}
    if not ok:
        return entries
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        if not path:
            continue
        parts = meta.split()
        if len(parts) != 3:
            continue
        mode, sha, stage = parts
        entries.setdefault(path, {})[int(stage)] = (mode, sha)
    return entries


def _has_unmerged(run: GitRun, top_dir: str) -> bool:
    """True if the index under ``top_dir`` has any unmerged entry."""
    return bool(_unmerged_entries(run, top_dir))


def _conflicted_gitlinks(run: GitRun, top_dir: str) -> list[tuple[str, str | None, str | None]]:
    """Paths with a gitlink (mode 160000) conflict -> ``(path, ours_sha, theirs_sha)``.

    A side missing from the index (gitlink added/removed on only one side) is
    reported as ``None`` for that side.
    """
    result: list[tuple[str, str | None, str | None]] = []
    for path, stages in _unmerged_entries(run, top_dir).items():
        if "160000" not in {mode for mode, _sha in stages.values()}:
            continue
        ours = stages.get(2)
        theirs = stages.get(3)
        result.append((path, ours[1] if ours else None, theirs[1] if theirs else None))
    return result


def _nongitlink_paths(entries: dict[str, dict[int, tuple[str, str]]]) -> list[str]:
    """Sorted paths in ``entries`` that are NOT gitlinks (ordinary file conflicts)."""
    return sorted(
        path
        for path, stages in entries.items()
        if "160000" not in {mode for mode, _sha in stages.values()}
    )


def _nongitlink_unmerged_paths(run: GitRun, top_dir: str) -> list[str]:
    """Sorted unmerged paths that are NOT gitlinks (ordinary file conflicts)."""
    return _nongitlink_paths(_unmerged_entries(run, top_dir))


# Reasons that leave the submodule mid-merge; the rest were skipped untouched.
_IN_MERGE_STATE_REASONS = frozenset({"content-conflict", "nested-conflict"})


@dataclass(frozen=True)
class UnresolvedSub:
    """A submodule whose gitlink conflict could not be auto-resolved."""

    path: str
    reason: str  # "dirty" | "content-conflict" | "nested-conflict" | "deleted-side"
    output: str  # captured git output (CONFLICT lines), "" when N/A

    @property
    def in_merge_state(self) -> bool:
        """True when git left this submodule mid-merge, so it needs resolving
        and a commit; False when it was skipped untouched (dirty tree, gitlink
        present on only one side) and needs a different fix entirely.
        """
        return self.reason in _IN_MERGE_STATE_REASONS


@dataclass(frozen=True)
class GitlinkResolution:
    """Outcome of resolving submodule gitlink conflicts under a superproject merge."""

    resolved: list[str]
    unresolved: list[UnresolvedSub]


def resolve_gitlink_conflicts(run: GitRun, top_dir: str, *, message: str) -> GitlinkResolution:
    """Auto-merge conflicted submodule gitlinks under an in-progress superproject merge.

    For each path with a gitlink (mode 160000) conflict, merge ``theirs`` into
    ``ours`` inside the submodule and stage the merged pointer, recursing into
    nested submodules whose own gitlinks conflict in turn. Never fails fast: a
    submodule that cannot be resolved is recorded in ``unresolved`` and the next
    one is still attempted, so one pass reports every conflict. Best-effort and
    never raises. Only gitlinks are touched; ordinary file conflicts are never
    resolved here.

    Paths are reported relative to ``top_dir``, so a nested submodule surfaces
    as ``lib/inner`` in both lists.
    """
    resolved: list[str] = []
    unresolved: list[UnresolvedSub] = []
    for path, ours_sha, theirs_sha in _conflicted_gitlinks(run, top_dir):
        sub_resolved, sub_unresolved = _resolve_one_gitlink(
            run, top_dir, path, ours_sha, theirs_sha, message=message
        )
        resolved.extend(sub_resolved)
        unresolved.extend(sub_unresolved)
    return GitlinkResolution(resolved, unresolved)


def _resolve_one_gitlink(
    run: GitRun,
    top_dir: str,
    path: str,
    ours_sha: str | None,
    theirs_sha: str | None,
    *,
    message: str,
) -> tuple[list[str], list[UnresolvedSub]]:
    """Resolve one conflicted gitlink -> ``(resolved paths, unresolved)``.

    Both lists are ``top_dir``-relative and may carry nested submodule paths.
    """
    sub = f"{top_dir}/{path}"
    if ours_sha is None or theirs_sha is None:
        return ([], [UnresolvedSub(path, "deleted-side", "")])
    if _is_dirty(run, sub):
        return ([], [UnresolvedSub(path, "dirty", "")])

    run(sub, ["checkout", "--detach", ours_sha])
    ok, out = run(sub, ["merge", "--no-edit", "-m", message, theirs_sha])
    entries = _unmerged_entries(run, sub)
    if not ok and not entries:
        # git refused the merge outright (unrelated histories, a stale index,
        # a missing object): there is nothing staged to finish.
        return ([], [UnresolvedSub(path, "content-conflict", out)])
    if _nongitlink_paths(entries):
        return ([], [UnresolvedSub(path, "content-conflict", out)])

    # Either the merge succeeded, or its only conflicts are the submodule's own
    # gitlinks — which git leaves for us (`CONFLICT (submodule)`, exit 1).
    nested = resolve_gitlink_conflicts(run, sub, message=message)
    resolved = [f"{path}/{p}" for p in nested.resolved]
    if nested.unresolved or _has_unmerged(run, sub):
        # The nested entries are reported in their own right (prefixed), so the
        # parent only records that it is blocked on them — no restated detail.
        unresolved = [
            UnresolvedSub(f"{path}/{u.path}", u.reason, u.output) for u in nested.unresolved
        ]
        unresolved.append(UnresolvedSub(path, "nested-conflict", ""))
        return (resolved, unresolved)

    if entries:
        # The merge stopped on those gitlinks and never committed; now that they
        # are staged, finishing it is what turns `sub` into a mergeable pointer.
        commit_ok, commit_out = run(sub, ["commit", "--no-edit"])
        if not commit_ok:
            return (resolved, [UnresolvedSub(path, "nested-conflict", commit_out or out)])

    run(top_dir, ["add", path])
    resolved.append(path)
    return (resolved, [])


HOST_SOURCE = "/mnt/host-source"
PROTOCOL_FILE_ALLOW = "protocol.file.allow=always"


class SubmoduleError(RuntimeError):
    """Raised when submodules cannot be brought fully into sync (user-visible)."""


def _list_gitmodules(
    incus: Incus, container: str, level_dir: str, *, uid: int
) -> list[tuple[str, str]]:
    """Parse one level's `.gitmodules` -> list of (name, path).

    `level_dir` is the working tree of the level being inspected
    (`repo_dir` at the top, `repo_dir/<path>` when recursing). Returns []
    when there is no `.gitmodules` (the `git config` call exits non-zero,
    which surfaces as `IncusError`).

    Reads pass only `uid` (no `gid`) since they don't write group-owned files.
    """
    try:
        out = incus.exec(
            container,
            [
                "git",
                "config",
                "-f",
                f"{level_dir}/.gitmodules",
                "--get-regexp",
                r"^submodule\..*\.path$",
            ],
            uid=uid,
        )
    except IncusError:
        return []

    result: list[tuple[str, str]] = []
    for line in out.splitlines():
        key, _, path = line.strip().partition(" ")
        if key.startswith("submodule.") and key.endswith(".path") and path:
            name = key[len("submodule.") : -len(".path")]
            result.append((name, path))
    return result


def init_submodules_in_container(
    incus: Incus,
    container: str,
    *,
    repo_dir: str,
    uid: int,
    gid: int,
    branch: str | None = None,
    base_branch: str | None = None,
) -> None:
    """Recursively init the container clone's submodules offline.

    For each submodule, point its URL at the matching `/mnt/host-source`
    subdir, `submodule update --init` from there (no network), then
    `submodule sync` to repoint origin at the real upstream. Recurses into
    each initialized submodule. Raises `SubmoduleError` if the host has not
    initialized a submodule it declares.

    Then places every submodule on ``branch`` (the container branch) and, when
    ``base_branch`` is also given, seeds each submodule's base anchors at the
    gitlink recorded at the superproject's ``refs/jailbee/base/<base_branch>``.
    """
    _init_level(
        incus,
        container,
        repo_dir=repo_dir,
        level_dir=repo_dir,
        host_source=HOST_SOURCE,
        uid=uid,
        gid=gid,
    )
    run = _container_runner(incus, container, uid=uid, gid=gid)
    _place_submodule_branches(run, repo_dir, branch)
    if branch and base_branch:
        seed_submodule_base_anchors(run, repo_dir, base_branch=base_branch, container_branch=branch)


def _init_level(
    incus: Incus,
    container: str,
    *,
    repo_dir: str,
    level_dir: str,
    host_source: str,
    uid: int,
    gid: int,
) -> None:
    for name, path in _list_gitmodules(incus, container, level_dir, uid=uid):
        local_source = f"{host_source}/{path}"
        try:
            incus.exec(container, ["test", "-e", f"{local_source}/.git"], uid=uid)
        except IncusError as exc:
            raise SubmoduleError(
                f"host submodule '{path}' is not initialized — run "
                f"'git submodule update --init --recursive' in the host repo "
                f"and retry"
            ) from exc

        incus.exec(
            container,
            ["git", "-C", level_dir, "config", f"submodule.{name}.url", local_source],
            uid=uid,
            gid=gid,
        )
        incus.exec(
            container,
            [
                "git",
                "-C",
                level_dir,
                "-c",
                PROTOCOL_FILE_ALLOW,
                "submodule",
                "update",
                "--init",
                "--",
                path,
            ],
            uid=uid,
            gid=gid,
        )
        incus.exec(
            container,
            ["git", "-C", level_dir, "submodule", "sync", "--", path],
            uid=uid,
            gid=gid,
        )
        _init_level(
            incus,
            container,
            repo_dir=repo_dir,
            level_dir=f"{level_dir}/{path}",
            host_source=local_source,
            uid=uid,
            gid=gid,
        )


def update_submodules_in_container(
    incus: Incus,
    container: str,
    *,
    repo_dir: str,
    uid: int,
    env: dict[str, str],
    branch: str | None = None,
) -> None:
    """`git submodule update --init --recursive` inside the container.

    Acts as the apply-and-verify gate after a container-side merge/rebase:
    a missing object makes git fail, surfaced here as `SubmoduleError`.
    `env` must carry HOME/USER/LOGNAME (git needs HOME for ~/.gitconfig).

    When `branch` is given, submodules are placed on it directly. When
    `branch` is None (the default), the container branch is read from the
    `user.jailbee.branch` label, preserving prior behaviour.
    """
    try:
        incus.exec(
            container,
            [
                "git",
                "-C",
                repo_dir,
                "-c",
                PROTOCOL_FILE_ALLOW,
                "submodule",
                "update",
                "--init",
                "--recursive",
            ],
            uid=uid,
            env=env,
        )
    except IncusError as exc:
        raise SubmoduleError(
            f"submodule update failed in container '{container}': {exc}\n"
            f"A submodule commit is missing or a submodule is uninitialized."
        ) from exc
    if branch is None:
        branch = incus.config_get(container, "user.jailbee.branch")
    _place_submodule_branches(
        _container_runner(incus, container, uid=uid, env=env), repo_dir, branch
    )


def update_submodules_on_host(repo_root: Path, branch: str | None = None) -> None:
    """`git submodule update --init --recursive` on the host working tree, then
    place every submodule on ``branch`` (recursively).

    When ``branch`` is given, each submodule is placed on it regardless of any
    ``.gitmodules`` ``branch`` declaration — mirroring the container. When
    ``branch`` is None (e.g. the host is in detached HEAD), only submodules that
    declare a ``.gitmodules`` branch are placed (legacy behaviour, the
    detached-HEAD safety valve).
    """
    try:
        git.submodule_update(repo_root)
    except git.GitError as exc:
        raise SubmoduleError(
            f"submodule update failed on host: {exc}\n"
            f"A submodule commit is missing or a submodule is uninitialized."
        ) from exc
    _place_submodule_branches(git.run_capture, str(repo_root), branch)


def _sub_upload_pack_url(cfg: Config, container: str, repo_dir: str, subpath: str) -> str:
    return (
        f"ext::incus exec --user {cfg.container_user.uid} {container} "
        f"-- git upload-pack {repo_dir}/{subpath}"
    )


def _sub_receive_pack_url(cfg: Config, container: str, repo_dir: str, subpath: str) -> str:
    return (
        f"ext::incus exec --user {cfg.container_user.uid} {container} "
        f"-- git receive-pack {repo_dir}/{subpath}"
    )


def _container_submodule_paths(
    incus: Incus, container: str, repo_dir: str, *, uid: int
) -> list[str]:
    try:
        out = incus.exec(
            container,
            ["git", "-C", repo_dir, "submodule", "status", "--recursive"],
            uid=uid,
        )
    except IncusError:
        return []
    paths: list[str] = []
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            paths.append(parts[1])
    return paths


def _host_subrepo_exists(repo_root: Path, subpath: str) -> bool:
    return (repo_root / subpath / ".git").exists()


def _container_submodule_url(
    incus: Incus, container: str, repo_dir: str, subpath: str, *, uid: int
) -> str | None:
    """URL recorded for `subpath` in the container's `.gitmodules`, or None.

    `subpath` is top-relative, so a nested submodule's entry lives in its
    *parent* level's `.gitmodules`, keyed by the leaf path.
    """
    parent, _, leaf = subpath.rpartition("/")
    level_dir = f"{repo_dir}/{parent}" if parent else repo_dir
    for name, path in _list_gitmodules(incus, container, level_dir, uid=uid):
        if path != leaf:
            continue
        try:
            out = incus.exec(
                container,
                [
                    "git",
                    "config",
                    "-f",
                    f"{level_dir}/.gitmodules",
                    "--get",
                    f"submodule.{name}.url",
                ],
                uid=uid,
            )
        except IncusError:
            return None
        return out.strip() or None
    return None


def _repoint_cloned_subrepo(
    incus: Incus, container: str, repo_dir: str, subpath: str, host_sub: Path, *, uid: int
) -> None:
    """Give a just-cloned host sub-repo the upstream its `.gitmodules` names.

    `git clone` over `ext::` leaves origin pointing at `incus exec … container`:
    a remote that pushes into the container and breaks once it is destroyed.
    Failure here is cosmetic — the objects are already across — so it warns
    instead of failing the pull.
    """
    url = _container_submodule_url(incus, container, repo_dir, subpath, uid=uid)
    if not url:
        return
    try:
        git.set_origin_url(host_sub, url)
    except git.GitError as exc:
        _warn(f"submodule '{subpath}': could not set origin to '{url}' ({exc})")


def transport_submodules_to_host(
    cfg: Config,
    incus: Incus,
    container: str,
    short: str,
    *,
    repo_dir: str,
    only: str | None = None,
) -> None:
    """Fetch each container submodule's objects into the matching host sub-repo.

    Enumerates submodule paths on the container (sender). For a brand-new
    submodule the host has no repo for — one added inside the container —
    clones it from the container over ext:: so the later host-side
    `submodule update` can check it out, then repoints the clone's origin at
    the upstream `.gitmodules` names instead of the ext:: URL it was cloned
    from. An *existing* host sub-repo is only fetched into: its remotes are
    the user's own and are left alone.

    `only` restricts the transport to one submodule path (used by
    `jailbee submodule pr`, which publishes exactly one sub-repo).

    Postcondition, uniform across both branches below: every transported
    submodule has `refs/jailbee-sub/<short>/<path>/HEAD` and
    `.../heads/*` in its host sub-repo. A freshly cloned sub-repo is fetched
    into as well — `git clone` over ext:: leaves those refs absent, and callers
    that publish from them (`jailbee submodule pr`) would otherwise find nothing
    to push for a submodule the host had never seen.
    """
    uid = cfg.container_user.uid
    repo_root = Path(cfg.repo_root)
    for path in _container_submodule_paths(incus, container, repo_dir, uid=uid):
        if only is not None and path != only:
            continue
        url = _sub_upload_pack_url(cfg, container, repo_dir, path)
        host_sub = repo_root / path
        if not _host_subrepo_exists(repo_root, path):
            host_sub.parent.mkdir(parents=True, exist_ok=True)
            git.clone_url(url, host_sub)
            _repoint_cloned_subrepo(incus, container, repo_dir, path, host_sub, uid=uid)
        git.fetch_url_multi(
            host_sub,
            url,
            [
                f"+HEAD:refs/jailbee-sub/{short}/{path}/HEAD",
                f"+refs/heads/*:refs/jailbee-sub/{short}/{path}/heads/*",
            ],
        )


def _container_subrepo_exists(
    incus: Incus, container: str, repo_dir: str, subpath: str, *, uid: int
) -> bool:
    """Run `test -e <repo_dir>/<subpath>/.git` in the container. False on missing."""
    try:
        incus.exec(container, ["test", "-e", f"{repo_dir}/{subpath}/.git"], uid=uid)
    except IncusError:
        return False
    return True


def _submodule_upstream_url(host_sub: Path) -> str | None:
    """The upstream URL of the host sub-repo at `host_sub`, or None.

    Resolved against the submodule's *own* directory rather than inherited from
    `cfg.upstream_remote`: a submodule is a separate repository and may well
    name its upstream something the superproject does not.
    """
    remote = git.detect_upstream_remote(host_sub) or git.DEFAULT_REMOTE
    return git.get_remote_url(host_sub, remote)


def _create_container_subrepo(
    incus: Incus,
    container: str,
    sub_dir: str,
    *,
    origin_url: str | None,
    uid: int,
    gid: int,
) -> None:
    """`git init` an empty repo at `sub_dir` in the container, with `origin` set.

    `git init` creates leading directories, so a nested path needs no mkdir.
    The origin is the host sub-repo's own upstream — `git init` leaves none,
    and without it git inside the container cannot fetch or push that
    submodule. This is the same upstream `init_submodules_in_container`
    arrives at via `submodule sync`.
    """
    incus.exec(container, ["git", "init", "-q", sub_dir], uid=uid, gid=gid)
    if origin_url:
        incus.exec(
            container,
            ["git", "-C", sub_dir, "remote", "add", "origin", origin_url],
            uid=uid,
            gid=gid,
        )


def transport_submodules_to_container(
    cfg: Config, incus: Incus, container: str, *, repo_dir: str
) -> None:
    """Push each host submodule's objects into the matching container sub-repo.

    Enumerates submodule paths on the host (sender). For a submodule added on
    the host the container has no repo for, creates one first (mirror of
    `transport_submodules_to_host`'s clone-from-container fallback) and leaves
    it on the pushed tip: `git receive-pack` needs the repo to exist, and the
    later `submodule update --init` needs a current revision in it — an unborn
    HEAD fails with "Unable to find current revision in submodule path".

    An *existing* container sub-repo is only pushed into: it may hold the
    user's own in-container work, so its HEAD and working tree stay untouched.
    The container-side `submodule update` is the verify gate.
    """
    repo_root = Path(cfg.repo_root)
    uid = cfg.container_user.uid
    for path in git.submodule_status_paths(repo_root):
        url = _sub_receive_pack_url(cfg, container, repo_dir, path)
        sub_dir = f"{repo_dir}/{path}"
        created = not _container_subrepo_exists(incus, container, repo_dir, path, uid=uid)
        if created:
            _create_container_subrepo(
                incus,
                container,
                sub_dir,
                origin_url=_submodule_upstream_url(repo_root / path),
                uid=uid,
                gid=cfg.container_user.gid,
            )
        git.push_url_multi(
            repo_root / path,
            url,
            [
                f"+HEAD:refs/jailbee-sub/host/{path}/HEAD",
                f"+refs/heads/*:refs/jailbee-sub/host/{path}/heads/*",
            ],
        )
        if created:
            incus.exec(
                container,
                [
                    "git",
                    "-C",
                    sub_dir,
                    "checkout",
                    "--detach",
                    f"refs/jailbee-sub/host/{path}/HEAD",
                ],
                uid=uid,
                gid=cfg.container_user.gid,
            )


def prune_host_submodule_refs(cfg: Config, short: str) -> None:
    """Delete `refs/jailbee-sub/<short>/*` from each host submodule repo. Best-effort."""
    repo_root = Path(cfg.repo_root)
    for path in git.submodule_status_paths(repo_root):
        host_sub = repo_root / path
        for ref in git.list_refs(host_sub, f"refs/jailbee-sub/{short}/"):
            git.delete_ref(host_sub, ref)
