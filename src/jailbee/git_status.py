"""Probe git state inside jailbee containers via `incus exec`.

The probe makes one ``incus exec`` round-trip per container, runs a
small shell snippet that emits ten NUL-separated fields, and the
host parses them into a ``GitStatus``. Designed to be safe for
parallel use from a thread pool.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TypedDict

from jailbee.incus import Incus, IncusError

_SHORTSTAT_RE = re.compile(r"(?P<ins>\d+)\s+insertion|(?P<del>\d+)\s+deletion")


@dataclass(frozen=True)
class SubmoduleChange:
    """A single submodule's change vs the container's base branch.

    ``status`` is the gitlink pointer state vs base: ``"new"`` |
    ``"removed"`` | ``"modified"``. ``ahead_*`` describe committed
    changes inside the submodule; ``wt_*`` the uncommitted working tree.
    """

    path: str
    ahead_ins: int
    ahead_del: int
    ahead_commits: int
    wt_ins: int
    wt_del: int
    status: str


@dataclass(frozen=True)
class GitStatus:
    """Per-container git status, formatted for table display.

    Each field is already rendered as the string the table will show.
    """

    wt: str  # "+12 -3" | "clean" | "—" | "?"
    ahead_diff: str  # "+245 -18" | "clean" | "—" | "?"
    ahead_count: str  # "3" | "0" | "—" | "?"
    conflict: str  # "ok" | "conflict" | "—" | "?"
    submodules: tuple[SubmoduleChange, ...] = ()
    # The container's own HEAD, and whether any remote-tracking ref contains
    # it. Not columns — they feed the destroy guard and the `git_status`
    # JSON payload. `remote_contained is None` means the question could not
    # be answered, never "no".
    head_sha: str = ""
    remote_contained: bool | None = None
    # Diff vs the *host's current branch*, as opposed to `ahead_*`, which is
    # vs the container's pinned base. "?" when neither side holds the other's
    # tip — see the module docstring.
    local_diff: str = "?"  # "+12 -3" | "clean" | "?"
    local_count: str = "?"  # "3" | "0" | "?"


def _shortstat_ints(raw: str) -> tuple[int, int]:
    """Parse a single ``git diff --shortstat`` line into ``(ins, del)``.

    Empty or unrecognised input yields ``(0, 0)``.
    """
    ins = 0
    dels = 0
    for m in _SHORTSTAT_RE.finditer(raw):
        if m.group("ins"):
            ins = int(m.group("ins"))
        elif m.group("del"):
            dels = int(m.group("del"))
    return ins, dels


def parse_shortstat(raw: str) -> str:
    """Sum one or more ``git diff --shortstat`` lines into ``"+ins -del"``.

    Empty or whitespace-only input → ``"clean"``. Any line that
    contains no recognisable insertion/deletion phrase counts as
    malformed and the whole field becomes ``"?"``.
    """
    if not raw.strip():
        return "clean"
    total_ins = 0
    total_del = 0
    saw_any = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "file changed" not in line and "files changed" not in line:
            return "?"
        saw_any = True
        line_ins = 0
        line_del = 0
        for m in _SHORTSTAT_RE.finditer(line):
            if m.group("ins"):
                line_ins = int(m.group("ins"))
            elif m.group("del"):
                line_del = int(m.group("del"))
        total_ins += line_ins
        total_del += line_del
    if not saw_any:
        return "?"
    return f"+{total_ins} -{total_del}"


class _SubAcc(TypedDict):
    ahead_ins: int
    ahead_del: int
    ahead_commits: int
    wt_ins: int
    wt_del: int
    status: str


def _parse_submodules(committed_raw: str, wt_raw: str) -> tuple[SubmoduleChange, ...]:
    """Merge committed + working-tree per-submodule lines by path.

    ``committed_raw`` lines: ``path \\t status \\t commits \\t shortstat``.
    ``wt_raw`` lines: ``path \\t shortstat``.

    Submodules with no committed change, no working-tree change, and a
    plain ``modified`` status are dropped so a clean submodule never
    shows. Result is sorted by path.
    """
    acc: dict[str, _SubAcc] = {}

    def _blank() -> _SubAcc:
        return _SubAcc(
            ahead_ins=0,
            ahead_del=0,
            ahead_commits=0,
            wt_ins=0,
            wt_del=0,
            status="modified",
        )

    for line in committed_raw.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        path, status, commits_s = cols[0], cols[1], cols[2]
        if not path.strip():
            continue
        shortstat = cols[3] if len(cols) > 3 else ""
        ins, dels = _shortstat_ints(shortstat)
        entry = acc.setdefault(path, _blank())
        entry["ahead_ins"] = ins
        entry["ahead_del"] = dels
        entry["ahead_commits"] = int(commits_s) if commits_s.strip().isdigit() else 0
        entry["status"] = status or "modified"

    # Nested-submodule WT entries (from `git submodule foreach --recursive`,
    # keyed by $displaypath, e.g. "sub/nested") key independently of committed
    # entries (from the superproject's top-level `git diff --raw`, keyed by
    # the top-level gitlink path, e.g. "sub").  A dirty nested submodule will
    # therefore appear as its own row whose path does not match any committed
    # row — this is intentional and matches the "nested submodules as today"
    # feature scope (each submodule is tracked separately).
    for line in wt_raw.splitlines():
        if not line.strip():
            continue
        cols = line.split("\t", 1)
        path = cols[0]
        if not path.strip():
            continue
        shortstat = cols[1] if len(cols) > 1 else ""
        ins, dels = _shortstat_ints(shortstat)
        entry = acc.setdefault(path, _blank())
        entry["wt_ins"] = ins
        entry["wt_del"] = dels

    out: list[SubmoduleChange] = []
    for path in sorted(acc):
        e = acc[path]
        changed = (
            e["ahead_ins"]
            or e["ahead_del"]
            or e["ahead_commits"]
            or e["wt_ins"]
            or e["wt_del"]
            or e["status"] in ("new", "removed")
        )
        if not changed:
            continue
        out.append(
            SubmoduleChange(
                path=path,
                ahead_ins=e["ahead_ins"],
                ahead_del=e["ahead_del"],
                ahead_commits=e["ahead_commits"],
                wt_ins=e["wt_ins"],
                wt_del=e["wt_del"],
                status=e["status"],
            )
        )
    return tuple(out)


_PROBE_SNIPPET = r"""
set +e
cd "$REPO_DIR" 2>/dev/null || { printf '?\0?\0?\0?\0'; exit 0; }
test -d .git || { printf '?\0?\0?\0?\0'; exit 0; }

BASE=""
if [ -n "$BASE_BRANCH" ] \
   && git rev-parse --verify --quiet \
      "refs/jailbee/base/${BASE_BRANCH}^{commit}" >/dev/null 2>&1; then
  BASE="refs/jailbee/base/${BASE_BRANCH}"
elif [ -n "$BASE_BRANCH" ] \
   && git rev-parse --verify --quiet \
      "refs/remotes/origin/${BASE_BRANCH}^{commit}" >/dev/null 2>&1; then
  BASE="refs/remotes/origin/${BASE_BRANCH}"
elif [ -n "$BASE_BRANCH" ] \
   && git rev-parse --verify --quiet \
      "refs/heads/${BASE_BRANCH}^{commit}" >/dev/null 2>&1; then
  BASE="refs/heads/${BASE_BRANCH}"
elif [ -z "$BASE_BRANCH" ] \
   && git rev-parse --verify --quiet \
      "refs/remotes/origin/${DEFAULT_BRANCH}^{commit}" >/dev/null 2>&1; then
  # Only fall back to the default branch when NO base branch was requested.
  # A set-but-unresolvable BASE_BRANCH deliberately leaves BASE empty so the
  # probe reports "?" instead of a plausible-but-wrong diff against the
  # default branch (which had silently inflated AHEAD for PR-review containers
  # whose base ref never made it into the clone).
  BASE="refs/remotes/origin/${DEFAULT_BRANCH}"
fi

WT_UNSTAGED=$(git diff --shortstat --ignore-submodules=dirty HEAD 2>/dev/null) || WT_UNSTAGED=""
WT_STAGED=$(git diff --cached --shortstat --ignore-submodules=dirty 2>/dev/null) || WT_STAGED=""
SUB_WT=$(git submodule foreach --recursive --quiet \
  'git diff --shortstat HEAD || :' 2>/dev/null) || SUB_WT=""
WT="${WT_STAGED}${WT_UNSTAGED}${SUB_WT}"

if [ -n "$BASE" ]; then
  COMMITTED=$(git diff --shortstat --ignore-submodules=all \
    "${BASE}...HEAD" 2>/dev/null) || COMMITTED="?"
  SUB_COMMITTED=$(
    IFS_TAB="$(printf '\t')"
    git diff --raw --abbrev=40 "${BASE}...HEAD" 2>/dev/null \
      | while IFS="$IFS_TAB" read -r meta sub_path; do
      [ -z "$sub_path" ] && continue
      # meta = ":<oldmode> <newmode> <oldsha> <newsha> <status>"
      # Renamed gitlinks (status R*) carry a second tab-separated path, so
      # sub_path holds "<old>\t<new>"; the diff below then no-ops (empty line,
      # skipped) — rename deltas are not counted (rare, accepted).
      set -- $meta
      om=${1#:}; nm=$2; os=$3; ns=$4
      [ "$om" = "160000" ] || [ "$nm" = "160000" ] || continue
      case "$os" in *[!0]*) ;; *) continue ;; esac
      case "$ns" in *[!0]*) ;; *) continue ;; esac
      git -C "$sub_path" diff --shortstat "$os".."$ns" 2>/dev/null
    done
  )
  # If COMMITTED="?" (superproject diff failed) the whole field degrades to
  # "?"; SUB_COMMITTED is then ignored by the host parser.
  COMMITTED="${COMMITTED}
${SUB_COMMITTED}"
  COUNT=$(git rev-list --count "${BASE}..HEAD" 2>/dev/null) || COUNT="?"
  # exit 0 = clean merge; exit 1 = conflicts detected (best-effort).
  # exit >1 (unresolvable ref, usage error, old git) falls through to "?".
  git merge-tree --write-tree "${BASE}" HEAD >/dev/null 2>&1
  MT=$?
  if [ "$MT" -eq 0 ]; then
    CONFLICT="ok"
  elif [ "$MT" -eq 1 ]; then
    CONFLICT="conflict"
  else
    CONFLICT="?"
  fi
else
  COMMITTED="?"
  COUNT="?"
  CONFLICT="?"
fi

# --- per-submodule breakdown (fields 5 and 6) ---
SUB_COMMITTED_STRUCT=""
if [ -n "$BASE" ]; then
  SUB_COMMITTED_STRUCT=$(
    IFS_TAB="$(printf '\t')"
    git diff --raw --abbrev=40 "${BASE}...HEAD" 2>/dev/null \
      | while IFS="$IFS_TAB" read -r meta sub_path; do
      [ -z "$sub_path" ] && continue
      set -- $meta
      om=${1#:}; nm=$2; os=$3; ns=$4
      [ "$om" = "160000" ] || [ "$nm" = "160000" ] || continue
      os_zero=1; case "$os" in *[!0]*) os_zero=0 ;; esac
      ns_zero=1; case "$ns" in *[!0]*) ns_zero=0 ;; esac
      if [ "$os_zero" = "1" ] && [ "$ns_zero" = "0" ]; then
        status=new; commits=0; ss=""
      elif [ "$ns_zero" = "1" ]; then
        status=removed; commits=0; ss=""
      else
        status=modified
        commits=$(git -C "$sub_path" rev-list --count "$os".."$ns" 2>/dev/null) || commits=0
        ss=$(git -C "$sub_path" diff --shortstat "$os".."$ns" 2>/dev/null) || ss=""
      fi
      printf '%s\t%s\t%s\t%s\n' "$sub_path" "$status" "$commits" "$ss"
    done
  )
fi
SUB_WT_STRUCT=$(git submodule foreach --recursive --quiet \
  'ss=$(git diff --shortstat HEAD 2>/dev/null || :); printf "%s\t%s\n" "$displaypath" "$ss"' \
  2>/dev/null) || SUB_WT_STRUCT=""

# --- fields 7-10: head sha, remote containment, LOCAL diff vs the host ---
HEAD_SHA=$(git rev-parse HEAD 2>/dev/null) || HEAD_SHA=""

# "were these commits ever pushed anywhere". Remote-tracking refs may be
# stale in strict mode; the question is whether the work escaped the
# container, not whether it is current. Empty = the command itself failed.
if REMOTE_REFS=$(git branch -r --contains HEAD 2>/dev/null); then
  if [ -n "$REMOTE_REFS" ]; then
    REMOTE_CONTAINED=1
  else
    REMOTE_CONTAINED=0
  fi
else
  REMOTE_CONTAINED=""
fi

# "?" until proven otherwise, so an EMPTY field unambiguously means
# "computed, and clean". The host only tries its own side when it sees "?".
LOCAL_DIFF="?"
LOCAL_COUNT="?"
if [ -n "$HOST_HEAD" ] \
   && git cat-file -e "${HOST_HEAD}^{commit}" >/dev/null 2>&1; then
  LOCAL_DIFF=$(git diff --shortstat --ignore-submodules=dirty \
    "${HOST_HEAD}...HEAD" 2>/dev/null) || LOCAL_DIFF="?"
  LOCAL_COUNT=$(git rev-list --count "${HOST_HEAD}..HEAD" 2>/dev/null) || LOCAL_COUNT="?"
fi

printf '%s\0%s\0%s\0%s\0%s\0%s\0%s\0%s\0%s\0%s\0' \
  "$WT" "$COMMITTED" "$COUNT" "$CONFLICT" "$SUB_COMMITTED_STRUCT" "$SUB_WT_STRUCT" \
  "$HEAD_SHA" "$REMOTE_CONTAINED" "$LOCAL_DIFF" "$LOCAL_COUNT"
"""


def probe_container_git(
    incus: Incus,
    full_name: str,
    repo_dir: str,
    base_branch: str | None,
    default_branch: str,
    *,
    uid: int | None = None,
    timeout_s: int = 3,
    host_head: str | None = None,
) -> GitStatus:
    """Run the probe snippet inside `full_name`, return parsed `GitStatus`.

    AHEAD ±/↑ and the conflict flag are computed against the container's
    base branch (`refs/jailbee/base/<base_branch>`, falling back to
    `refs/remotes/origin/<base_branch>`, then `refs/heads/<base_branch>`).
    `origin/<default_branch>` is used **only** when no base branch was
    requested (`base_branch` is None/empty): a set-but-unresolvable base
    branch yields all-`?` rather than silently comparing against the default
    branch. `uid` is forwarded to `incus exec --user` so `git` runs as the
    container's dev user (avoids the `dubious ownership` refusal on a
    dev-owned repo).

    ``host_head`` is the host repo's current HEAD sha. When the container
    already holds that commit object, the probe also reports LOCAL ±/↑ —
    the diff against the branch the host has checked out *now*, as opposed
    to AHEAD's pinned base. It costs no extra round-trip. When the object
    is absent the LOCAL fields come back ``"?"`` and the host tries the
    mirror direction itself (see ``lifecycle._resolve_local_on_host``).

    Any exec failure (non-zero exit, missing binary, timeout) or partial
    output yields all-`?`.
    """
    try:
        raw = incus.exec(
            full_name,
            ["bash", "-c", _PROBE_SNIPPET],
            env={
                "REPO_DIR": repo_dir,
                "BASE_BRANCH": base_branch or "",
                "DEFAULT_BRANCH": default_branch,
                "HOST_HEAD": host_head or "",
                # The probe only reads, but `git diff`/`git diff --cached`/
                # `git submodule foreach 'git diff'` refresh the index and
                # write it back — which takes `.git/index.lock`. A listing
                # (`jailbee ls`, the push picker, the dashboard's periodic
                # refresh) would then race any write in the same container and
                # make `git merge` die with "Unable to create
                # '.git/index.lock': File exists". This tells git to skip the
                # optional lock; the only cost is that the refreshed stat
                # cache is not persisted.
                "GIT_OPTIONAL_LOCKS": "0",
            },
            uid=uid,
            timeout=timeout_s,
        )
    except IncusError:
        return GitStatus(wt="?", ahead_diff="?", ahead_count="?", conflict="?")

    parts = raw.split("\x00")
    if len(parts) < 4:
        return GitStatus(wt="?", ahead_diff="?", ahead_count="?", conflict="?")
    wt_raw, ahead_raw, count_raw, conflict_raw = parts[0], parts[1], parts[2], parts[3]

    wt = parse_shortstat(wt_raw)
    ahead_diff = parse_shortstat(ahead_raw) if ahead_raw.strip() != "?" else "?"
    count_str = count_raw.strip()
    if not count_str or count_str == "?":
        ahead_count = "?"
    elif count_str.isdigit():
        ahead_count = count_str
    else:
        ahead_count = "?"

    conflict_str = conflict_raw.strip()
    if conflict_str in ("ok", "conflict"):
        conflict = conflict_str
    else:
        conflict = "?"

    if len(parts) >= 6:
        submodules = _parse_submodules(parts[4], parts[5])
    else:
        submodules = ()

    if len(parts) >= 10:
        head_sha = parts[6].strip()
        remote_raw = parts[7].strip()
        if remote_raw == "1":
            remote_contained: bool | None = True
        elif remote_raw == "0":
            remote_contained = False
        else:
            remote_contained = None
        local_raw = parts[8]
        local_diff = "?" if local_raw.strip() == "?" else parse_shortstat(local_raw)
        local_count_str = parts[9].strip()
        local_count = local_count_str if local_count_str.isdigit() else "?"
    else:
        head_sha = ""
        remote_contained = None
        local_diff = "?"
        local_count = "?"

    return GitStatus(
        wt=wt,
        ahead_diff=ahead_diff,
        ahead_count=ahead_count,
        conflict=conflict,
        submodules=submodules,
        head_sha=head_sha,
        remote_contained=remote_contained,
        local_diff=local_diff,
        local_count=local_count,
    )


def probe_many_parallel(
    incus: Incus,
    targets: list[tuple[str, str, str | None]],
    default_branch: str,
    *,
    uid: int | None = None,
    max_workers: int = 8,
    timeout_s: int = 3,
    host_head: str | None = None,
) -> dict[str, GitStatus]:
    """Run `probe_container_git` for each (full_name, repo_dir, base_branch) target.

    Returns a dict keyed by full_name. Failures and timeouts are captured
    as all-`?` results so a single broken container does not stall the
    listing. ``host_head`` is resolved once per listing (the host's current
    HEAD does not vary per container) and forwarded unchanged to every
    `probe_container_git` call.
    """
    if not targets:
        return {}

    def _probe(target: tuple[str, str, str | None]) -> tuple[str, GitStatus]:
        full_name, repo_dir, base_branch = target
        status = probe_container_git(
            incus,
            full_name,
            repo_dir,
            base_branch,
            default_branch,
            uid=uid,
            timeout_s=timeout_s,
            host_head=host_head,
        )
        return full_name, status

    worker_count = min(max_workers, len(targets))
    results: dict[str, GitStatus] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        for full_name, status in pool.map(_probe, targets):
            results[full_name] = status
    return results
