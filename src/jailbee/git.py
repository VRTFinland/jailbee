"""Thin git CLI wrapper. Only this module calls `subprocess` for git ops.

Architecture rule: keep `subprocess` calls out of `config.py` so config
loading stays unit-testable. Mirrors the `incus.py` wrapper convention.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_FALLBACK_BRANCH = "main"
_ORIGIN_PREFIX = "origin/"


def detect_default_branch(repo_root: Path) -> str:
    """Return the upstream's default branch name.

    Runs `git symbolic-ref --short refs/remotes/origin/HEAD` in repo_root.
    Output looks like `origin/main`; we strip the `origin/` prefix.

    On any failure (no origin remote, command non-zero, missing git binary,
    unexpected output), returns "main".
    """
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return _FALLBACK_BRANCH

    if result.returncode != 0:
        return _FALLBACK_BRANCH

    out = result.stdout.strip()
    if not out.startswith(_ORIGIN_PREFIX):
        return _FALLBACK_BRANCH

    branch = out[len(_ORIGIN_PREFIX) :]
    return branch or _FALLBACK_BRANCH


def get_origin_url(repo_root: Path) -> str | None:
    """Return the URL of the host repo's `origin` remote, or None.

    Used by `jailbee new` to rewrite the in-container clone's origin from the
    RO mount path (`/mnt/host-source`) to the real upstream so
    `git push`/`fetch` reach GitHub when strict-mode allows it.

    Returns None if there's no origin remote, the command fails, or git
    isn't on PATH — callers fall back to leaving the mount path in place.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None

    if result.returncode != 0:
        return None

    url = result.stdout.strip()
    return url or None


def set_origin_url(repo_root: Path, url: str) -> None:
    """`git remote set-url origin <url>` in repo_root.

    Used after cloning a submodule out of a container over `ext::`: the clone's
    origin would otherwise point at `incus exec …`, a remote that pushes into
    the container and dies with it. Raises `GitError` when the command fails
    (e.g. no origin remote) — callers treat that as cosmetic.
    """
    result = subprocess.run(
        ["git", "remote", "set-url", "origin", url],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GitError(f"git remote set-url origin failed (exit {result.returncode})")


def get_branch_tracking(repo_root: Path, branch: str) -> tuple[str, str] | None:
    """Return (remote, merge_ref) tracking config for `branch`, or None.

    Reads `branch.<branch>.remote` and `branch.<branch>.merge` from the host
    repo's git config. Both must be present for tracking to be considered
    configured; if either is missing, returns None and the caller defaults
    to ("origin", "refs/heads/<branch>").
    """
    remote = _git_config_get(repo_root, f"branch.{branch}.remote")
    merge = _git_config_get(repo_root, f"branch.{branch}.merge")
    if remote is None or merge is None:
        return None
    return remote, merge


def _git_config_get(repo_root: Path, key: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", "--get", key],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None

    if result.returncode != 0:
        return None

    value = result.stdout.strip()
    return value or None


def get_current_branch(repo_root: Path) -> str | None:
    """Return the name of the currently checked-out branch in `repo_root`.

    Used by `jailbee new --current`. Returns None when:
      - HEAD is detached (`git symbolic-ref` exits non-zero), or
      - git is unavailable.

    Callers must surface a user-facing error for None; this helper stays
    purely informational so its tests don't need to assert error messages.
    """
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None

    if result.returncode != 0:
        return None

    branch = result.stdout.strip()
    return branch or None


def get_head_sha(repo_root: Path) -> str | None:
    """Return the full sha of ``repo_root``'s current HEAD, or None.

    Handed to the container-side git probe as ``HOST_HEAD`` so it can
    compute a diff against the host's *current* branch without a second
    round-trip. None when the repo has no commits yet, git is missing, or
    git failed — every one of those means "no LOCAL diff", not an error.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def has_commit(repo_root: Path, sha: str) -> bool:
    """True when ``sha`` names a commit object already present in ``repo_root``.

    Purely local — no fetch, no network, no ref written. Answers both
    "can the host compute the LOCAL diff itself" (git_status fallback) and
    "does the host already hold this container's work" (destroy guard).
    """
    if not sha:
        return False
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def diff_shortstat_between(repo_root: Path, base: str, tip: str) -> str | None:
    """``git diff --shortstat <base>...<tip>`` in ``repo_root``.

    Returns raw stdout — **empty string when there is no difference** — or
    None when the command could not run. Callers must keep the two apart:
    empty renders as ``clean``, None as ``?``.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--shortstat", "--ignore-submodules=dirty", f"{base}...{tip}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None

    if result.returncode != 0:
        return None
    return result.stdout


def count_commits_between(repo_root: Path, base: str, tip: str) -> str | None:
    """``git rev-list --count <base>..<tip>`` in ``repo_root``, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base}..{tip}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip()


class GitFetchError(RuntimeError):
    """`git fetch` returned non-zero or git itself is missing.

    Carries the captured stderr in `stderr` so callers can surface a
    user-actionable message (network issue, missing remote, ACL denied,
    etc.) without re-running the command.
    """

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


def fetch_origin_ref(repo_root: Path, branch: str) -> None:
    """Run ``git fetch origin <branch>`` in ``repo_root``.

    Raises `GitFetchError` on any failure (git missing, fetch non-zero).
    The remote-tracking ref ``refs/remotes/origin/<branch>`` is updated
    on success; the host's local ``refs/heads/<branch>`` is left
    untouched (no `:refs/heads/...` refspec).
    """
    try:
        result = subprocess.run(
            ["git", "fetch", "origin", branch],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as e:
        raise GitFetchError("git is not installed or not on PATH") from e
    if result.returncode != 0:
        raise GitFetchError(
            f"git fetch origin {branch} failed in {repo_root}",
            stderr=result.stderr,
        )


def rev_parse_origin(repo_root: Path, branch: str) -> str | None:
    """Resolve ``refs/remotes/origin/<branch>`` to a commit SHA in ``repo_root``.

    Returns ``None`` when the ref does not exist or git is unavailable.
    Callers (notably `lifecycle.new_container` in origin-mode) treat
    None as "ref missing" and raise a user-facing error.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def show_file_at_ref(repo_root: Path, ref: str, path: str) -> str | None:
    """Read a file's content at a git ref, without touching the working tree.

    ``ref`` may be anything git resolves — a full ref name, a branch, or a raw
    SHA. Returns ``None`` when the ref or the file does not exist, or when git
    is unavailable; returns ``""`` for a file that exists but is empty (a
    distinct state the caller must not confuse with "absent").

    Used by `branch_config` to read a target branch's `.jailbee/config.yaml` before
    `jailbee new` clones it.
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def branch_exists_in_source(repo_root: Path, branch: str) -> bool:
    """Check whether `branch` exists in `repo_root` as a local or origin ref.

    Used by `jailbee new` for pre-flight validation before creating a container.
    Mirrors the in-container `_branch_exists_in_source` in lifecycle.py but
    runs on the host (no `incus exec` round-trip).

    Returns False if neither ref resolves, or if git is unavailable.
    """
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        try:
            result = subprocess.run(
                ["git", "show-ref", "--verify", "--quiet", ref],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
        except (FileNotFoundError, OSError):
            return False
        if result.returncode == 0:
            return True
    return False


def branch_exists_locally(repo_root: Path, branch: str) -> bool:
    """Check whether `branch` exists as a local `refs/heads/<branch>` ref.

    Distinct from `branch_exists_in_source` in that origin refs do **not**
    count: when `git clone --shared --branch X /local/path` runs in a
    container, the source's `refs/remotes/origin/*` are not visible to
    the clone. Only `refs/heads/*` qualifies as "local". Callers use
    this to decide whether to use the `--branch` clone (local mode) or
    fall back to origin-mode (clone + `checkout -B <branch> <origin sha>`).

    Returns False if the ref is absent or git is unavailable.
    """
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


class GitError(RuntimeError):
    """Raised when a git command fails. Carries stderr in the message."""


def fetch_url(repo_root: Path, url: str, refspec: str) -> None:
    """Run `git fetch <url> <refspec>` in repo_root.

    Used by `jailbee git fetch` to pull commits from a container's clone via the
    ext::-transport URL built by `sync._build_ext_url`. `refspec` is
    expected in `+refs/heads/<branch>:refs/jailbee/<short>/<branch>` form;
    the leading `+` allows non-fast-forward updates so rewritten history
    inside the container is mirrored without manual cleanup.

    Passes ``-c protocol.ext.allow=always`` because git blocks ``ext::``
    transports by default (CVE-2020-5260 mitigation). We construct the
    URL ourselves from a trusted container name, so allowing it for
    this single invocation is safe.

    Passes ``--no-recurse-submodules`` because git's ``fetch.recurseSubmodules``
    defaults to ``on-demand``: without it, fetching a superproject commit that
    bumps a submodule to a container-authored commit makes git recurse and try
    to fetch that commit from the submodule's *real upstream* (which doesn't
    have it) — failing with ``not our ref``. jailbee transports submodule objects
    itself via ``submodules.transport_submodules_to_host``.

    git's output is inherited by the parent process — the user sees
    progress and the resulting ref update directly.
    """
    returncode = subprocess.call(
        [
            "git",
            "-c",
            "protocol.ext.allow=always",
            "fetch",
            "--no-recurse-submodules",
            url,
            refspec,
        ],
        cwd=repo_root,
    )
    if returncode != 0:
        raise GitError(f"git fetch failed (exit {returncode})")


def push_url(repo_root: Path, url: str, refspec: str) -> None:
    """Run `git push <url> <refspec>` in repo_root.

    Mirror of `fetch_url` for the host->container direction. Used by
    `jailbee git push` to send a host branch into a container's clone via the
    ``ext::incus exec ... git receive-pack`` URL built by
    ``sync._build_receive_url``. `refspec` is expected in
    `+refs/heads/<branch>:refs/jailbee/host/<branch>` form; the leading `+`
    allows non-FF updates so a force-pushed host branch is mirrored
    without manual cleanup.

    Passes ``-c protocol.ext.allow=always`` because git blocks ``ext::``
    transports by default (CVE-2020-5260 mitigation). We construct the
    URL ourselves from a trusted container name, so allowing it for
    this single invocation is safe.

    Passes ``--no-recurse-submodules`` so a configured
    ``push.recurseSubmodules=check`` can't abort the superproject push over
    unpushed submodule commits — jailbee pushes submodule objects itself via
    ``submodules.transport_submodules_to_container``.

    git's output is inherited by the parent process — the user sees the
    push progress and any error from receive-pack directly.
    """
    returncode = subprocess.call(
        ["git", "-c", "protocol.ext.allow=always", "push", "--no-recurse-submodules", url, refspec],
        cwd=repo_root,
    )
    if returncode != 0:
        raise GitError(f"git push failed (exit {returncode})")


def check_ref_format(name: str) -> bool:
    """Return True if `name` is a valid git branch name (`git check-ref-format --branch`)."""
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", name],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def remote_branch_sha(repo_root: Path, remote: str, branch: str) -> str | None:
    """Return the sha of `refs/heads/<branch>` on `<remote>`, or None if absent.

    Uses `git ls-remote`; a network/SSH round-trip. Used only as the
    `--force-with-lease` anchor, so it runs only on the `--force` path.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", remote, f"refs/heads/{branch}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    line = result.stdout.strip()
    return line.split("\t", 1)[0] if line else None


def rename_branch(repo_root: Path, old: str, new: str) -> None:
    """Rename local branch `old` to `new` (`git branch -m`). Raises GitError."""
    returncode = subprocess.call(["git", "branch", "-m", old, new], cwd=repo_root)
    if returncode != 0:
        raise GitError(f"git branch -m failed (exit {returncode})")


def set_upstream(repo_root: Path, branch: str, upstream: str) -> None:
    """Set `branch`'s upstream to `upstream` (`git branch --set-upstream-to`). Raises GitError."""
    returncode = subprocess.call(
        ["git", "branch", f"--set-upstream-to={upstream}", branch], cwd=repo_root
    )
    if returncode != 0:
        raise GitError(f"git branch --set-upstream-to failed (exit {returncode})")


def push_to_origin(
    repo_root: Path,
    src_ref: str,
    branch: str,
    *,
    force_with_lease: str | None = None,
) -> None:
    """Run `git push origin <src_ref>:refs/heads/<branch>` in repo_root.

    Used by `jailbee pr` to publish a container's fetched branch
    (`refs/jailbee/<short>/<branch>`) to the GitHub origin under the host's
    credentials. With `force_with_lease=None` (default) no `--force` and no
    leading `+` is used — git's native fast-forward rule rejects a diverged
    remote branch, which is what we want before opening a PR. When
    `force_with_lease` is a sha, `--force-with-lease=refs/heads/<branch>:<sha>`
    is passed so the branch is overwritten only if it is still at `<sha>`
    (rebased/amended PR head); a concurrent push makes git refuse. jailbee never
    issues a plain `--force`.

    Output is inherited by the parent process so the user sees push
    progress and any SSH auth prompt directly.
    """
    cmd = ["git", "push"]
    if force_with_lease is not None:
        cmd.append(f"--force-with-lease=refs/heads/{branch}:{force_with_lease}")
    cmd += ["origin", f"{src_ref}:refs/heads/{branch}"]
    returncode = subprocess.call(cmd, cwd=repo_root)
    if returncode != 0:
        raise GitError(f"git push failed (exit {returncode})")


def commit_subject(repo_root: Path, ref: str) -> str | None:
    """Return the subject line of the commit at `ref`, or None.

    None covers both an unresolvable ref and an empty subject — callers
    fall back to another title source.
    """
    ok, out = run_capture(str(repo_root), ["log", "-1", "--format=%s", ref])
    if not ok:
        return None
    subject = out.strip()
    return subject or None


def run_capture(cwd: str, args: list[str]) -> tuple[bool, str]:
    """Run ``git <args>`` in ``cwd``; return ``(exit_ok, stdout)``.

    The generic host-side executor used by the submodule branch-placement
    routine. Never raises: a missing ``git`` / bad cwd maps to ``(False, "")``,
    matching the ``check=False`` style of the other helpers here.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return (False, "")
    return (result.returncode == 0, result.stdout)


def rev_parse(repo_root: Path, ref: str) -> str | None:
    """Return the full OID of `ref`, or None if it doesn't resolve."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if result.returncode != 0:
        return None
    oid = result.stdout.strip()
    return oid or None


def list_refs(repo_root: Path, prefix: str) -> list[str]:
    """Return all refs under `prefix` (e.g. ``refs/jailbee/feat-foo/``)."""
    try:
        result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", prefix],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def list_branches(repo_root: Path) -> list[str]:
    """Return local branch short names in `repo_root`, or [] on failure.

    Same never-raises contract as `list_refs`: shell completion calls this on
    every TAB press, where a missing git binary or a non-repo cwd must produce
    an empty list rather than a traceback across the user's prompt.
    """
    try:
        result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def delete_ref(repo_root: Path, ref: str) -> None:
    """Delete a ref via `git update-ref -d`. Never raises."""
    try:
        subprocess.run(
            ["git", "update-ref", "-d", ref],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return


def log_oneline(repo_root: Path, range_spec: str) -> list[str]:
    """Return the oneline log of `range_spec` (e.g. ``abc..def``)."""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", range_spec],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def local_branch_exists(repo_root: Path, branch: str) -> bool:
    """Return True if `refs/heads/<branch>` exists in `repo_root`."""
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def remote_ref_exists(repo_root: Path, remote: str, branch: str) -> bool:
    """Return True if `refs/remotes/<remote>/<branch>` exists locally."""
    try:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{branch}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def is_merged_into(repo_root: Path, branch: str, into: str) -> bool:
    """Return True if `refs/heads/<branch>` is an ancestor of `<into>`.

    Used by `jailbee git pull --cleanup` to verify a host branch is safe to
    delete (its tip is already reachable from the merge target).
    Returns False on any error (missing git, missing ref) — callers
    treat False as "don't offer to delete this branch."
    """
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", f"refs/heads/{branch}", into],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return False
    return result.returncode == 0


def fast_forward_branch(repo_root: Path, branch: str, source_ref: str) -> bool:
    """Fast-forward ``refs/heads/<branch>`` to ``source_ref`` without checkout.

    Uses ``git fetch . <source_ref>:refs/heads/<branch>``, which updates the
    branch ref only when the move is a fast-forward (no leading ``+``), and
    refuses to touch a branch that is currently checked out. Returns True
    on success, False on a non-fast-forward (the caller falls back to the
    checkout path or errors).
    """
    result = subprocess.run(
        ["git", "fetch", ".", f"{source_ref}:refs/heads/{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def host_tree_dirty(repo_root: Path) -> bool:
    """Return True if ``git status --porcelain`` in ``repo_root`` has output."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def create_branch(
    repo_root: Path,
    branch: str,
    *,
    start_point: str,
    track: str | None,
) -> None:
    """Create branch `branch` at `start_point` and check it out.

    Output goes to the terminal. If `track` is given, sets the new branch's
    upstream via `git branch --set-upstream-to`. We don't use
    `git checkout -b --track` because the start point is `refs/jailbee/...`
    and the tracking target is `origin/...` — two different refs.
    """
    returncode = subprocess.call(
        ["git", "checkout", "-b", branch, start_point],
        cwd=repo_root,
    )
    if returncode != 0:
        raise GitError(f"git checkout -b failed (exit {returncode})")

    if track is not None:
        returncode = subprocess.call(
            ["git", "branch", f"--set-upstream-to={track}", branch],
            cwd=repo_root,
        )
        if returncode != 0:
            raise GitError(f"git branch --set-upstream-to failed (exit {returncode})")


def checkout_branch(repo_root: Path, branch: str) -> None:
    """Run `git checkout <branch>`. Output goes to the terminal.

    Raises `GitError` on non-zero exit.
    """
    returncode = subprocess.call(["git", "checkout", branch], cwd=repo_root)
    if returncode != 0:
        raise GitError(f"git checkout failed (exit {returncode})")


def merge_ref(
    repo_root: Path,
    ref: str,
    *,
    message: str | None,
    no_ff: bool,
    ff_only: bool,
) -> None:
    """Run `git merge` with the given flags. Output goes to the terminal.

    Raises `GitError` on non-zero exit — typically a conflict, in which
    case the user has already seen git's stderr and the working tree is
    in merge state for manual resolution.
    """
    cmd = ["git", "merge"]
    if ff_only:
        cmd.append("--ff-only")
    if no_ff:
        cmd.append("--no-ff")
    if message is not None:
        cmd.extend(["-m", message])
    cmd.append(ref)
    returncode = subprocess.call(cmd, cwd=repo_root)
    if returncode != 0:
        raise GitError(f"git merge failed (exit {returncode})")


def delete_branch(repo_root: Path, branch: str) -> None:
    """Run `git branch -d <branch>`. Output goes to the terminal.

    Uses `-d` (safe delete) not `-D` (force) — callers should verify
    the branch is merged via `is_merged_into` first. Raises `GitError`
    on non-zero exit (e.g., branch not fully merged, currently checked
    out).
    """
    returncode = subprocess.call(["git", "branch", "-d", branch], cwd=repo_root)
    if returncode != 0:
        raise GitError(f"git branch -d failed (exit {returncode})")


def submodule_update(repo_root: Path) -> None:
    """Run `git submodule update --init --recursive` in repo_root.

    Passes `-c protocol.file.allow=always` so submodule clones from local
    paths work (git blocks the file transport for submodules by default
    since 2.38 / CVE-2022-39253). Output is inherited by the parent.
    """
    returncode = subprocess.call(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "update",
            "--init",
            "--recursive",
        ],
        cwd=repo_root,
    )
    if returncode != 0:
        raise GitError(f"git submodule update failed (exit {returncode})")


def fetch_url_multi(repo_root: Path, url: str, refspecs: list[str]) -> None:
    """`git fetch <url> <refspec...>` in repo_root (multiple refspecs).

    ``--no-recurse-submodules``: this is itself a per-submodule transport, so
    we must not let git's ``on-demand`` default recurse further (see
    ``fetch_url``).
    """
    returncode = subprocess.call(
        [
            "git",
            "-c",
            "protocol.ext.allow=always",
            "fetch",
            "--no-recurse-submodules",
            url,
            *refspecs,
        ],
        cwd=repo_root,
    )
    if returncode != 0:
        raise GitError(f"git fetch failed (exit {returncode})")


def push_url_multi(repo_root: Path, url: str, refspecs: list[str]) -> None:
    """`git push <url> <refspec...>` in repo_root (multiple refspecs).

    ``--no-recurse-submodules``: jailbee drives submodule transport explicitly;
    don't let a configured ``push.recurseSubmodules`` interfere (see
    ``push_url``).
    """
    returncode = subprocess.call(
        [
            "git",
            "-c",
            "protocol.ext.allow=always",
            "push",
            "--no-recurse-submodules",
            url,
            *refspecs,
        ],
        cwd=repo_root,
    )
    if returncode != 0:
        raise GitError(f"git push failed (exit {returncode})")


def clone_url(url: str, dest: Path) -> None:
    """`git clone <url> <dest>` over the ext:: transport (dest.parent must exist).

    ``--no-recurse-submodules``: a brand-new submodule's own nested submodules
    (if any) are initialized separately and offline; cloning must not try to
    reach their upstreams.
    """
    returncode = subprocess.call(
        [
            "git",
            "-c",
            "protocol.ext.allow=always",
            "clone",
            "--no-recurse-submodules",
            url,
            str(dest),
        ],
        cwd=dest.parent,
    )
    if returncode != 0:
        raise GitError(f"git clone failed (exit {returncode})")


def submodule_status_paths(repo_root: Path) -> list[str]:
    """Return submodule paths (recursive, top-relative) via `git submodule status`.

    Each line looks like ` <sha> <path> (<describe>)`, `-<sha> <path>`, or
    `+<sha> <path>` — the second whitespace-token is the path. Returns []
    when there are no submodules (or the call errors).
    """
    try:
        result = subprocess.run(
            ["git", "submodule", "status", "--recursive"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    paths: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            paths.append(parts[1])
    return paths
