"""GitHub PR resolution and fetch — supports `jailbee new --pr <N>`.

This module is the only place that calls the `gh` CLI. All other jailbee
modules stay PR-agnostic. The main functions are:

  - resolve_pr(): subprocess `gh pr view` → PrInfo
  - fetch_pr_head(): subprocess `git fetch <remote> +pull/N/head:...`
                     into jailbee's own `refs/jailbee/pr/<N>/head`
  - resolve_pr_head_sha(): the --no-fetch counterpart — resolve the head
                           from refs already on the host
  - create_pr(): subprocess `gh pr create` → PrCreated
                 (used by `jailbee pr`; idempotent on already-exists)
  - view_existing_pr(): subprocess `gh pr view <head> --json` (update path)
  - edit_pr(): subprocess `gh pr edit` — update title/body
  - set_ready(): subprocess `gh pr ready [--undo]` — toggle draft state
  - open_pr_in_browser(): subprocess `gh pr view --web` (best effort)

All raise PrError subclasses with user-actionable messages on failure.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Never

from jailbee.retry import with_remote_retry

GH_PR_VIEW_JSON_FIELDS = (
    "number,headRefName,headRefOid,state,baseRefName,author,isCrossRepository,headRepositoryOwner"
)


@dataclass(frozen=True)
class PrInfo:
    """Resolved metadata for a single GitHub PR.

    The last three fields describe *where the head lives and who owns it*.
    They carry defaults because gh may serialise them as `null` (a deleted
    author) and because test helpers construct PrInfo with the core fields
    only; `resolve_pr` always sets them explicitly.
    """

    number: int
    head_ref: str
    head_sha: str
    state: Literal["OPEN", "CLOSED", "MERGED"]
    base_ref: str
    author_login: str | None = None
    is_cross_repository: bool = False
    head_repo_owner: str | None = None


@dataclass(frozen=True)
class FetchResult:
    """Outcome of fetching a PR head into `refs/jailbee/pr/<N>/head`."""

    updated: bool
    prev_sha: str | None  # None when jailbee had not fetched this head before
    new_sha: str
    ref: str  # the host ref the head now lives in


@dataclass(frozen=True)
class PrCreated:
    """Outcome of create_pr — the PR that now exists for the head branch."""

    number: int
    url: str
    already_existed: bool


class PrError(Exception):
    """Base for all pr.py errors."""


class PrResolveError(PrError):
    """`gh pr view` failed: gh missing, not authed, PR not found, etc."""


class PrFetchError(PrError):
    """git fetch of the PR head failed (gh/network/auth, or git missing)."""


class PrCreateError(PrError):
    """`gh pr create` failed: gh missing, not authed, validation error, etc."""


class PrEditError(PrError):
    """`gh pr edit` / `gh pr ready` failed: gh missing, not authed, etc."""


def resolve_pr(repo_root: Path, number: int, *, remote: str) -> PrInfo:
    """Resolve PR metadata via `gh pr view`.

    Pre-flights `git remote get-url <remote>` to fail fast with a clear
    message when the upstream is missing or isn't on GitHub, then invokes
    `gh pr view <number> --json ...` with cwd=repo_root so gh's own
    auto-detection picks up the right repo.

    Note that `gh` resolves the repo by its own rules (it prefers a remote
    named `upstream`, and honours `gh repo set-default`), so in a multi-remote
    repo it may not land on `remote`. This pre-flight only guarantees that the
    remote jailbee itself fetches from is a GitHub one.
    """
    _validate_github_origin(repo_root, remote)
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", str(number), "--json", GH_PR_VIEW_JSON_FIELDS],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise PrResolveError("--pr requires the 'gh' CLI. Install: https://cli.github.com/") from e
    if proc.returncode != 0:
        _raise_from_gh_failure(number, proc.stderr)
    return _pr_info_from_json(proc.stdout)


def _pr_info_from_json(stdout: str) -> PrInfo:
    """Build a PrInfo from `gh pr view --json GH_PR_VIEW_JSON_FIELDS` output."""
    data = json.loads(stdout)
    return PrInfo(
        number=data["number"],
        head_ref=data["headRefName"],
        head_sha=data["headRefOid"],
        state=data["state"],
        base_ref=data["baseRefName"],
        author_login=(data.get("author") or {}).get("login"),
        is_cross_repository=bool(data.get("isCrossRepository")),
        head_repo_owner=(data.get("headRepositoryOwner") or {}).get("login"),
    )


def find_pr_for_branch(repo_root: Path, branch: str) -> PrInfo | None:
    """Return the PR whose head is `branch`, or None when there is none.

    `jailbee pr` uses this on the create path: a container made from an existing
    branch carries no PR label, yet that branch may already have a PR open —
    and without this lookup jailbee would propose a fresh head branch name and open
    a *second* PR for the same work.

    Best-effort by design: no PR, no `gh`, no network, an origin that is not on
    GitHub, or output gh changes the shape of all yield None, which falls back
    to the ordinary create path. Never raises.
    """
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", branch, "--json", GH_PR_VIEW_JSON_FIELDS],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return _pr_info_from_json(proc.stdout)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def pr_head_ref(number: int) -> str:
    """Host ref where jailbee keeps PR #`number`'s head.

    Deliberately *not* a branch. `git fetch` refuses to update a
    `refs/heads/*` ref that is checked out in any worktree of the repo
    ("refusing to fetch into branch ... checked out at ..."), which made
    `jailbee new --pr N` fail outright whenever the host happened to have the
    PR's own branch checked out — the normal state when reviewing your own
    PR. jailbee's own namespace has no such restriction, cannot collide with
    the user's branches, and keeps the fetched objects alive against `gc`.
    """
    return f"refs/jailbee/pr/{number}/head"


def fetch_pr_head(repo_root: Path, pr: PrInfo, *, remote: str) -> FetchResult:
    """Fetch PR head into `refs/jailbee/pr/<N>/head` (forced).

    Uses `git fetch <remote> +pull/<N>/head:refs/jailbee/pr/<N>/head`, which works
    uniformly for same-repo and fork PRs and never touches a branch — see
    `pr_head_ref`. The refspec is forced because the PR head is
    authoritative: an author who force-pushes (rebase, amend) rewrites it,
    and jailbee's copy must follow rather than reject the update.

    Returns a FetchResult describing whether jailbee's copy of the head moved.
    When jailbee had not fetched this head before, `prev_sha` is `None` and
    `updated` is `True`. Raises PrFetchError when the fetch fails, which is
    offered as a retry on a TTY (see `retry.with_remote_retry`).
    """
    prev_sha = _rev_parse(repo_root, pr_head_ref(pr.number))

    with_remote_retry(
        lambda: _run_pr_head_fetch(repo_root, pr, remote),
        label=f"fetching PR #{pr.number}'s head",
        catch=PrFetchError,
    )

    return FetchResult(
        updated=prev_sha != pr.head_sha,
        prev_sha=prev_sha,
        new_sha=pr.head_sha,
        ref=pr_head_ref(pr.number),
    )


def _run_pr_head_fetch(repo_root: Path, pr: PrInfo, remote: str) -> None:
    """Fetch PR `pr`'s head into `refs/jailbee/pr/<N>/head` once.

    The retryable unit of `fetch_pr_head`: one `git fetch` plus the
    classification of its failure as `PrFetchError`.
    """
    refspec = f"+pull/{pr.number}/head:{pr_head_ref(pr.number)}"
    try:
        proc = subprocess.run(
            ["git", "fetch", remote, refspec],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as e:
        raise PrFetchError("--pr requires git to be installed and on PATH") from e
    if proc.returncode != 0:
        raise PrFetchError(f"git fetch failed for PR #{pr.number}: {proc.stderr.strip()}")


def _rev_parse(repo_root: Path, ref: str) -> str | None:
    """Resolve `ref` in `repo_root`; None when it does not exist.

    Raises PrFetchError when git itself is unavailable — a missing git is a
    setup problem, not an absent ref.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as e:
        raise PrFetchError("--pr requires git to be installed and on PATH") from e
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def fetch_base_ref(repo_root: Path, base_ref: str, *, remote: str) -> str | None:
    """Refresh ``refs/remotes/<remote>/<base_ref>`` in `repo_root`; return its SHA.

    A PR-review container's AHEAD numbers are computed against
    ``refs/jailbee/base/<base_ref>``, which `lifecycle` seeds from the host's
    ``refs/remotes/<remote>/<base_ref>`` at create time. Fetching the PR head
    alone leaves that remote-tracking ref at whatever the host last fetched;
    once it predates the PR's branch point, the three-dot diff picks *it* as
    the merge base and folds every base-branch commit made since into the PR's
    diff. Refreshing it here makes the seeded anchor at-or-after the branch
    point, which is exactly the condition for `jailbee ls` to match GitHub.

    The refspec is forced: a base branch can be rebased or force-pushed
    upstream, and the remote-tracking ref must follow the real remote tip.

    Best-effort — returns ``None`` on any failure (base branch deleted
    upstream, no network, git missing) and never raises. A stale anchor
    degrades the AHEAD numbers; it must not block container creation.
    """
    refspec = f"+refs/heads/{base_ref}:refs/remotes/{remote}/{base_ref}"
    try:
        proc = subprocess.run(
            ["git", "fetch", remote, refspec],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return None
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}/{base_ref}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if resolved.returncode != 0:
        return None
    return resolved.stdout.strip() or None


def resolve_pr_head_sha(repo_root: Path, pr: PrInfo) -> str:
    """Resolve PR `pr`'s head from refs already on the host, without fetching.

    The --no-fetch path: jailbee's own `refs/jailbee/pr/<N>/head` first (what a
    previous `--pr` run left behind, and the only ref jailbee itself maintains),
    then a local `refs/heads/<head_ref>` for the user who fetched the head by
    hand. Raises PrError when the head is on neither — without it the
    container would be built against the wrong code.
    """
    for ref in (pr_head_ref(pr.number), f"refs/heads/{pr.head_ref}"):
        sha = _rev_parse(repo_root, ref)
        if sha is not None:
            return sha
    raise PrError(
        f"PR #{pr.number}'s head is not on the host: neither "
        f"'{pr_head_ref(pr.number)}' nor 'refs/heads/{pr.head_ref}' exists in "
        f"{repo_root}. Drop --no-fetch to fetch it."
    )


def create_pr(
    repo_root: Path,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
    remote: str,
    draft: bool = True,
) -> PrCreated:
    """Create a GitHub PR for `head` via `gh pr create` (non-interactive).

    Pre-flights the GitHub upstream like resolve_pr. When gh reports that a
    PR for `head` already exists, the existing PR is looked up with
    `gh pr view <head>` and returned with `already_existed=True`, making
    repeated invocations idempotent for the caller.
    """
    _validate_github_origin(repo_root, remote)
    cmd = [
        "gh",
        "pr",
        "create",
        "--head",
        head,
        "--base",
        base,
        "--title",
        title,
        "--body",
        body,
    ]
    if draft:
        cmd.append("--draft")
    try:
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise PrCreateError(
            "jailbee pr requires the 'gh' CLI. Install: https://cli.github.com/"
        ) from e
    if proc.returncode != 0:
        stderr = proc.stderr
        if "already exists" in stderr.lower():
            return view_existing_pr(repo_root, head)
        if (
            "not logged" in stderr.lower()
            or "authentication" in stderr.lower()
            or "gh auth login" in stderr.lower()
        ):
            raise PrCreateError("'gh' is not authenticated. Run: gh auth login")
        raise PrCreateError(f"'gh pr create' failed: {stderr.strip()}")
    url = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    return PrCreated(number=_number_from_url(url), url=url, already_existed=False)


def open_pr_in_browser(repo_root: Path, number: int) -> None:
    """Open PR #`number` in the browser via `gh pr view --web` (best effort)."""
    subprocess.run(
        ["gh", "pr", "view", str(number), "--web"],
        cwd=repo_root,
        check=False,
    )


def edit_pr(
    repo_root: Path,
    number: int,
    *,
    title: str | None = None,
    body: str | None = None,
) -> None:
    """Update PR #`number`'s title/body via `gh pr edit`.

    A field left as None is omitted so it stays unchanged; a no-op when both
    are None. Used by `jailbee pr` on the update path.
    """
    if title is None and body is None:
        return
    cmd = ["gh", "pr", "edit", str(number)]
    if title is not None:
        cmd += ["--title", title]
    if body is not None:
        cmd += ["--body", body]
    _run_gh_mutation(repo_root, cmd, "gh pr edit")


def set_ready(repo_root: Path, number: int, ready: bool) -> None:
    """Mark PR #`number` ready (`gh pr ready`) or back to draft (`--undo`)."""
    cmd = ["gh", "pr", "ready", str(number)]
    if not ready:
        cmd.append("--undo")
    _run_gh_mutation(repo_root, cmd, "gh pr ready")


def _run_gh_mutation(repo_root: Path, cmd: list[str], label: str) -> None:
    """Run a mutating `gh pr ...` command, mapping failures to PrEditError."""
    try:
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise PrEditError(
            "updating a PR requires the 'gh' CLI. Install: https://cli.github.com/"
        ) from e
    if proc.returncode != 0:
        stderr = proc.stderr.lower()
        if "not logged" in stderr or "authentication" in stderr or "gh auth login" in stderr:
            raise PrEditError("'gh' is not authenticated. Run: gh auth login")
        raise PrEditError(f"'{label}' failed: {proc.stderr.strip()}")


def _validate_github_origin(repo_root: Path, remote: str) -> None:
    """Fail fast unless `remote` exists in `repo_root` and points at GitHub."""
    try:
        proc = subprocess.run(
            ["git", "remote", "get-url", remote],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as e:
        raise PrResolveError("--pr requires git to be installed and on PATH") from e
    if proc.returncode != 0:
        raise PrResolveError(f"--pr requires a GitHub '{remote}' remote in {repo_root}")
    if "github.com" not in proc.stdout:
        raise PrResolveError(
            f"--pr requires a GitHub '{remote}' remote in {repo_root} "
            f"(found: {proc.stdout.strip()})"
        )


def _raise_from_gh_failure(number: int, stderr: str) -> Never:
    msg = stderr.lower()
    if "no pull requests found" in msg or "could not resolve" in msg or "not found" in msg:
        raise PrResolveError(f"PR #{number} not found")
    if "not logged" in msg or "authentication" in msg or "gh auth login" in msg:
        raise PrResolveError("'gh' is not authenticated. Run: gh auth login")
    raise PrResolveError(f"'gh pr view' failed: {stderr.strip()}")


def view_existing_pr(repo_root: Path, head: str) -> PrCreated:
    """Resolve the PR that already exists for `head` (already-exists / update path)."""
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", head, "--json", "number,url"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:
        raise PrCreateError(
            "jailbee pr requires the 'gh' CLI. Install: https://cli.github.com/"
        ) from e
    if proc.returncode != 0:
        raise PrCreateError(f"'gh pr view {head}' failed: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    return PrCreated(number=int(data["number"]), url=str(data["url"]), already_existed=True)


def _number_from_url(url: str) -> int:
    """Parse the trailing PR number out of a github.com/.../pull/<N> URL."""
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError as e:
        raise PrCreateError(f"could not parse PR number from gh output: {url!r}") from e
