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

import posixpath
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from jailbee import git
from jailbee.incus import IncusError

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

# (cwd, git-args-without-"git") -> (exit_ok, stdout). Lets the placement
# routine run identically against the host (subprocess) and a container
# (incus.exec).
GitRun = Callable[[str, list[str]], tuple[bool, str]]


@dataclass(frozen=True)
class DeclaredSubmodule:
    """One normalized host declaration, including its exact declaring repo."""

    path: str
    url: str
    parent_path: str = "."


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


def _place_one(run: GitRun, sub: str, branch: str, gitlink: str) -> None:
    """Best-effort: put submodule `sub` on `branch` near the superproject's `gitlink`.

    `gitlink` comes from the superproject's tree, never from the submodule's
    own HEAD: `git submodule update` may have skipped the submodule (e.g.
    `submodule.<name>.update = none`), leaving HEAD on an unrelated commit.

    Guards: refuse on a dirty working tree, and leave the submodule alone when
    the gitlink commit is not in its object store. For the gitlink-vs-branch
    relationship:

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
    sha = gitlink
    ok, _ = run(sub, ["cat-file", "-e", f"{sha}^{{commit}}"])
    if not ok:
        _warn(
            f"submodule '{sub}': superproject gitlink {sha[:7]} is not in the "
            f"submodule — left as is. Run 'git submodule update' there."
        )
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


def _gitmodules_paths_at(
    run: GitRun, repo_dir: str, commit: str, *, strict: bool = False
) -> list[tuple[str, str]]:
    """`.gitmodules` entries as recorded in `commit` -> [(name, path)].

    The working-tree twin of this is `_gitmodules_paths`. This one reads the
    blob out of the object store instead, because the no-checkout placement
    path runs while the working tree is on a different branch — which may not
    have the same submodules, or any.
    """
    if strict:
        ok, tree = run(repo_dir, ["ls-tree", commit, "--", ".gitmodules"])
        if not ok:
            raise SubmoduleError(
                f"Cannot inspect declaration tree in commit '{commit}' in '{repo_dir}': {tree.strip()}"
            )
        if not tree.strip():
            return []
        # A no-match exit is normal for an empty config, but not for an
        # unreadable or malformed existing blob. Validate it separately.
        ok, parsed = run(repo_dir, ["config", "--blob", f"{commit}:.gitmodules", "--list"])
        if not ok:
            raise SubmoduleError(
                f"Cannot inspect .gitmodules in commit '{commit}' in '{repo_dir}': {parsed.strip()}"
            )
        strict_result = []
        for line in parsed.splitlines():
            key, _, path = line.partition("=")
            if key.startswith("submodule.") and key.endswith(".path") and path:
                strict_result.append((key[len("submodule.") : -len(".path")], path))
        return strict_result
    ok, out = run(
        repo_dir,
        [
            "config",
            "--blob",
            f"{commit}:.gitmodules",
            "--get-regexp",
            r"^submodule\..*\.path$",
        ],
    )
    if not ok:
        return []
    result: list[tuple[str, str]] = []
    for line in out.splitlines():
        key, _, path = line.strip().partition(" ")
        if key.startswith("submodule.") and key.endswith(".path") and path:
            result.append((key[len("submodule.") : -len(".path")], path))
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
        gitlink = _gitlink_at(run, top_dir, "HEAD", path)
        if gitlink is not None:
            if branch is not None:
                _place_one(run, sub, branch, gitlink)
            else:
                declared = _gitmodules_branch(run, top_dir, name)
                if declared and declared != ".":
                    _place_one(run, sub, declared, gitlink)
        _place_submodule_branches(run, sub, branch)


def _gitlink_at(
    run: GitRun, repo_dir: str, commit: str, path: str, *, strict: bool = False
) -> str | None:
    """Return the submodule (gitlink) commit recorded for ``path`` in tree
    ``commit``. ``commit`` is resolved in ``repo_dir``'s object store. None when
    ``path`` is not a gitlink there, or the lookup fails.
    """
    ok, out = run(repo_dir, ["ls-tree", commit, "--", path])
    if not ok:
        if strict:
            raise SubmoduleError(
                f"Cannot inspect gitlink '{path}' in commit '{commit}' in '{repo_dir}': {out.strip()}"
            )
        return None
    # "<mode> <type> <sha>\t<path>" -> ["160000", "commit", "<sha>", "<path>"]
    parts = out.split()
    if len(parts) >= 3 and parts[0] == "160000" and parts[1] == "commit":
        return parts[2]
    return None


PlacementStatus = git.PlaceStatus | Literal["unreachable"]
"""What happened to one submodule's `refs/heads/<branch>`.

`git.PlaceStatus`'s seven outcomes plus the one only a submodule can have:
`"unreachable"`, a sub-repo that is not on disk at all. Composed rather than
restated, so the vocabularies cannot drift apart.

`"checked-out"` is reachable here and matters: host submodules are *not*
always detached — `update_submodules_on_host` deliberately puts them on
`<branch>` — so a sub-repo already sitting on the target branch is a normal
state, and one this module reports rather than writes.
"""


@dataclass(frozen=True)
class SubBranchPlacement:
    """What happened to one submodule's `refs/heads/<branch>`."""

    path: str  # top-relative, e.g. "mid/inner"
    status: PlacementStatus
    old_oid: str | None
    new_oid: str


def _gitmodules_url_at(run: GitRun, repo_dir: str, commit: str, name: str) -> str | None:
    ok, out = run(
        repo_dir,
        ["config", "--blob", f"{commit}:.gitmodules", "--get", f"submodule.{name}.url"],
    )
    return (out.strip() or None) if ok else None


@dataclass(frozen=True)
class CommitSubmodule:
    """One submodule as a superproject commit records it, located on the host."""

    path: str
    name: str
    gitlink: str | None
    url: str | None
    repo: HostSubRepo | None


def host_submodules_at(
    repo_root: Path, commit: str, *, strict: bool = False
) -> list[CommitSubmodule]:
    """Every submodule `commit` records, recursively, parents before children.

    Reads `.gitmodules` and gitlinks from the commit's trees, never from the
    working tree — the host may be on a branch without these submodules.
    A child level is read from the parent's gitdir at the parent's gitlink,
    so it is only descended into when that sub-repo is on the host.
    `strict` rejects unreadable commits, declarations and trees for push;
    placement callers retain their best-effort enumeration contract.
    """
    out: list[CommitSubmodule] = []

    def walk(level: HostSubRepo, at: str) -> None:
        root = str(level.root)
        if strict and not git.has_commit(level.root, at):
            raise SubmoduleError(
                f"Submodule '{level.path or '.'}' commit '{at}' is not on the host. Fetch it first."
            )
        declarations = (
            _gitmodules_paths_at(git.run_capture, root, at, strict=True)
            if strict else _gitmodules_paths_at(git.run_capture, root, at)
        )
        for name, leaf in declarations:
            sha = (
                _gitlink_at(git.run_capture, root, at, leaf, strict=True)
                if strict else _gitlink_at(git.run_capture, root, at, leaf)
            )
            repo = locate_host_subrepo(level, leaf, name)
            path = posixpath.join(level.path, leaf) if level.path else leaf
            url = _gitmodules_url_at(git.run_capture, root, at, name)
            out.append(CommitSubmodule(path, name, sha, url, repo))
            if repo is not None and sha is not None:
                walk(repo, sha)

    walk(superproject_subrepo(repo_root), commit)
    return out


def _detach_worktreeless_head(repo: HostSubRepo) -> None:
    """Detach a gitdir-only sub-repo's HEAD at its current commit.

    With no worktree there is no index or tree a moved branch could desync,
    but `git.place_branch` still refuses to move the branch HEAD names.
    Detaching first lets placement proceed; the next `submodule update`
    checks out the gitlink detached anyway.
    """
    root = str(repo.root)
    on_branch, _ = git.run_capture(root, ["symbolic-ref", "-q", "HEAD"])
    if not on_branch:
        return
    ok, sha = git.run_capture(root, ["rev-parse", "--verify", "-q", "HEAD"])
    if ok and sha.strip():
        git.run_capture(root, ["update-ref", "--no-deref", "HEAD", sha.strip()])


def place_branches_from_commit(
    repo_root: Path, commit: str, branch: str, *, force: bool = False
) -> list[SubBranchPlacement]:
    """Point every submodule's `refs/heads/<branch>` at the gitlink `commit` records.

    Ref writes only for checked-out sub-repos: their HEAD, index and working
    tree are untouched. A gitdir-only sub-repo's symbolic HEAD is detached at
    its current commit before moving the branch it named. The guarantee is
    about **both** repos, and each half is load-bearing:

    * *The superproject* need not be checked out on `branch`, or on anything —
      the gitlinks come from `commit`'s trees (`_gitlink_at`) and `.gitmodules`
      from `commit`'s blob (`_gitmodules_paths_at`), never from the working
      tree. That is the whole reason this exists:
      `update_submodules_on_host` needs the superproject checked out and reads
      each submodule's *current HEAD* as the gitlink, and both assumptions are
      exactly what the no-checkout caller (a host branch placed from a
      container fetch, without moving the working tree) lacks.
    * *A sub-repo* keeps its own HEAD, index and working tree. Host submodules
      are frequently sitting on a branch rather than detached —
      `update_submodules_on_host` puts them there — so a sub-repo already
      checked out on `branch` is a normal state, and `git.place_branch`
      refuses to move the ref under it: the outcome is reported as
      `"checked-out"`, not written and not merged. Advancing it would need a
      checkout's worth of index and tree work, which this function is defined
      not to do.

    Fast-forward otherwise; `force` overwrites a diverged branch (but never a
    checked-out one). A sub-repo that is not there on disk is reported as
    `"unreachable"` rather than skipped — a silent skip is what made an
    earlier nested-submodule report misdescribe a missing checkout as
    "detached".
    """
    out: list[SubBranchPlacement] = []
    for sub in host_submodules_at(Path(repo_root), commit):
        if sub.gitlink is None:
            out.append(SubBranchPlacement(sub.path, "failed", None, ""))
            continue
        if sub.repo is None:
            out.append(SubBranchPlacement(sub.path, "unreachable", None, sub.gitlink))
            continue
        if sub.repo.worktree is None:
            _detach_worktreeless_head(sub.repo)
        out.append(_place_ref(sub.repo.root, sub.path, branch, sub.gitlink, force=force))
    return out


def _place_ref(
    repo_dir: Path, display: str, branch: str, new_oid: str, *, force: bool
) -> SubBranchPlacement:
    """One fast-forward-or-report ref write, labelled with the submodule's path.

    The ladder itself lives in `git.place_branch` — it is identical for the
    superproject (`sync._place_host_branch`) and every submodule, and one
    drifted copy of "may I move this ref?" loses commits. All this adds is the
    display path the report is keyed by.
    """
    status, old_oid = git.place_branch(repo_dir, branch, new_oid, force=force)
    return SubBranchPlacement(display, status, old_oid, new_oid)


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
    incus: Incus, container: str, level_dir: str, *, uid: int, at_head: bool = False
) -> list[tuple[str, str]]:
    """Parse one level's `.gitmodules` -> list of (name, path).

    `level_dir` is the working tree of the level being inspected
    (`repo_dir` at the top, `repo_dir/<path>` when recursing). Returns []
    when there is no `.gitmodules` (the `git config` call exits non-zero,
    which surfaces as `IncusError`).

    `at_head` reads the committed blob rather than an edited worktree file.
    Reads pass only `uid` (no `gid`) since they don't write group-owned files.
    """
    try:
        out = incus.exec(
            container,
            [
                *(
                    ["git", "-C", level_dir, "config", "--blob", "HEAD:.gitmodules"]
                    if at_head
                    else ["git", "config", "-f", f"{level_dir}/.gitmodules"]
                ),
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
                # Overrides `submodule.<name>.update = none`, which `--init`
                # alone skips — leaving an empty directory, and nothing for
                # push/pull to transport.
                "--checkout",
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
    """`git submodule update --init --checkout --recursive` inside the container.

    Acts as the apply-and-verify gate after a container-side merge/rebase:
    a missing object makes git fail, surfaced here as `SubmoduleError`.
    `--checkout` overrides `submodule.<name>.update = none`, as on the host
    (`git.submodule_update`).
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
                "--checkout",
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
    """`git submodule update --init --checkout --recursive` on the host working tree, then
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
    # Absorb before placement, not after: a freshly cloned sub-repo's git dir
    # is migrated into `.git/modules/<name>` here, and branch placement should
    # operate on the final layout. Running it from this function rather than
    # from `transport_submodules_to_host` also heals sub-repos cloned by
    # earlier versions on the next pull, without anyone having to notice.
    git.submodule_absorb_gitdirs(repo_root)
    _place_submodule_branches(git.run_capture, str(repo_root), branch)


class WorktreeFs(Protocol):
    """Non-git worktree operations, on the host or in a container."""

    def remove_file(self, path: str) -> None: ...
    def remove_empty_dirs(self, path: str) -> None: ...
    def list_files(self, path: str) -> list[str]: ...


class HostFs:
    def remove_file(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)

    def remove_empty_dirs(self, path: str) -> None:
        root = Path(path)
        if root.is_symlink() or not root.exists():
            return
        for directory, _dirs, _files in root.walk(top_down=False, on_error=_raise_fs_error):
            if not any(directory.iterdir()):
                directory.rmdir()

    def list_files(self, path: str) -> list[str]:
        root = Path(path)
        if root.is_symlink():
            return [str(root)]
        if not root.exists():
            return []
        return sorted(
            str(directory / name)
            for directory, _dirs, files in root.walk(on_error=_raise_fs_error)
            for name in files
        )


def _raise_fs_error(exc: OSError) -> None:
    raise exc


class ContainerFs:
    def __init__(self, incus: Incus, container: str, *, uid: int) -> None:
        self._incus = incus
        self._container = container
        self._uid = uid

    def _exec(self, cmd: list[str]) -> str:
        return self._incus.exec(self._container, cmd, uid=self._uid)

    def remove_file(self, path: str) -> None:
        self._exec(["rm", "-f", "--", path])

    def remove_empty_dirs(self, path: str) -> None:
        self._exec(["find", path, "-depth", "-type", "d", "-empty", "-delete"])

    def list_files(self, path: str) -> list[str]:
        # find does not follow symlinks; a missing root alone is benign.
        out = self._exec(
            [
                "sh",
                "-c",
                'if [ -e "$1" ] || [ -L "$1" ]; then find "$1" ! -type d; fi',
                "sh",
                path,
            ]
        )
        return sorted(line for line in out.splitlines() if line)


def _require_git(run: GitRun, cwd: str, args: list[str]) -> str:
    ok, out = run(cwd, args)
    if not ok:
        raise SubmoduleError(
            f"Cannot run git {args[0]} in '{cwd}': {out.strip() or 'command failed'}. "
            "Check repository objects, locks and permissions before retrying."
        )
    return out


def gitlinks_at(run: GitRun, top_dir: str, commit: str) -> set[str]:
    """This level's gitlinks from a commit; unreadable trees are never empty."""
    out = _require_git(run, top_dir, ["ls-tree", "-r", commit])
    found: set[str] = set()
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        if meta.split()[:2] == ["160000", "commit"]:
            found.add(_normalize_declared_path(path))
    return found


def vanishing_submodules(
    run: GitRun, top_dir: str, *, head: str, target: str, base: str | None = None
) -> list[str]:
    """Gitlinks removed by checkout, or deleted by the other side of a merge."""
    in_head = gitlinks_at(run, top_dir, head)
    in_target = gitlinks_at(run, top_dir, target)
    candidates = in_head if base is None else in_head & gitlinks_at(run, top_dir, base)
    return sorted(candidates - in_target)


def _has_own_worktree(run: GitRun, sub: str) -> bool:
    ok, top = run(sub, ["rev-parse", "--show-toplevel"])
    return ok and top.strip() == sub


def _assert_clean_worktree(
    run: GitRun, sub: str, label: str, *, display: str | None = None
) -> None:
    out = _require_git(
        run, sub, ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"]
    )
    if out.strip():
        raise SubmoduleError(
            f"Cannot switch to '{label}': submodule '{display or sub}' has local changes. "
            "Commit, stash or remove them first."
        )


def _external_gitdir(run: GitRun, sub: str) -> str:
    gitdir = _require_git(run, sub, ["rev-parse", "--absolute-git-dir"]).strip()
    if not gitdir or gitdir == sub or gitdir.startswith(f"{sub}/"):
        raise SubmoduleError(
            f"Submodule '{sub}' keeps its git directory inside the working tree. "
            "Run 'git submodule absorbgitdirs' before switching and retry."
        )
    return gitdir


def _owned_prune_tree(
    run: GitRun, parent: str, path: str, *, before_move: bool
) -> bool:
    """Validate the whole ownership chain before absorption or any tree writes."""
    sub = f"{parent}/{path}"
    if not _has_own_worktree(run, sub):
        _warn(f"Submodule '{sub}': no own worktree; left in place")
        return False
    gitdir = _require_git(run, sub, ["rev-parse", "--absolute-git-dir"]).strip()
    parent_gitdir = _require_git(run, parent, ["rev-parse", "--absolute-git-dir"]).strip()
    modules = f"{parent_gitdir}/modules/"
    declared = dict((leaf, name) for name, leaf in _gitmodules_paths_at(run, parent, "HEAD"))
    name = declared.get(path)
    legacy = gitdir == f"{sub}/.git"
    if legacy:
        if not before_move:
            _external_gitdir(run, sub)
        # Legacy directories may only be absorbed while their declaration exists.
        owned = before_move and name is not None and path in gitlinks_at(run, parent, "HEAD")
    else:
        owned = gitdir.startswith(modules)
        if name is not None:
            owned = owned and gitdir == posixpath.normpath(f"{modules}{name}")
        if owned:
            worktree = _require_git(
                run, sub, ["config", "--file", f"{gitdir}/config", "--get", "core.worktree"]
            ).strip()
            associated = posixpath.normpath(posixpath.join(gitdir, worktree))
            owned = bool(worktree) and associated == sub
    if not owned:
        _warn(f"Submodule '{sub}': foreign or ambiguous git directory; left in place")
        return False
    for nested in sorted(gitlinks_at(run, sub, "HEAD")):
        if before_move and not _has_own_worktree(run, f"{sub}/{nested}"):
            # Uninitialized children have nothing for absorbgitdirs to move.
            continue
        if not _owned_prune_tree(run, sub, nested, before_move=before_move):
            return False
    return True


def assert_vanishing_clean(
    run: GitRun, top_dir: str, paths: list[str], *, target_label: str
) -> None:
    """Refuse dirty removals and absorb legacy gitdirs before the HEAD move."""
    for path in paths:
        path = _normalize_declared_path(path)
        sub = f"{top_dir}/{path}"
        if not _has_own_worktree(run, sub):
            _warn(f"Submodule '{path}': no own worktree; left in place")
            continue
        if not _owned_prune_tree(run, top_dir, path, before_move=True):
            continue
        _assert_clean_worktree(run, sub, target_label, display=path)
        # Git absorbs nested legacy gitdirs recursively while declarations exist.
        _require_git(run, top_dir, ["submodule", "absorbgitdirs", "--", path])
        _check_prune_tree(run, sub, target_label)


def _check_prune_tree(run: GitRun, sub: str, label: str) -> None:
    _external_gitdir(run, sub)
    _assert_clean_worktree(run, sub, label)
    for nested in sorted(gitlinks_at(run, sub, "HEAD")):
        child = f"{sub}/{nested}"
        if _has_own_worktree(run, child):
            _check_prune_tree(run, child, label)


def _prune_one(run: GitRun, sub: str, fs: WorktreeFs) -> None:
    gitdir = _external_gitdir(run, sub)
    for nested in sorted(gitlinks_at(run, sub, "HEAD")):
        child = f"{sub}/{nested}"
        if not _has_own_worktree(run, child):
            raise SubmoduleError(f"Submodule '{child}': no own worktree; parent left in place")
        _prune_one(run, child, fs)
    _require_git(run, sub, ["read-tree", "-u", "--reset", git.EMPTY_TREE])
    _require_git(run, sub, ["config", "--file", f"{gitdir}/config", "--unset", "core.worktree"])
    fs.remove_file(f"{sub}/.git")
    fs.remove_empty_dirs(sub)


def prune_vanished_worktrees(
    run: GitRun, top_dir: str, paths: list[str], *, fs: WorktreeFs, target_label: str
) -> dict[str, list[str]]:
    """Prune tracked files only, preserving repositories and ignored leftovers.

    A nested failure aborts its parent cleanup. Missing or foreign worktrees
    are warned and preserved; unreadable trees/status and write failures raise.
    """
    if _require_git(run, top_dir, ["ls-files", "-u"]).strip():
        _warn(f"Cannot prune submodules on '{target_label}': resolve merge conflicts first")
        return {}
    still = gitlinks_at(run, top_dir, "HEAD")
    kept: dict[str, list[str]] = {}
    for path in paths:
        path = _normalize_declared_path(path)
        sub = f"{top_dir}/{path}"
        if path in still:
            continue
        if not _has_own_worktree(run, sub):
            _warn(f"Submodule '{path}': no own worktree; left in place")
            continue
        if not _owned_prune_tree(run, top_dir, path, before_move=False):
            continue
        _check_prune_tree(run, sub, target_label)
        _prune_one(run, sub, fs)
        leftovers = fs.list_files(sub)
        if leftovers:
            kept[path] = leftovers
            _warn(
                f"Submodule '{path}' is not on '{target_label}'; its ignored files were kept:\n"
                + "\n".join(f"  {file}" for file in leftovers)
            )
    return kept


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


@dataclass(frozen=True)
class HostSubRepo:
    """A host repo found by its gitdir; the worktree is optional.

    `root` is the directory every `git.py` helper is run in: the worktree
    when it is checked out, else the gitdir itself. Git accepts a gitdir as
    the cwd for ref and object commands as long as `core.worktree` does not
    name a missing directory — `locate_host_subrepo` guarantees that.
    """

    path: str
    gitdir: Path
    worktree: Path | None

    @property
    def root(self) -> Path:
        return self.worktree if self.worktree is not None else self.gitdir


def _gitdir_of(worktree: Path) -> Path | None:
    """The gitdir a worktree's `.git` names (a `gitdir:` file or legacy dir)."""
    dot_git = worktree / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        text = dot_git.read_text().strip()
        if text.startswith("gitdir:"):
            return (worktree / text[len("gitdir:") :].strip()).resolve()
    return None


def superproject_subrepo(repo_root: Path) -> HostSubRepo:
    """The superproject itself, as the root level of a locator walk."""
    gitdir = _gitdir_of(repo_root) or repo_root / ".git"
    return HostSubRepo(path="", gitdir=gitdir, worktree=repo_root)


def locate_host_subrepo(parent: HostSubRepo, leaf: str, name: str) -> HostSubRepo | None:
    """Find submodule `leaf` relative to `parent`, using its declared `name`."""
    path = posixpath.join(parent.path, leaf) if parent.path else leaf
    if parent.worktree is not None:
        worktree = parent.worktree / leaf
        gitdir = _gitdir_of(worktree)
        if gitdir is not None:
            return HostSubRepo(path=path, gitdir=gitdir, worktree=worktree)
    candidate = parent.gitdir / "modules" / name
    if not (candidate / "HEAD").is_file():
        return None
    has_worktree, _ = git.run_capture(
        str(parent.gitdir),
        ["config", "--file", str(candidate / "config"), "--get", "core.worktree"],
    )
    if has_worktree:
        repaired, _ = git.run_capture(
            str(parent.gitdir),
            ["config", "--file", str(candidate / "config"), "--unset", "core.worktree"],
        )
        still_has_worktree, _ = git.run_capture(
            str(parent.gitdir),
            ["config", "--file", str(candidate / "config"), "--get", "core.worktree"],
        )
        if not repaired or still_has_worktree:
            raise SubmoduleError(
                f"Cannot repair stale core.worktree in submodule gitdir '{candidate}'; "
                "check config.lock and permissions"
            )
    return HostSubRepo(path=path, gitdir=candidate, worktree=None)


def _locate_default_nested(
    parent: HostSubRepo, remaining: str, selected_commit: str
) -> HostSubRepo | None:
    """Resolve default-name modules without splitting declared slash paths blindly."""
    direct = locate_host_subrepo(parent, remaining, remaining)
    if direct is not None:
        return direct
    if not remaining:
        return None
    parts = remaining.split("/")
    for index in range(1, len(parts)):
        prefix = "/".join(parts[:index])
        child = locate_host_subrepo(parent, prefix, prefix)
        if child is None:
            continue
        oid = _gitlink_at(git.run_capture, str(parent.root), selected_commit, prefix)
        # A branch without this gitlink has no selected child tree; the local
        # gitdir's HEAD is the only available declaration source for fallback.
        child_commit = oid or "HEAD"
        resolved = _resolve_host_subrepo(child, "/".join(parts[index:]), child_commit)
        if resolved is not None:
            return resolved
    return None


def _resolve_host_subrepo(level: HostSubRepo, remaining: str, commit: str) -> HostSubRepo | None:
    object_ok, _ = git.run_capture(
        str(level.root), ["rev-parse", "--verify", f"{commit}^{{commit}}"]
    )
    if not object_ok and commit != "HEAD":
        raise SubmoduleError(
            f"Cannot inspect submodule tree '{commit}' in '{level.root}'; "
            "the referenced commit object is unavailable"
        )
    entries = _gitmodules_paths_at(git.run_capture, str(level.root), commit)
    for name, path in entries:
        if path == remaining:
            return locate_host_subrepo(level, path, name)
        if remaining.startswith(f"{path}/"):
            child = locate_host_subrepo(level, path, name)
            if child is None:
                return None
            gitlink = _gitlink_at(git.run_capture, str(level.root), commit, path)
            if gitlink is None:
                raise SubmoduleError(
                    f"Cannot resolve gitlink '{path}' from commit '{commit}' in '{level.root}'"
                )
            return _resolve_host_subrepo(child, remaining[len(path) + 1 :], gitlink)
    return _locate_default_nested(level, remaining, commit)


def resolve_host_subrepo(repo_root: Path, subpath: str, *, at: str = "HEAD") -> HostSubRepo | None:
    """Locate top-relative `subpath` by declarations and gitlinks in commit trees."""
    return _resolve_host_subrepo(superproject_subrepo(repo_root), subpath, at)


def host_subrepo_root(repo_root: Path, subpath: str) -> Path:
    """Located host sub-repo root, or the path for a missing sub-repo."""
    sub = resolve_host_subrepo(repo_root, subpath)
    return sub.root if sub is not None else Path(repo_root) / subpath


def host_subrepo_exists(repo_root: Path, subpath: str) -> bool:
    """True when the host has `subpath`'s sub-repo, checked out or gitdir only."""
    return resolve_host_subrepo(repo_root, subpath) is not None


def _declared_submodules_at(repo_dir: Path) -> tuple[tuple[str, str], ...]:
    ok, output = git.run_capture(
        str(repo_dir),
        [
            "config",
            "-f",
            f"{repo_dir}/.gitmodules",
            "--get-regexp",
            r"^submodule\..*\.(path|url)$",
        ],
    )
    if not ok:
        config_path = repo_dir / ".gitmodules"
        try:
            config_path.stat()
        except FileNotFoundError:
            try:
                config_path.lstat()
            except FileNotFoundError:
                return ()
            except OSError as exc:
                raise SubmoduleError(
                    f"could not inspect submodule declarations at {config_path}"
                ) from exc
            raise SubmoduleError(
                f"could not read submodule declarations from {config_path}"
            ) from None
        except OSError as exc:
            raise SubmoduleError(
                f"could not inspect submodule declarations at {config_path}"
            ) from exc
        valid, _ = git.run_capture(str(repo_dir), ["config", "-f", str(config_path), "--list"])
        if valid:
            return ()
        raise SubmoduleError(f"could not read submodule declarations from {config_path}")

    values: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for line in output.splitlines():
        key, separator, value = line.strip().partition(" ")
        if not separator or not key.startswith("submodule."):
            raise SubmoduleError(f"malformed submodule declaration in {repo_dir / '.gitmodules'}")
        field = "path" if key.endswith(".path") else "url" if key.endswith(".url") else None
        if field is None:
            continue
        name = key[len("submodule.") : -len(field) - 1]
        if not name or not value:
            raise SubmoduleError(f"malformed submodule declaration in {repo_dir / '.gitmodules'}")
        if name not in values:
            values[name] = {}
            order.append(name)
        if field in values[name]:
            raise SubmoduleError(
                f"duplicate submodule {field} for '{name}' in {repo_dir / '.gitmodules'}"
            )
        values[name][field] = value

    result: list[tuple[str, str]] = []
    for name in order:
        path = values[name].get("path")
        url = values[name].get("url")
        if path is None:
            raise SubmoduleError(
                f"submodule '{name}' is missing path in {repo_dir / '.gitmodules'}"
            )
        if url is None:
            raise SubmoduleError(f"submodule '{name}' is missing URL in {repo_dir / '.gitmodules'}")
        result.append((path, url))
    return tuple(result)


def _normalize_declared_path(path: str) -> str:
    parts = path.split("/")
    normalized = posixpath.normpath(path)
    if (
        not path
        or path.startswith("/")
        or ".." in parts
        or normalized in {"", ".", ".."}
        or normalized.startswith("../")
    ):
        raise SubmoduleError(f"unsafe submodule path '{path}'")
    return normalized


def declared_submodule_remotes(repo_root: Path) -> tuple[DeclaredSubmodule, ...]:
    """Return the safe repository tree declared by host ``.gitmodules`` files.

    Uninitialized children remain allowed leaves. Recursion reads only working
    trees that are initialized host repositories, and canonical-directory
    tracking rejects symlink aliases or cycles before another file is read.
    """
    root = Path(repo_root)
    canonical_root = root.resolve()
    visited = {canonical_root}
    seen_paths: set[str] = set()
    result: list[DeclaredSubmodule] = []

    def walk(repo_dir: Path, prefix: str) -> None:
        for raw_path, url in _declared_submodules_at(repo_dir):
            relative = _normalize_declared_path(raw_path)
            path = posixpath.join(prefix, relative) if prefix else relative
            if path in seen_paths:
                raise SubmoduleError(f"duplicate submodule path '{path}'")
            seen_paths.add(path)
            result.append(DeclaredSubmodule(path, url, parent_path=prefix or "."))

            if not host_subrepo_exists(root, path):
                continue
            child = root / path
            canonical_child = child.resolve()
            try:
                canonical_child.relative_to(canonical_root)
            except ValueError as exc:
                raise SubmoduleError(
                    f"submodule path '{path}' resolves outside the host repository"
                ) from exc
            if canonical_child in visited:
                raise SubmoduleError(
                    f"submodule path '{path}' revisits host directory '{canonical_child}'"
                )
            visited.add(canonical_child)
            walk(child, path)

    walk(root, "")
    return tuple(result)


def host_submodule_paths(repo_root: Path) -> list[str]:
    """Top-relative initialized host submodule paths, recursively."""

    def run(directory: str, args: list[str]) -> tuple[bool, str]:
        return git.run_capture(directory, args)

    paths = (
        Path(directory).relative_to(repo_root).as_posix()
        for directory in _walk_submodule_dirs(run, str(repo_root))
    )
    return [path for path in paths if host_subrepo_exists(repo_root, path)]


def _container_submodule_name(
    incus: Incus, container: str, repo_dir: str, parent_path: str, leaf: str, *, uid: int
) -> str:
    """Resolve a leaf's declared name from the container parent's commit."""
    level_dir = f"{repo_dir}/{parent_path}" if parent_path else repo_dir
    for name, path in _list_gitmodules(incus, container, level_dir, uid=uid, at_head=True):
        if path == leaf:
            return name
    return leaf


def _container_submodule_url(
    incus: Incus,
    container: str,
    repo_dir: str,
    subpath: str,
    *,
    uid: int,
    parent_path: str | None = None,
    at_head: bool = False,
) -> str | None:
    """URL recorded for `subpath` in the container's `.gitmodules`, or None.

    `subpath` is top-relative, so a nested submodule's entry lives in its
    *parent* level's `.gitmodules`, keyed by the leaf path.
    """
    parent, _, leaf = subpath.rpartition("/")
    if parent_path is not None:
        parent = parent_path
        leaf = subpath[len(parent) + 1 :] if parent else subpath
    level_dir = f"{repo_dir}/{parent}" if parent else repo_dir
    for name, path in _list_gitmodules(incus, container, level_dir, uid=uid, at_head=at_head):
        if path != leaf:
            continue
        try:
            out = incus.exec(
                container,
                [
                    *(
                        ["git", "-C", level_dir, "config", "--blob", "HEAD:.gitmodules"]
                        if at_head
                        else ["git", "config", "-f", f"{level_dir}/.gitmodules"]
                    ),
                    "--get",
                    f"submodule.{name}.url",
                ],
                uid=uid,
            )
        except IncusError:
            return None
        return out.strip() or None
    return None


def _container_subrepo_origin_url(
    incus: Incus, container: str, repo_dir: str, subpath: str, *, uid: int
) -> str | None:
    """The `remote.origin.url` of the container's own sub-repo at `subpath`.

    The fallback for a submodule whose upstream was set on the sub-repo rather
    than declared in the superproject's `.gitmodules` — the shape a repository
    created inside the container tends to have. None when the sub-repo has no
    origin or the query fails.
    """
    try:
        out = incus.exec(
            container,
            ["git", "-C", f"{repo_dir}/{subpath}", "config", "--get", "remote.origin.url"],
            uid=uid,
        )
    except IncusError:
        return None
    return out.strip() or None


def _repoint_cloned_subrepo(
    incus: Incus,
    container: str,
    repo_dir: str,
    subpath: str,
    host_sub: Path,
    *,
    uid: int,
    parent_path: str | None = None,
    at_head: bool = False,
) -> None:
    """Give a freshly created gitdir-only host sub-repo a usable upstream.

    Initializing with the transport URL leaves origin pointing at the container:
    a remote that pushes into the container and breaks once it is destroyed.
    The upstream is looked for in order:

    1. the URL the container's `.gitmodules` declares for this path — the
       superproject's own answer, and the one `git submodule update --init`
       will register on the host regardless, so overriding it would only make
       two files disagree;
    2. the container sub-repo's own `remote.origin.url`, which is where a
       repository created inside the container carries its upstream;
    3. nothing — in which case `origin` is *removed*. An `ext::` remote left
       in place is a live footgun; an absent one only costs a benign
       `warning: could not look up configuration 'remote.origin.url'` from a
       later `submodule update --init`.

    A candidate that is itself an `ext::` URL is skipped at step 2: that is
    jailbee's own transport, never a real upstream.

    Failure here is cosmetic — the objects are already across — so every
    branch warns instead of failing the pull.
    """
    url = _container_submodule_url(
        incus, container, repo_dir, subpath, uid=uid, parent_path=parent_path, at_head=at_head
    )
    if not url:
        candidate = _container_subrepo_origin_url(incus, container, repo_dir, subpath, uid=uid)
        if candidate and not candidate.startswith("ext::"):
            url = candidate
    if url:
        try:
            git.set_origin_url(host_sub, url)
        except git.GitError as exc:
            _warn(f"submodule '{subpath}': could not set origin to '{url}' ({exc})")
        return
    try:
        git.remove_origin(host_sub)
    except git.GitError as exc:
        _warn(f"submodule '{subpath}': could not remove the container-local origin ({exc})")
        return
    _warn(
        f"submodule '{subpath}': no upstream the host can reach was found, so its "
        f"origin was removed. Set one with: "
        f"git -C {subpath} remote add origin <url>"
    )


def transport_submodules_to_host(
    cfg: Config,
    incus: Incus,
    container: str,
    short: str,
    *,
    repo_dir: str,
    only: str | None = None,
) -> None:
    """Fetch container submodules without materialising any host worktree.

    New repos live under their parent's gitdir/modules/<declared name> and
    receive the container HEAD before nested repos are processed. Existing
    repos keep their origin and HEAD. `only` also includes required ancestors,
    but never creates unrelated repos. All fetched repos receive the namespaced
    HEAD and branch refs used by later placement and publication.
    """
    uid = cfg.container_user.uid
    repo_root = Path(cfg.repo_root)
    located = {"": superproject_subrepo(repo_root)}
    for path in _container_submodule_paths(incus, container, repo_dir, uid=uid):
        if only is not None and path != only and not only.startswith(f"{path}/"):
            continue
        # Recursive status lists parents before children. Fetch parents first,
        # including the ancestors of an explicitly selected nested leaf.
        parent_path = max(
            (p for p in located if not p or path.startswith(f"{p}/")), key=len
        )
        parent = located[parent_path]
        leaf = path[len(parent_path) + 1 :] if parent_path else path
        name = _container_submodule_name(
            incus, container, repo_dir, parent_path, leaf, uid=uid
        )
        url = _sub_upload_pack_url(cfg, container, repo_dir, path)
        sub = locate_host_subrepo(parent, leaf, name)
        created = sub is None
        if sub is None:
            from jailbee.remote_ssh.session import is_remote_session

            if is_remote_session():
                _warn(
                    f"submodule '{path}' is new in the container; it is not brought "
                    f"onto the host over remote SSH — run this on the host to bring it in."
                )
                continue
            gitdir = parent.gitdir / "modules" / name
            git.init_subrepo_gitdir(gitdir, url)
            sub = HostSubRepo(path=path, gitdir=gitdir, worktree=None)
        located[path] = sub
        git.fetch_url_multi(
            sub.root,
            url,
            [
                f"+HEAD:refs/jailbee-sub/{short}/{path}/HEAD",
                f"+refs/heads/*:refs/jailbee-sub/{short}/{path}/heads/*",
            ],
        )
        if created:
            ok, error = git.run_capture(
                str(sub.root),
                ["update-ref", "--no-deref", "HEAD", f"refs/jailbee-sub/{short}/{path}/HEAD"],
            )
            if not ok:
                raise git.GitError(f"submodule '{path}': could not initialize HEAD ({error})")
            _repoint_cloned_subrepo(
                incus, container, repo_dir, path, sub.root, uid=uid,
                parent_path=parent_path, at_head=True,
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
    cfg: Config,
    incus: Incus,
    container: str,
    *,
    repo_dir: str,
    source_ns: str = "host",
    paths: list[str] | None = None,
    commit: str | None = None,
) -> None:
    """Push submodule objects into `container`'s matching sub-repos.

    `source_ns` names whose objects these are, and becomes the
    `refs/jailbee-sub/<source_ns>/<path>/...` namespace inside the target sub-repo.
    The default "host" enumerates the pushed commit's trees recursively and
    pushes its pinned gitlinks, not the host worktree's current HEADs.
    Every source repo and commit is validated before any target is created.

    A non-host `source_ns` relays another container's work instead. Those
    objects were fetched into the host sub-repos under
    `refs/jailbee-sub/<source_ns>/<path>/*` by `transport_submodules_to_host`
    and live in *neither* default refspec, so the refspecs become
    namespace-to-namespace copies of that ref tree. `paths` must then be the
    source container's submodule paths — the host working tree may not have
    the same ones, or any, so host enumeration is skipped whenever `paths` is
    given explicitly.

    For a submodule the target container has no repo for yet, creates one
    first (mirror of `transport_submodules_to_host`'s clone-from-container
    fallback) and checks out the pushed tip: `git receive-pack` needs the repo
    to exist, and the later `submodule update --init` needs a current
    revision in it — an unborn HEAD fails with "Unable to find current
    revision in submodule path". The checkout target is always the ref that
    was just pushed under this call's own `source_ns`, never a hardcoded
    `host` — a relay's target sub-repo only ever receives
    `refs/jailbee-sub/<source_ns>/...`, so checking out `.../host/...` there
    would reference a ref that was never pushed.

    An *existing* container sub-repo is only pushed into: it may hold the
    user's own in-container work, so its HEAD and working tree stay untouched.
    The container-side `submodule update` is the verify gate.
    """
    repo_root = Path(cfg.repo_root)
    uid = cfg.container_user.uid
    entries: list[tuple[str, Path, str | None, str | None]]
    if source_ns == "host":
        if commit is None:
            raise ValueError("transport_submodules_to_container: commit is required for 'host'")
        if not git.has_commit(repo_root, commit):
            raise SubmoduleError(f"Source commit '{commit}' is not on the host. Fetch it first.")
        try:
            subs = host_submodules_at(repo_root, commit, strict=True)
        except git.GitError as exc:
            raise SubmoduleError(f"Cannot inspect submodules in commit '{commit}': {exc}") from exc
        missing = [
            sub.path
            for sub in subs
            if sub.gitlink is not None
            and (sub.repo is None or not git.has_commit(sub.repo.root, sub.gitlink))
        ]
        if missing:
            raise SubmoduleError(
                "\n".join(
                    f"Submodule '{path}' commit (gitlink in {commit[:7]}) is not on the host. "
                    f"Fetch it first: git -C {repo_root} submodule update --init {path}"
                    for path in missing
                )
            )
        entries = [
            (sub.path, sub.repo.root, sub.gitlink, sub.url)
            for sub in subs
            if sub.repo is not None and sub.gitlink is not None
        ]
    else:
        entries = [(path, host_subrepo_root(repo_root, path), None, None) for path in (paths or [])]
    for path, host_root, gitlink, declared_url in entries:
        url = _sub_receive_pack_url(cfg, container, repo_dir, path)
        sub_dir = f"{repo_dir}/{path}"
        created = not _container_subrepo_exists(incus, container, repo_dir, path, uid=uid)
        if created:
            _create_container_subrepo(
                incus,
                container,
                sub_dir,
                origin_url=_submodule_upstream_url(host_root) or declared_url,
                uid=uid,
                gid=cfg.container_user.gid,
            )
        ns = f"refs/jailbee-sub/{source_ns}/{path}"
        head_ref = f"{ns}/HEAD"
        if source_ns == "host":
            refspecs = [
                f"+{gitlink}:{head_ref}",
                f"+refs/heads/*:{ns}/heads/*",
            ]
        else:
            refspecs = [f"+{ns}/HEAD:{head_ref}", f"+{ns}/heads/*:{ns}/heads/*"]
        git.push_url_multi(host_root, url, refspecs)
        if created:
            incus.exec(
                container,
                [
                    "git",
                    "-C",
                    sub_dir,
                    "checkout",
                    "--detach",
                    head_ref,
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
