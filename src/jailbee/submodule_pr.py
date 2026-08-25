"""`jailbee submodule pr` — pull requests for a submodule's own repository.

This module holds what is genuinely submodule-only: finding which submodule
holds unpublished commits, resolving the base/head/source names from the
submodule's own git data, remembering the PR decision, and the
transport-plus-push pipeline. The interactive flow it shares with `jailbee pr`
lives in `pr_flow`.

Deliberately prompt-free: no `typer`, no confirmations. Failures are raised as
`SubmodulePrError` subclasses and presented by `cli.py`. Container git goes
through the `Incus` wrapper, host git through `git.py`, and `gh` through
`pr.py` — this module calls no `subprocess` of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.incus import IncusError

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

    # Task 10 implements the publish flow that uses this; unused for now.


class SubmodulePrError(RuntimeError):
    """Base for every `jailbee submodule pr` failure."""


class NoSubmoduleCandidatesError(SubmodulePrError):
    """No submodule has commits ahead of its base."""


class UnknownSubmodulePathError(SubmodulePrError):
    """The requested path is not a submodule of this container's repo."""


class AmbiguousSubmoduleTargetError(SubmodulePrError):
    """More than one submodule is ahead; the user must pick one."""

    def __init__(self, candidates: list[SubCandidate]) -> None:
        super().__init__("more than one submodule has commits to publish")
        self.candidates = candidates


@dataclass(frozen=True)
class SubCandidate:
    """One submodule as the container sees it.

    `commits` is `None` when no base could be resolved — the count is unknown,
    not zero. `branch` is `None` when the submodule is detached. `recorded_sha`
    is the gitlink the *immediate* superproject's HEAD commit records for this
    path, which is how `gitlink_stale` spots submodule commits the superproject
    has not been told about yet.
    """

    path: str
    commits: int | None
    branch: str | None
    dirty: bool
    head_sha: str
    recorded_sha: str
    subject: str

    @property
    def gitlink_stale(self) -> bool:
        """True when the superproject's HEAD records another commit for this path."""
        return (
            bool(self.head_sha) and bool(self.recorded_sha) and self.head_sha != self.recorded_sha
        )


# Runs once per submodule under `git submodule foreach --recursive`. The base is
# jailbee's own anchor (pinned by `submodules.seed_submodule_base_anchors` at the
# submodule commit recorded on the superproject's base branch); when that is
# absent — a legacy container, or a submodule added after creation — it falls
# back to the sub-repo's `origin/HEAD`. `origin` is right here and must not be
# parameterised: a container sub-repo's remote is one jailbee created itself.
# An unresolvable base prints "?" rather than a plausible-but-wrong zero.
_FOREACH_SNIPPET = r"""
base=""
if git rev-parse --verify --quiet "refs/jailbee/base/$JB_BASE^{commit}" >/dev/null 2>&1; then
  base="refs/jailbee/base/$JB_BASE"
elif git rev-parse --verify --quiet "refs/remotes/origin/HEAD^{commit}" >/dev/null 2>&1; then
  base="refs/remotes/origin/HEAD"
fi
count="?"
if [ -n "$base" ]; then
  count=$(git rev-list --count "$base..HEAD" 2>/dev/null) || count="?"
fi
branch=$(git symbolic-ref --short -q HEAD 2>/dev/null) || branch=""
dirty=""
git diff --quiet HEAD 2>/dev/null || dirty=1
head=$(git rev-parse HEAD 2>/dev/null) || head=""
recorded=$(git -C "$toplevel" rev-parse "HEAD:$sm_path" 2>/dev/null) || recorded=""
subject=$(git log -1 --format=%s HEAD 2>/dev/null) || subject=""
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$displaypath" "$count" "$branch" "$dirty" "$head" "$recorded" "$subject"
"""


def detect_candidates(
    cfg: Config,
    incus: Incus,
    full: str,
    *,
    repo_dir: str,
    base_branch: str,
    short: str,
) -> list[SubCandidate]:
    """Describe every submodule in the container, recursively, in one exec.

    Raises `SubmodulePrError` when the container cannot be queried: without
    this data neither the target nor the source ref can be determined, so a
    silent empty list would publish the wrong thing or nothing at all.
    """
    try:
        out = incus.exec(
            full,
            ["git", "submodule", "foreach", "--recursive", "--quiet", _FOREACH_SNIPPET],
            uid=cfg.container_user.uid,
            cwd=repo_dir,
            env={"JB_BASE": base_branch},
        )
    except IncusError as exc:
        raise SubmodulePrError(f"Could not inspect submodules in '{short}': {exc}") from exc

    subs: list[SubCandidate] = []
    for line in out.splitlines():
        fields = line.split("\t", 6)
        if len(fields) != 7 or not fields[0]:
            continue
        path, count, branch, dirty, head, recorded, subject = fields
        subs.append(
            SubCandidate(
                path=path,
                commits=int(count) if count.isdigit() else None,
                branch=branch or None,
                dirty=bool(dirty),
                head_sha=head,
                recorded_sha=recorded,
                subject=subject,
            )
        )
    return subs


def select_target(subs: list[SubCandidate], path: str | None) -> SubCandidate:
    """Pick the submodule to open a PR for.

    An explicit `path` always wins, commits or not — the user named it. Without
    one, exactly one submodule ahead of its base is targeted; several raise
    `AmbiguousSubmoduleTargetError` (two submodules are two repositories and
    two PRs, so the user picks), none raises `NoSubmoduleCandidatesError`. A
    submodule whose count is unknown is never auto-targeted.
    """
    if path is not None:
        for sub in subs:
            if sub.path == path:
                return sub
        known = ", ".join(s.path for s in subs) or "none"
        raise UnknownSubmodulePathError(
            f"'{path}' is not a submodule of this container's repo (known: {known})"
        )
    ahead = [s for s in subs if s.commits is not None and s.commits > 0]
    if not ahead:
        raise NoSubmoduleCandidatesError("no submodule has commits ahead of its base")
    if len(ahead) > 1:
        raise AmbiguousSubmoduleTargetError(ahead)
    return ahead[0]
