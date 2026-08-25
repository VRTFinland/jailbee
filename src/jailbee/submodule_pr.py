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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee import git, submodules
from jailbee.incus import IncusError

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.pr_flow import PrRecord


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


STATE_KEY = "user.jailbee.sub_pr"


def resolve_remote(repo_root: Path, subpath: str) -> str:
    """The upstream remote of the host sub-repo at `subpath`.

    Resolved against the submodule's own directory rather than inherited from
    `cfg.upstream_remote`: a submodule is a separate repository and may name
    its upstream something the superproject does not.
    """
    return git.detect_upstream_remote(repo_root / subpath) or git.DEFAULT_REMOTE


def resolve_base_branch(repo_root: Path, subpath: str, *, override: str | None) -> str:
    """The branch a submodule PR targets in the submodule's own repository.

    Order: `--base` > `submodule.<name>.branch` in the *parent level's*
    `.gitmodules` > the sub-repo's `<remote>/HEAD` > `main`. The remote name is
    resolved per submodule (see `resolve_remote`), which is why this cannot
    reuse `submodules._detect_submodule_default` — that one hardcodes `origin`
    for its container callers.
    """
    if override:
        return override
    parent, _, leaf = subpath.rpartition("/")
    parent_dir = str(repo_root / parent) if parent else str(repo_root)
    declared = submodules.declared_branch_for_path(git.run_capture, parent_dir, leaf)
    if declared:
        return declared
    host_sub = repo_root / subpath
    remote = resolve_remote(repo_root, subpath)
    ok, out = git.run_capture(
        str(host_sub), ["symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD"]
    )
    ref = out.strip() if ok else ""
    if ref:
        return ref.split("/", 1)[1] if "/" in ref else ref
    return "main"


def source_ref(short: str, subpath: str, branch: str | None) -> str:
    """The host ref holding the submodule commits to publish.

    `submodules.transport_submodules_to_host` writes both forms; a detached
    submodule has no branch ref, so its HEAD is the only source.
    """
    prefix = f"refs/jailbee-sub/{short}/{subpath}"
    return f"{prefix}/heads/{branch}" if branch else f"{prefix}/HEAD"


def _load_map(incus: Incus, full: str) -> dict[str, dict[str, object]]:
    """The container's submodule-PR map, or {} when unset or malformed."""
    raw = incus.config_get(full, STATE_KEY)
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {k: v for k, v in loaded.items() if isinstance(k, str) and isinstance(v, dict)}


def recorded_paths(incus: Incus, full: str) -> list[str]:
    """Submodule paths that have a PR recorded on this container, sorted."""
    return sorted(_load_map(incus, full))


class SubmodulePrState:
    """`pr_flow.PrState` over one entry of the `user.jailbee.sub_pr` JSON map.

    A single key rather than one flat key per submodule: submodule paths
    contain slashes, so flat keys would need sanitising and `a/b` would then
    collide with `a_b`. The write is one atomic `config_set`, so the careful
    write ordering `jailbee pr` needs across three labels has no counterpart
    here.
    """

    def __init__(self, incus: Incus, full: str, subpath: str) -> None:
        self._incus = incus
        self._full = full
        self._subpath = subpath

    def read(self) -> PrRecord:
        from jailbee.pr_flow import PrRecord

        entry = _load_map(self._incus, self._full).get(self._subpath, {})
        number = entry.get("pr")
        head = entry.get("branch")
        return PrRecord(
            number=number if isinstance(number, int) else None,
            head=head if isinstance(head, str) and head else None,
            author=bool(entry.get("author")),
            adopted=bool(entry.get("adopted")),
        )

    def record(
        self,
        *,
        head: str,
        author: bool,
        adopted: bool,
        number: int | None,
        context: str | None = None,
    ) -> None:
        """Merge this submodule's decision into the map, best-effort.

        `number=None` keeps whatever number is already recorded — the adoption
        path can learn the head before the number. `context`, when given,
        replaces the generic failure warning with a caller-supplied one.
        """
        current = _load_map(self._incus, self._full)
        entry = dict(current.get(self._subpath, {}))
        entry["branch"] = head
        entry["author"] = author
        entry["adopted"] = adopted
        if number is not None:
            entry["pr"] = number
        current[self._subpath] = entry
        try:
            self._incus.config_set(self._full, STATE_KEY, json.dumps(current, sort_keys=True))
        except IncusError as exc:
            from jailbee.tui import warn

            default_context = f"Could not record the submodule PR decision for '{self._subpath}'"
            warn(f"{context or default_context}: {exc}")


if TYPE_CHECKING:
    from jailbee.pr_flow import PrState

    _: type[PrState] = SubmodulePrState
