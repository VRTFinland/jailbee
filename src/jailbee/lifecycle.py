"""Container lifecycle: new/start/stop/restart/destroy/list/shell."""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee import table_format
from jailbee.config import CONTAINER_USERNAME, Config, HostMount, SharedCache
from jailbee.git import (
    GitFetchError,
    branch_exists_in_source,
    branch_exists_locally,
    count_commits_between,
    diff_shortstat_between,
    fetch_remote_ref,
    get_head_sha,
    has_commit,
    rev_parse,
    rev_parse_remote,
)

# `parse_shortstat` is imported (not reimplemented) so the host-side
# fallback below produces byte-identical `clean` / `+N -M` / `?` strings to
# the container-side probe — duplicating the parser is how the two would drift.
from jailbee.git_status import GitStatus, parse_shortstat, probe_many_parallel
from jailbee.incus import Incus, IncusError
from jailbee.profiles import (
    _device_name_from_path,
    is_under_repo,
    profile_names,
)
from jailbee.retry import with_remote_retry
from jailbee.tui import ConfirmFn, default_confirm, info, warn, warn_plain

if TYPE_CHECKING:
    from jailbee.branch_config import EscalationVerdict
    from jailbee.db.models import BackgroundJob


@dataclass
class ContainerInfo:
    name: str
    state: str
    network: str | None
    ip: str | None
    memory_limit: str | None
    repo: str | None = None
    mode: str = "clone"
    loose_until: datetime | None = None
    base_branch: str | None = None
    repo_dir: str | None = None
    pr_number: int | None = None
    pr_author: bool = False
    created_at: datetime | None = None
    memory_usage: int | None = None
    git_status: GitStatus | None = None
    job_phase: str | None = None
    job_pid: int | None = None
    job_kind: str | None = None
    # Recorded failure message of the job row, so UIs can explain *why* a job
    # failed. Not a `jailbee ls` column — `jailbee job ls` is where errors are listed.
    job_error: str | None = None

    @property
    def display_name(self) -> str:
        """Short name with the ``<repo>-`` prefix stripped, if present."""
        if self.repo and self.name.startswith(f"{self.repo}-"):
            return self.name[len(self.repo) + 1 :]
        return self.name


# Trims sub-microsecond precision that Incus (Go's RFC3339Nano) emits but
# datetime.fromisoformat can't parse: "...123456789Z" -> "...123456Z".
_SUBSEC_RE = re.compile(r"(\.\d{6})\d+")

# Sentinel used to sort containers with no known creation time first (they are
# either mid-creation background rows or legacy containers) under "newest first".
_NEWEST_FIRST = datetime.max.replace(tzinfo=UTC)


def _parse_incus_timestamp(raw: object) -> datetime | None:
    """Parse an Incus ``created_at`` string into an aware datetime, or None.

    Incus reports RFC3339Nano (``2026-07-13T14:30:00.123456789Z``) and uses
    the Go zero time (``0001-01-01T00:00:00Z``) for unset timestamps.
    """
    if not isinstance(raw, str) or not raw:
        return None
    s = _SUBSEC_RE.sub(r"\1", raw.strip())
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return None if dt.year <= 1 else dt


def _resolve_local_on_host(cfg: Config, status: GitStatus) -> GitStatus:
    """Fill in LOCAL ±/↑ from the host when the container could not.

    The container reports ``?`` when it does not hold the host's current
    tip. The host may still hold the *container's* HEAD — any earlier
    ``jailbee git pull`` put it there — so try the mirror direction locally.
    Both directions are object-presence checks; neither fetches, and
    neither writes a ref.

    Returns ``status`` unchanged when there is nothing to do, so the common
    case allocates nothing and runs no subprocess.
    """
    if status.local_diff != "?" or not status.head_sha:
        return status
    if not has_commit(cfg.repo_root, status.head_sha):
        return status
    raw_diff = diff_shortstat_between(cfg.repo_root, "HEAD", status.head_sha)
    raw_count = count_commits_between(cfg.repo_root, "HEAD", status.head_sha)
    if raw_diff is None or raw_count is None:
        # A present object but a failed command is still "unknown" — never
        # let a git failure render as `clean`.
        return status
    return replace(
        status,
        local_diff=parse_shortstat(raw_diff),
        local_count=raw_count if raw_count.isdigit() else "?",
    )


def list_containers(
    cfg: Config,
    incus: Incus,
    *,
    all_repos: bool = False,
    with_git_status: bool = False,
    with_background: bool = False,
    fast: bool = False,
    timeout: int | None = None,
) -> list[ContainerInfo]:
    """Return container infos for jailbee-managed containers.

    By default, filters to containers carrying this repo's <repo>-base
    profile. With ``all_repos=True``, returns containers carrying any
    profile that ends in ``-base`` (heuristic for "jailbee-managed by some
    repo"); the inferred repo prefix is exposed on ``ContainerInfo.repo``.

    ``fast`` and ``timeout`` are forwarded to ``Incus.list_containers``.
    ``fast=True`` leaves ``ip`` and ``memory_usage`` as None because the
    per-instance state is not fetched; callers that only need names (shell
    completion) use it.
    """
    own_names = profile_names(cfg)
    own_net_to_mode = {v: k for k, v in own_names.net_by_mode.items()}

    out: list[ContainerInfo] = []
    for raw in incus.list_containers(fast=fast, timeout=timeout):
        # A container mid-destroy can be reported with "profiles": null, so the
        # `.get(..., [])` default is bypassed (key present, value None) — same
        # trap the state/network chain below guards against with `or {}`.
        profiles = raw.get("profiles") or []

        # Identify the *-base profile (if any) and derive repo prefix from it.
        repo: str | None = None
        for p in profiles:
            if p.endswith("-base") and p != "default":
                repo = p[: -len("-base")]
                break
        if repo is None:
            continue  # not jailbee-managed

        if not all_repos and repo != cfg.container_prefix:
            continue

        # Determine network mode from profile names. For own-repo we have a
        # known mapping; for foreign repos, strip "<repo>-net-" prefix.
        network: str | None = None
        for p in profiles:
            if p in own_net_to_mode:
                network = own_net_to_mode[p]
                break
            prefix = f"{repo}-net-"
            if p.startswith(prefix):
                mode = p[len(prefix) :]
                if mode in ("strict", "loose"):
                    network = mode
                break

        # Extract IP for running containers. Stopped containers have
        # state.network = null (not {}), so the explicit `or {}` chain
        # is required at every level — `.get(key, {})` returns None when
        # the key exists with a None value.
        ip: str | None = None
        state_data = raw.get("state") or {}
        net_state = state_data.get("network") or {}
        eth0 = net_state.get("eth0") or {}
        for addr in eth0.get("addresses", []):
            if addr.get("family") == "inet":
                ip = addr.get("address")
                break

        config = raw.get("config") or {}
        memory_limit = config.get("limits.memory")

        mem_usage_raw = (state_data.get("memory") or {}).get("usage")
        memory_usage = mem_usage_raw if isinstance(mem_usage_raw, int) else None

        mode_value = config.get("user.jailbee.mode") or "clone"

        base_branch_raw = config.get("user.jailbee.base_branch")
        base_branch = (
            base_branch_raw if isinstance(base_branch_raw, str) and base_branch_raw else None
        )

        repo_dir_raw = config.get("user.jailbee.repo_dir")
        repo_dir = repo_dir_raw if isinstance(repo_dir_raw, str) and repo_dir_raw else None

        pr_raw = config.get("user.jailbee.pr")
        pr_number: int | None = None
        if isinstance(pr_raw, str) and pr_raw:
            try:
                pr_number = int(pr_raw)
            except ValueError:
                # Corrupt label — treat as "no PR" rather than raising, like
                # cli._pr_head_for does.
                pr_number = None
        pr_author = config.get("user.jailbee.pr_author") == "1"

        loose_until_raw = config.get("user.jailbee.loose_until")
        loose_until: datetime | None = None
        if isinstance(loose_until_raw, str) and loose_until_raw:
            try:
                loose_until = datetime.fromisoformat(loose_until_raw)
            except ValueError:
                # Treat unparseable timestamps as absent; loose_revert will
                # clean the corrupt label on its next pass.
                loose_until = None

        out.append(
            ContainerInfo(
                name=raw["name"],
                state=raw.get("status", "Unknown"),
                network=network,
                ip=ip,
                memory_limit=memory_limit,
                repo=repo,
                mode=mode_value,
                loose_until=loose_until,
                base_branch=base_branch,
                repo_dir=repo_dir,
                pr_number=pr_number,
                pr_author=pr_author,
                created_at=_parse_incus_timestamp(raw.get("created_at")),
                memory_usage=memory_usage,
            )
        )

    if with_git_status:
        targets: list[tuple[str, str, str | None]] = []
        for c in out:
            if c.state != "Running":
                continue
            if c.mode == "mount":
                continue
            if not c.repo_dir:
                continue
            targets.append((c.name, c.repo_dir, c.base_branch))

        # `get_head_sha` is a real `git rev-parse` on the host, so it stays
        # inside the `if targets` guard: with nothing to probe there is
        # nothing to compare against and the subprocess would be wasted
        # (`probe_many_parallel` early-returns on an empty target list, but
        # its arguments are evaluated first).
        statuses: dict[str, GitStatus] = {}
        if targets:
            statuses = probe_many_parallel(
                incus,
                targets,
                cfg.default_branch,
                uid=cfg.container_user.uid,
                host_head=get_head_sha(cfg.repo_root),
            )
        for c in out:
            status = statuses.get(c.name)
            c.git_status = None if status is None else _resolve_local_on_host(cfg, status)

    if with_background:
        from sqlmodel import Session

        from jailbee import background
        from jailbee.db import get_engine

        with Session(get_engine()) as session:
            ops = background.list_jobs(session, cfg.container_prefix)
        seen = {c.name for c in out}
        for c in out:
            row = ops.get(c.name)
            if row is not None:
                c.job_phase = row.phase
                c.job_pid = row.pid
                c.job_kind = row.op_kind
                c.job_error = row.error_msg
        for name, row in ops.items():
            if name in seen:
                continue
            out.append(
                ContainerInfo(
                    name=name,
                    state="—",
                    network=None,
                    ip=None,
                    memory_limit=None,
                    repo=cfg.container_prefix,
                    job_phase=row.phase,
                    job_pid=row.pid,
                    job_kind=row.op_kind,
                    job_error=row.error_msg,
                )
            )

    # Newest first. Containers with no known creation time (mid-creation
    # background rows, legacy containers) sort ahead of dated ones.
    out.sort(key=lambda c: c.created_at or _NEWEST_FIRST, reverse=True)
    return out


def container_repo_dir(cfg: Config, incus: Incus, name: str) -> str:
    """In-container path where the repo is cloned.

    Reads the per-container ``user.jailbee.repo_dir`` label persisted at
    create time. Pre-feature containers without the label fall back to
    the legacy ``/home/<user>/<repo_root.name>`` path — that is where
    their clone actually lives.
    """
    persisted = incus.config_get(name, "user.jailbee.repo_dir")
    if isinstance(persisted, str) and persisted:
        return persisted
    return f"/home/{CONTAINER_USERNAME}/{cfg.repo_root.name}"


_VALID_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def short_name(cfg: Config, name: str) -> str:
    """User-facing form: strip the ``<container_prefix>-`` prefix if present.

    Returns the input unchanged when the name does not start with this
    repo's prefix (foreign-repo or manually imported containers).
    """
    prefix = f"{cfg.container_prefix}-"
    if name.startswith(prefix):
        return name[len(prefix) :]
    return name


def resolve_container_name(cfg: Config, incus: Incus, name: str) -> str:
    """Resolve a user-supplied container name to its full Incus name.

    1. If a container exists with the exact ``name``, return it.
    2. Else try ``f"{cfg.container_prefix}-{name}"``; return if it exists.
    3. Else raise ValueError listing both attempts.
    """
    if incus.exists(name):
        return name
    prefixed = f"{cfg.container_prefix}-{name}"
    if prefixed != name and incus.exists(prefixed):
        return prefixed
    raise ValueError(f"no such container: '{name}' (also tried '{prefixed}')")


def lookup_background_job(cfg: Config, name: str) -> BackgroundJob | None:
    """Return the in-flight background job row matching a user-typed name.

    Tries the name as-is and with the ``<container_prefix>-`` prefix, so
    ``jailbee tmux feat-foo`` resolves a still-being-created container before
    it exists in Incus. Returns None when no op matches.
    """
    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    prefixed = f"{cfg.container_prefix}-{name}"
    candidates = (name,) if prefixed == name else (name, prefixed)
    with Session(get_engine()) as session:
        ops = background.list_jobs(session, cfg.container_prefix)
    for cand in candidates:
        row = ops.get(cand)
        if row is not None:
            return row
    return None


POLL_INTERVAL_SEC = 0.5


def wait_for_background_ready(
    cfg: Config,
    name: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_phase: Callable[[str], None] | None = None,
) -> None:
    """Block until the background op for ``name`` finishes, or fail fast.

    ``name`` is the full Incus container name. Polls the ``background_op``
    table:

    * row gone          -> ready; return (returns instantly when already done)
    * create + autostart -> ready; return (container is already started, so
      shell/tmux can attach while autostart steps still run — see
      :data:`background.ATTACHABLE_CREATE_PHASES`)
    * phase == failed   -> raise ValueError carrying the recorded error
    * worker pid dead   -> raise ValueError (stale op, worker crashed)

    On each phase change, ``on_phase(phase)`` is invoked so the caller can
    update a spinner. ``sleep`` is injectable for deterministic testing.
    """
    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import BackgroundJob

    engine = get_engine()
    last_phase: str | None = None
    short = short_name(cfg, name)
    while True:
        with Session(engine) as session:
            row = session.get(BackgroundJob, name)
        if row is None:
            return
        if row.op_kind == background.JOB_DESTROY and background.worker_alive(row.pid):
            raise ValueError(f"'{short}' is being destroyed")
        if row.phase in background.TERMINAL_PHASES:
            verb = "destroy" if row.op_kind == background.JOB_DESTROY else "creation"
            raise ValueError(
                f"background {verb} of '{short}' failed: {row.error_msg or 'unknown error'}"
            )
        if not background.worker_alive(row.pid):
            raise ValueError(f"background worker for '{short}' is gone (last phase: {row.phase})")
        if on_phase is not None and row.phase != last_phase:
            on_phase(row.phase)
            last_phase = row.phase
        if (
            row.op_kind == background.JOB_CREATE
            and row.phase in background.ATTACHABLE_CREATE_PHASES
        ):
            return
        sleep(POLL_INTERVAL_SEC)


def derive_container_name(cfg: Config, branch: str) -> str:
    """Derive a valid Incus container name from a branch name.

    Lowercases, replaces any character outside ``[a-z0-9-]`` with ``-``
    (so common refname punctuation like ``/`` and ``#`` survives), strips
    leading dots from each segment, collapses runs of ``-``, trims leading
    and trailing ``-``, and prefixes with ``<container_prefix>-``. Raises
    only when the result is structurally unusable (empty, all dashes).
    """
    sanitized = branch.lower()
    sanitized = "-".join(seg.lstrip(".") for seg in sanitized.split("-"))
    sanitized = re.sub(r"[^a-z0-9-]+", "-", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    full = f"{cfg.container_prefix}-{sanitized}"
    if not _VALID_NAME_RE.match(full):
        raise ValueError(
            f"Branch '{branch}' yields invalid container name: '{full}'. "
            "Use only [a-z0-9-] and ensure first char is alphanumeric."
        )
    return full


@dataclass
class NewContainerOptions:
    # The git branch created or checked out inside the container's clone
    # (clone mode). In --mount mode this is unused ("") — the positional the
    # user typed is a container name, not a branch, and the container name
    # comes from `name` instead.
    container_branch: str
    name: str | None
    network: str
    memory: str
    cpu: int
    from_base: str
    clone: bool
    autostart: bool = True
    mirror_endpoint: tuple[str, int] | None = None
    mirror_ca_path: Path | None = None
    base: str | None = None
    mount: bool = False
    base_branch_label: str | None = None
    # PR number when created via `jailbee new --pr N` (review container).
    # Persisted as the `user.jailbee.pr` label; None for non-PR containers.
    pr: int | None = None
    # The clone's head is code nobody with push access to this repo has
    # vouched for: a PR whose head lives in a fork (`is_cross_repository`).
    # Consulted by the autostart privilege gate — see `EscalationVerdict`.
    #
    # Deliberately not "is a PR": an internal PR's head is a branch in the
    # operator's own origin, byte-identical to what `jailbee new <branch>` would
    # clone and pushed by someone who can already run code in these
    # containers. Gating one spelling and not the other would only teach the
    # operator to click through. MUST be mirrored in
    # `background.op_to_job`/`job_to_opts` — see `assume_yes`.
    untrusted_head: bool = False
    # Exact commit to check `container_branch` out at, bypassing all
    # host branch resolution. Set for PR containers, whose head lives in
    # `refs/jailbee/pr/<N>/head` and deliberately not in any branch (see
    # `pr.pr_head_ref`); a same-named host branch must not win over it.
    clone_commit: str | None = None
    # Suppress interactive confirmations (`jailbee new --yes`). Consulted for the
    # branch-autostart escalation prompt. MUST be mirrored in both
    # `background.op_to_job` and `background.job_to_opts` — a field added to
    # only one side is silently dropped in background `jailbee new`.
    assume_yes: bool = False
    # The ref whose autostart escalation the operator has already accepted, set
    # by the foreground pre-flight of a background `jailbee new`. Narrower than
    # `assume_yes`: the answer only carries while the resolved ref matches.
    # Mirror in `background.op_to_job`/`job_to_opts` — see `assume_yes`.
    approved_autostart_ref: str | None = None
    # The host-side `git fetch` (if any) already happened in the foreground, so
    # the worker must not repeat it: a second fetch costs another round trip —
    # and another hardware-key touch on an SSH remote — and could resolve a
    # newer commit than the one the operator was shown. Mirror in
    # `background.op_to_job`/`job_to_opts` — see `assume_yes`.
    autofetch_done: bool = False


@dataclass(frozen=True)
class CloneRef:
    """Where a clone-mode `jailbee new` starts from, resolved against the host repo.

    Produced by `resolve_clone_ref` and consumed by both `new_container` and the
    foreground pre-flight, so the two can never disagree about which commit the
    container will hold.
    """

    # None in mount / `--no-clone` mode, where no host branch is consulted.
    source_branch: str | None
    create_new_branch: bool
    use_origin_mode: bool
    # The exact commit to check out, when the clone is pinned to one (origin
    # mode, or a caller-supplied `clone_commit` such as a PR head).
    checkout_commit: str | None

    @property
    def autostart_ref(self) -> str | None:
        """The git ref the branch's autostart config is read at.

        A pinned commit when there is one, so the read cannot drift from what
        gets cloned; otherwise the local branch ref.
        """
        if self.source_branch is None:
            return None
        if self.checkout_commit is not None:
            return self.checkout_commit
        return f"refs/heads/{self.source_branch}"

    @property
    def autostart_source_label(self) -> str | None:
        """How `autostart_ref` is named to the user: the full ref for a local
        branch, `<sha12> (<branch>)` for a pinned commit."""
        if self.source_branch is None:
            return None
        if self.checkout_commit is not None:
            return f"{self.checkout_commit[:12]} ({self.source_branch})"
        return f"refs/heads/{self.source_branch}"


def resolve_clone_ref(cfg: Config, opts: NewContainerOptions, *, autofetch: bool) -> CloneRef:
    """Decide what a clone-mode `jailbee new` starts from, fetching if asked to.

    Split out of `new_container` so a background run can resolve — and
    therefore assess and ask about — the very same ref in the foreground,
    before there is a detached worker that cannot be asked anything.

    Raises `ValueError` for the two user-fixable failures: a failed autofetch,
    and a source branch that exists on neither `refs/heads/*` nor
    `refs/remotes/origin/*`.
    """
    # Decide the branch story up front so the user sees it in the first
    # status line, not after `incus init` + start.
    #
    # `opts.base` names the container's *base branch*; forking off it is only
    # implied when `container_branch` does not exist yet. An existing branch is
    # therefore checked first: `jailbee new <existing> <base>` clones the branch as
    # it is and uses `base` purely as the comparison anchor (`base_label`),
    # which is what a review or a hand-off container needs.
    source_branch: str | None = None
    create_new_branch = False
    if opts.clone:
        if opts.clone_commit is not None:
            # The commit *is* the starting point; nothing on the host is
            # consulted, so there is no source branch to speak of.
            source_branch = opts.container_branch
            create_new_branch = False
        elif branch_exists_in_source(cfg.repo_root, cfg.upstream_remote, opts.container_branch):
            source_branch = opts.container_branch
            create_new_branch = False
        elif opts.base is not None:
            source_branch = opts.base
            create_new_branch = True
        else:
            source_branch = cfg.default_branch
            create_new_branch = True

    # Origin-mode triggers in two cases:
    #   1. User prefers origin (`clone_from=origin`) AND we're starting from
    #      cfg.default_branch with no explicit base — the historical case
    #      that clones from the fresh upstream tip rather than the host's
    #      possibly-stale local branch.
    #   2. The chosen source_branch exists only as `refs/remotes/origin/...`
    #      on the host — e.g. the user `git fetch`ed but never checked out
    #      a local copy. A `git clone --shared --branch X` from the bind-
    #      mounted repo would fail because `--branch` only resolves against
    #      `refs/heads/*`. Falling back to origin-mode (clone + checkout -B
    #      <branch> <origin-sha>) is the only way to honour validation that
    #      already accepted the origin-only ref.
    #
    # `opts.clone_commit` is the third, unconditional case: the caller already
    # holds the exact commit (a PR head in `refs/jailbee/pr/<N>/head`), so no host
    # branch — local or remote-tracking, stale or not — may influence the
    # clone, and there is nothing to fetch.
    checkout_commit: str | None = None
    use_origin_mode = False
    if opts.clone and opts.clone_commit is not None:
        checkout_commit = opts.clone_commit
    elif opts.clone:
        assert source_branch is not None
        prefer_origin_default = (
            opts.base is None
            and source_branch == cfg.default_branch
            and cfg.new.clone_from == "origin"
        )
        is_local = branch_exists_locally(cfg.repo_root, source_branch)
        use_origin_mode = prefer_origin_default or not is_local
    if use_origin_mode:
        assert source_branch is not None
        # Bound to a local so the closure below carries the narrowed `str` type
        # rather than the enclosing `str | None`.
        fetch_branch: str = source_branch
        remote = cfg.upstream_remote
        if autofetch:
            info(f"→ Fetching {remote}/{fetch_branch} on host...")
            try:
                # Retry only the host fetch — never container creation.
                with_remote_retry(
                    lambda: fetch_remote_ref(cfg.repo_root, remote, fetch_branch),
                    label=f"fetching {remote}/{fetch_branch}",
                    catch=GitFetchError,
                )
            except GitFetchError as e:
                raise ValueError(
                    f"jailbee new: autofetch of '{remote}/{fetch_branch}' failed: "
                    f"{e.stderr.strip() or e}\n"
                    f"Resolve the underlying issue, or set new.autofetch=false "
                    f"in .jailbee/config.yaml to skip."
                ) from e
        checkout_commit = rev_parse_remote(cfg.repo_root, remote, source_branch)
        if checkout_commit is None:
            raise ValueError(
                f"jailbee new: 'refs/remotes/{remote}/{source_branch}' not found "
                f"in {cfg.repo_root}, and no local `refs/heads/{source_branch}` "
                f"either.\n"
                f"Fetch it first: git fetch {remote} {source_branch}\n"
                f"Or create a local branch: git branch {source_branch} "
                f"{remote}/{source_branch}"
            )
    return CloneRef(
        source_branch=source_branch,
        create_new_branch=create_new_branch,
        use_origin_mode=use_origin_mode,
        checkout_commit=checkout_commit,
    )


@dataclass(frozen=True)
class BranchAutostartAssessment:
    """The outcome of reading the target branch's autostart config.

    `verdict` is None when there was nothing to assess — mount mode,
    `--no-clone`, `--no-autostart`, or a branch that commits no
    `.jailbee/config.yaml` — in which case `effective_cfg` is the host config
    unchanged and nothing needs confirming.
    """

    ref: str | None
    effective_cfg: Config
    verdict: EscalationVerdict | None


def assess_branch_autostart(
    cfg: Config, opts: NewContainerOptions, clone_ref: CloneRef
) -> BranchAutostartAssessment:
    """Read the branch's autostart, report both comparisons, return the verdict.

    Deliberately never prompts: the caller owns that, because *where* the
    question can be asked differs. `new_container` asks inline; a background
    `jailbee new` asks in the foreground before detaching, then hands the answer to
    the worker via `opts.approved_autostart_ref`.

    Skipped entirely without `opts.autostart`: `--no-autostart` runs neither
    `run_autostart` call, so the branch's autostart has no effect and neither a
    diff nor an escalation prompt would be about anything.
    """
    ref = clone_ref.autostart_ref
    label = clone_ref.autostart_source_label
    if not (opts.clone and opts.autostart) or ref is None or label is None:
        return BranchAutostartAssessment(ref=None, effective_cfg=cfg, verdict=None)

    from jailbee import branch_config

    loaded = branch_config.load_branch_autostart(cfg, ref, source_label=label)
    if loaded is None:
        return BranchAutostartAssessment(ref=ref, effective_cfg=cfg, verdict=None)
    if loaded.deviation.any_change:
        # `warn_plain`, not `warn`: step names are trigger-qualified
        # (`on_create[build]`) and the label can hold a branch name like
        # `feat/[wip]`, both of which Rich's markup parser would eat.
        warn_plain(branch_config.format_deviation(loaded.deviation, source=loaded.source))
    # A *separate* comparison, against the repo's reviewed baseline rather than
    # this checkout — see branch_config's module docstring. A checkout that
    # merely lags `origin/<default_branch>` is not a privilege escalation, and
    # used to be treated as one.
    verdict = branch_config.assess_escalation(
        cfg, loaded.cfg.autostart, untrusted=opts.untrusted_head
    )
    if verdict.any_widening:
        warn_plain(branch_config.format_escalation(verdict))
    return BranchAutostartAssessment(ref=ref, effective_cfg=loaded.cfg, verdict=verdict)


def _autostart_approved(opts: NewContainerOptions, ref: str | None) -> bool:
    """True when the operator has already answered the escalation question.

    `--yes` accepts whatever the branch config turns out to say. The pre-flight
    carry is narrower on purpose: it accepts *one* ref, so a worker that ends up
    resolving another commit asks again (and, being detached, fails loudly)
    instead of provisioning a config nobody was shown.
    """
    return opts.assume_yes or (
        opts.approved_autostart_ref is not None and opts.approved_autostart_ref == ref
    )


def new_container(
    cfg: Config,
    incus: Incus,
    opts: NewContainerOptions,
    *,
    on_phase: Callable[[str], None] | None = None,
    confirm_fn: ConfirmFn | None = None,
) -> str:
    """Create a new container from the golden image. Returns container name.

    Steps:
      1. Derive name from branch (or use opts.name)
      2. Verify container does not already exist
      3. ``incus copy`` from golden image
      4. Assign profiles (default + base + binds + selected net)
      5. Set memory and CPU limits
      6. Start the container
      7. (optional) git-clone the repo from /mnt/host-source

    ``on_phase``, if given, is invoked with a short phase label at each
    major boundary — ``"creating"`` (before ``incus init``), ``"cloning"``
    (before the repo clone), and ``"autostart"`` (before autostart). The
    background worker uses it to record progress; the synchronous path
    passes ``None`` and the callback is a no-op.

    ``confirm_fn``, if given, replaces the interactive prompt used when the
    target branch's autostart config widens privileges (network access, or a
    host mount it attaches). The detached background worker injects one that
    always declines, so it never blocks on stdin; tests inject their own.
    """

    def _phase(label: str) -> None:
        if on_phase is not None:
            on_phase(label)

    name = opts.name or derive_container_name(cfg, opts.container_branch)
    if incus.exists(name):
        raise ValueError(f"Container '{name}' already exists")

    if opts.mount:
        if opts.base is not None:
            raise ValueError("base argument is for clone mode; not applicable with --mount.")
        if opts.clone:
            raise ValueError("--mount and clone-mode are mutually exclusive; clone must be False.")
        if not cfg.repo_root.exists():
            raise ValueError(f"repo_root does not exist: {cfg.repo_root}")

    if opts.clone_commit is not None:
        if not opts.clone:
            raise ValueError(
                "clone_commit requires clone (incompatible with --no-clone and --mount)."
            )
        if opts.base is not None:
            raise ValueError(
                "clone_commit and base are mutually exclusive: a raw commit is "
                "already the exact starting point."
            )

    if opts.base is not None:
        if not opts.clone:
            raise ValueError("base argument requires clone (incompatible with --no-clone).")
        if not branch_exists_in_source(cfg.repo_root, cfg.upstream_remote, opts.base):
            raise ValueError(
                f"Base branch '{opts.base}' not found in source repo at "
                f"{cfg.repo_root}. Fetch it first:\n"
                f"  git fetch {cfg.upstream_remote} {opts.base}"
            )

    names = profile_names(cfg)
    if opts.network not in names.net_by_mode:
        raise ValueError(f"Unknown network mode: {opts.network}")

    # Resolving the clone ref is `resolve_clone_ref`'s job — shared with the
    # foreground pre-flight a background `jailbee new` runs before detaching. The
    # locals below keep the rest of this function reading as it did.
    clone_ref = resolve_clone_ref(
        cfg, opts, autofetch=cfg.new.autofetch and not opts.autofetch_done
    )
    source_branch = clone_ref.source_branch
    create_new_branch = clone_ref.create_new_branch
    use_origin_mode = clone_ref.use_origin_mode
    checkout_commit = clone_ref.checkout_commit

    # The branch's own autostart config wins over the host checkout's: the
    # container runs the branch's files, so it must run the branch's startup
    # steps. Everything else stays under host control. Done here, after the
    # clone ref is resolved (so the read is never fetch-stale) and before
    # `_phase("creating")` below, so declining costs nothing.
    assessment = assess_branch_autostart(cfg, opts, clone_ref)
    effective_cfg = assessment.effective_cfg
    verdict = assessment.verdict
    if verdict is not None and verdict.prompts and not _autostart_approved(opts, assessment.ref):
        if opts.approved_autostart_ref is not None:
            # Approved, but for a different commit than this run resolved: the
            # branch moved in between. Carrying the answer over would grant
            # privileges to a config nobody looked at.
            raise ValueError(
                f"Aborted: the target branch moved between the confirmation and "
                f"provisioning (approved {opts.approved_autostart_ref[:12]}, "
                f"resolved {(assessment.ref or '?')[:12]}). Re-run `jailbee new`."
            )
        fn = confirm_fn or default_confirm
        if not fn("Provision with the branch's widened privileges?"):
            raise ValueError(
                "Aborted: declined the target branch's autostart config. "
                "Pass --yes to accept it, or edit the branch's "
                ".jailbee/config.yaml."
            )

    if opts.mount:
        branch_note = f"mount mode, host repo at {cfg.repo_root}"
    elif not opts.clone:
        branch_note = "no clone"
    elif opts.clone_commit is not None:
        branch_note = f"'{opts.container_branch}' at commit {opts.clone_commit[:12]}"
    elif use_origin_mode and create_new_branch:
        branch_note = f"new branch '{opts.container_branch}' off 'origin/{source_branch}'"
    elif use_origin_mode:
        branch_note = f"checking out 'origin/{source_branch}' as '{opts.container_branch}'"
    elif create_new_branch:
        branch_note = f"new branch '{opts.container_branch}' off '{source_branch}'"
    else:
        branch_note = f"cloning existing branch '{opts.container_branch}'"
        if opts.base is not None:
            # The base did not decide the clone (the branch already existed),
            # so name it explicitly — it is what `jailbee ls` / `jailbee git pull` use.
            branch_note += f" (base '{opts.base}')"

    info(
        f"→ Creating '{short_name(cfg, name)}' from base image "
        f"'{opts.from_base}' ({branch_note})..."
    )
    _phase("creating")
    incus.init(opts.from_base, name)
    incus.profile_assign(
        name,
        [
            "default",
            names.base,
            names.binds,
            names.net_by_mode[opts.network],
        ],
    )
    incus.config_set(name, "limits.memory", opts.memory)
    incus.config_set(name, "limits.cpu", str(opts.cpu))

    # Persist the in-container clone path so later commands (shell,
    # tmux, fetch, merge, …) can look it up without re-deriving from
    # config — pre-feature containers without this label fall back to
    # /home/<user>/<repo_root.name>.
    repo_dir = f"/home/{CONTAINER_USERNAME}/{cfg.container_prefix}"
    incus.config_set(name, "user.jailbee.repo_dir", repo_dir)

    if opts.mount:
        incus.config_set(name, "user.jailbee.mode", "mount")
    else:
        incus.config_set(name, "user.jailbee.mode", "clone")
        # Persist the user-provided branch name (not the sanitized short name)
        # so `jailbee git fetch / checkout / merge` can recover it later via
        # `incus.config_get(name, "user.jailbee.branch")`. The container name is
        # a lossy projection of the branch (`/` → `-`, lowercased) and can't
        # be reversed reliably.
        incus.config_set(name, "user.jailbee.branch", opts.container_branch)
        # Surface the branch to processes inside the container so the
        # provisioned bash prompt can render `(<branch>)` next to the cwd.
        # `environment.X` config keys are inherited by `incus exec`, so
        # interactive shells launched via `jailbee shell` see it automatically.
        incus.config_set(name, "environment.JAILBEE_BRANCH", opts.container_branch)

    # `user.jailbee.base_branch`: where this container's work conceptually
    # branches from. Read by `jailbee ls` / pickers. Precedence: explicit
    # label override (e.g. PR's baseRefName) → the user-named `base` (which
    # is the base whether or not we forked off it) → the source branch we
    # cloned from → cfg.default_branch (mount / --no-clone paths have
    # no real clone source).
    base_label = opts.base_branch_label or opts.base
    if base_label is None:
        base_label = source_branch if opts.clone and source_branch else cfg.default_branch
    incus.config_set(name, "user.jailbee.base_branch", base_label)

    # `user.jailbee.pr`: the PR number for a `jailbee new --pr N` review container,
    # read back by `jailbee ls` / `jailbee pr`. Written here — alongside the other
    # metadata labels and *before* autostart — so it survives an autostart
    # step failure (the container is left running for debugging and must keep
    # its PR association). Best-effort: unlike branch/mode this is display-only
    # metadata, so a set failure warns but must not abort an otherwise-good
    # container.
    if opts.pr is not None:
        try:
            incus.config_set(name, "user.jailbee.pr", str(opts.pr))
        except Exception as e:
            warn(f"Container created, but failed to set PR label: {e}")

    # Repo source bind (RO). Per-container rather than in the
    # `<prefix>-binds` profile so multiple clones of the same upstream
    # sharing a `container_prefix` each get their own clone source.
    incus.config_device_add(
        name,
        "host-source",
        "disk",
        {
            "source": str(cfg.repo_root),
            "path": "/mnt/host-source",
            "readonly": "true",
        },
    )

    if opts.mount:
        incus.config_device_add(
            name,
            "host-repo-rw",
            "disk",
            {
                "source": str(cfg.repo_root),
                "path": repo_dir,
            },
        )

    incus.start(name)

    # Attach /run/user/<uid>/* GUI sockets after logind has provisioned
    # the dev-owned tmpfs at /run/user/<uid> — see the runtime_mounts
    # module docstring for why this can't be a profile device.
    from jailbee.runtime_mounts import attach_runtime_devices

    attach_runtime_devices(cfg, incus, name)

    # Add the dev user to the groups owning any host_devices nodes (e.g.
    # `kvm` for /dev/kvm). A device with a udev `static_node` rule gets
    # reset to its distro default (root:kvm 0660) by the container's
    # systemd-udevd regardless of the profile `mode`, so group membership —
    # not `mode` — is what grants `dev` access. Runs before autostart so
    # those steps' sessions pick up the group. See device_groups module.
    from jailbee.device_groups import ensure_device_groups

    ensure_device_groups(cfg, incus, name)

    # Attach the repo's `host_ports` forwards (Incus proxy devices) before
    # autostart, so a step can use a forwarded host service — an adb command
    # or a database client is exactly the case this exists for. Proxy devices
    # hotplug, so this needs no restart.
    if cfg.host_ports:
        from jailbee.ports import PortError, attach_config_ports

        # Warn and continue rather than let this abort `new_container`: the
        # container is already created and started at this point, so it is
        # usable regardless, and `jailbee apply` will attach the forward on
        # its next run (`reconcile_config_ports` treats a missing `port-cfg-*`
        # device the same as any other drift).
        try:
            attached = attach_config_ports(cfg, incus, name)
        except PortError as e:
            warn_plain(
                f"Could not attach port forward(s) to {short_name(cfg, name)}: {e}\n"
                f"  The container is usable; run `jailbee apply` to retry the forward."
            )
        else:
            if attached:
                info(f"Attached {len(attached)} port forward(s) to {short_name(cfg, name)}")

    # Pin /etc/hosts for strict profile so the container's resolver sees
    # the ACL'd IPs before autostart's first network use. Must run after
    # `incus.start` because `apply_hosts` uses `incus exec`.
    # `mirror_endpoint` also pins jailbee-registry-mirror.incus because
    # incusbr0's dnsmasq doesn't know about the mirror on jailbee-loose.
    if opts.network == "strict":
        from jailbee.hosts import apply_hosts

        apply_hosts(cfg, incus, name, mirror_endpoint=opts.mirror_endpoint)

    # Wire dockerd to the registry mirror via HTTPS_PROXY. Strict
    # mode needs the proxy to reach upstreams under its ACL; loose mode
    # gets it too for caching. None endpoint OR None CA path means caller
    # didn't compute them (e.g. unit tests, mirror disabled globally) —
    # no-op.
    if opts.mirror_endpoint is not None and opts.mirror_ca_path is not None:
        from jailbee.docker_daemon import apply_docker_proxy
        from jailbee.registry import apply_mirror_registries

        # Push per-repo extra upstreams (e.g. ECR) into the mirror's
        # REGISTRIES before dockerd starts using the proxy, so the first
        # pull through HTTPS_PROXY already hits the cache path. No-op if
        # the list is empty or every entry is already configured.
        apply_mirror_registries(incus, cfg.docker_registry_mirror.extra_registries)

        _, port = opts.mirror_endpoint
        ca_pem = opts.mirror_ca_path.read_text()
        apply_docker_proxy(incus, name, ca_cert_pem=ca_pem, port=port)

    if opts.clone:
        assert source_branch is not None
        _phase("cloning")
        _clone_repo_in_container(
            cfg,
            incus,
            name,
            opts.container_branch,
            source_branch=source_branch,
            create_new_branch=create_new_branch,
            repo_target=repo_dir,
            base_branch=base_label,
            checkout_commit=checkout_commit,
        )

    # Attach host_mounts whose container path is under /home/<user>/<repo>/.
    # These cannot live in the <prefix>-binds profile: Incus pre-creates the
    # mount target at `incus start`, which pre-populates the clone destination
    # and breaks `git clone`. Attaching them after the clone (or after start
    # in mount / --no-clone mode) sidesteps that.
    _attach_under_repo_host_mounts(cfg, incus, name)

    # Auto-share <repo>/.local (RW) for host<->container file transfer.
    # Mount-mode is excluded: host-repo-rw already exposes it. Must follow
    # the clone for the same reason as under-repo host_mounts above.
    if not opts.mount:
        _attach_share_local(cfg, incus, name, repo_dir=repo_dir, clone=opts.clone)

    # Same trick for under-repo shared_caches (e.g. jetbrains-idea), but
    # only in clone mode. In --mount mode the host-repo-rw bind already
    # covers /home/<user>/<container_prefix>/, and the host's own .idea
    # is what the user wants live — don't shadow it with the shared cache.
    if not opts.mount:
        _attach_under_repo_shared_caches(cfg, incus, name)

    # Install/update every enabled agent before autostart execs them. Must
    # come after mounts are attached (each agent's shared cache, e.g.
    # claude-install, provides its persistent store) and after the network
    # ACL warm-up, so installers/updaters can reach their egress hosts.
    from jailbee.agents import ensure_agents

    ensure_agents(cfg, incus, name, repo_dir, mirror_endpoint=opts.mirror_endpoint)

    # Sync jailbee's own skills into the shared ~/.claude/skills so the
    # in-container Claude understands jailbee and can help with .jailbee/config.yaml
    # edits. Host-side file copy; no-op unless claude.enabled and
    # install_jailbee_skills. Non-fatal — never block container creation.
    from jailbee.claude_skills import sync_jailbee_skills

    try:
        sync_jailbee_skills(cfg)
    except Exception as e:  # non-fatal
        warn(f"jailbee-skills sync failed (continuing): {e}")

    # GH_TOKEN injection is auto-enabled infrastructure, not a user autostart
    # command — write it regardless of --no-autostart so `gh` works in every
    # container (mirrors the agent install/update above). No-op when the
    # github integration is off or no token applies.
    #
    # Deliberately `cfg`, not `effective_cfg`: this is jailbee's own step, and
    # `_apply_step` merges `cfg.autostart.env` (PATH included) into every step's
    # environment. The step pipes the host's GitHub PAT through `sudo tee`, and
    # the fork's tree is already cloned in by now, so the branch's `autostart`
    # must not reach it. `inject_github_token` needs nothing from `autostart`.
    from jailbee.autostart import inject_github_token

    inject_github_token(cfg, incus, name, repo_dir, mirror_endpoint=opts.mirror_endpoint)

    if opts.autostart:
        _phase("autostart")
        from jailbee.autostart import AutostartTrigger, run_autostart

        short = short_name(cfg, name)
        info(
            f"→ Running autostart "
            f"[dim](tip: 'jailbee tmux {short}' in another terminal to follow live)[/dim]"
        )
        run_autostart(
            effective_cfg,
            incus,
            name,
            AutostartTrigger.ON_CREATE,
            repo_dir=repo_dir,
            mirror_endpoint=opts.mirror_endpoint,
        )
        # `incus.start` above transitioned the container into the running
        # state, so on_start steps apply on this first launch too.
        run_autostart(
            effective_cfg,
            incus,
            name,
            AutostartTrigger.ON_START,
            repo_dir=repo_dir,
            mirror_endpoint=opts.mirror_endpoint,
        )

    return name


def _under_repo_host_mounts(cfg: Config) -> list[HostMount]:
    """Return the effective host_mounts whose container path is under the
    container's repo dir. These can't be attached via the binds profile —
    see ``profiles.is_under_repo``.
    """
    return [m for m in cfg.effective_host_mounts() if is_under_repo(m.container, cfg)]


def _attach_under_repo_host_mounts(cfg: Config, incus: Incus, name: str) -> None:
    """Attach each under-repo host_mount as a per-container disk device.

    Device names share the ``host-<derived>`` namespace with the binds
    profile's entries; collisions are not expected because the profile
    excludes the same set of mounts (``profiles.binds_profile_yaml``).
    """
    for mount in _under_repo_host_mounts(cfg):
        device_name = "host-" + _device_name_from_path(mount.host)
        props: dict[str, str] = {
            "source": str(mount.host),
            "path": mount.container,
        }
        if mount.readonly:
            props["readonly"] = "true"
        incus.config_device_add(name, device_name, "disk", props)


def _attach_share_local(
    cfg: Config, incus: Incus, name: str, *, repo_dir: str, clone: bool
) -> None:
    """Attach the auto-shared ``<repo>/.local`` dir as a RW disk device.

    No-op unless ``share_local`` is enabled and ``<repo_root>/.local`` exists
    (see ``Config.share_local_mount``). Mount-mode is handled by the caller —
    the ``host-repo-rw`` bind already exposes ``.local`` there.

    Must run AFTER the clone: a profile/early device would pre-create the
    mount target and break ``git clone`` (same constraint as the other
    under-repo mounts). When a clone is present, ``/.local/`` is appended to
    the clone's ``.git/info/exclude`` (idempotently) so the mount doesn't show
    up as untracked in ``jailbee ls`` / ``jailbee git diff``.
    """
    mount = cfg.share_local_mount()
    if mount is None:
        return
    incus.config_device_add(
        name,
        "share-local",
        "disk",
        {"source": str(mount.host), "path": mount.container},
    )
    if clone:
        incus.exec(
            name,
            [
                "bash",
                "-lc",
                "grep -qxF '/.local/' .git/info/exclude 2>/dev/null "
                "|| echo '/.local/' >> .git/info/exclude",
            ],
            uid=cfg.container_user.uid,
            cwd=repo_dir,
        )


def _under_repo_shared_caches(cfg: Config) -> list[tuple[SharedCache, str]]:
    """Return effective shared_caches whose resolved container path is
    under /home/<user>/<container_prefix>/.

    The resolved path (with ``~`` expanded) is returned alongside the
    cache so the caller doesn't repeat the expansion.
    """
    home = f"/home/{CONTAINER_USERNAME}"
    result: list[tuple[SharedCache, str]] = []
    for cache in cfg.effective_shared_caches():
        path = (
            cache.container_path.replace("~", home, 1)
            if cache.container_path.startswith("~")
            else cache.container_path
        )
        if is_under_repo(path, cfg):
            result.append((cache, path))
    return result


def _attach_under_repo_shared_caches(cfg: Config, incus: Incus, name: str) -> None:
    """Attach each under-repo shared_cache as a per-container disk device.

    Mirrors ``_attach_under_repo_host_mounts`` for the shared-cache list.
    Must run AFTER ``git clone``, same reason: profile-level disks would
    pre-create the mount target and break the clone.

    Creates the source path on the host if it doesn't exist yet. Init/apply
    own that lifecycle (and the user-visible "create the empty subdir"
    semantics), but this defensive mkdir lets ``jailbee new`` succeed even on
    upgrades where the user hasn't re-run ``jailbee apply`` yet — Incus
    ``config device add`` validates the source path and fails the create
    otherwise.
    """
    assert cfg.shared_dir is not None  # set by load_config
    shared_root = cfg.shared_dir
    for cache, path in _under_repo_shared_caches(cfg):
        source = shared_root / cache.host_subpath
        source.mkdir(parents=True, exist_ok=True)
        incus.config_device_add(
            name,
            f"shared-{cache.name}",
            "disk",
            {
                "source": str(source),
                "path": path,
            },
        )


def _clone_repo_in_container(
    cfg: Config,
    incus: Incus,
    container: str,
    branch: str,
    *,
    source_branch: str,
    create_new_branch: bool,
    repo_target: str,
    base_branch: str,
    checkout_commit: str | None = None,
) -> None:
    """Clone the repo from /mnt/host-source into ``repo_target``.

    Two modes:

    * **Commit mode** (``checkout_commit`` set): clone without ``--branch``
      and then ``git checkout -B <branch> <checkout_commit>``. This lands
      the working tree at exactly that commit — the host's
      ``refs/remotes/origin/<source_branch>`` tip, or a PR head the caller
      resolved — regardless of what any host branch of that name points at.
      The object is available via the ``--shared`` alternates pointing at
      the host repo's object store, so no network fetch is involved.

    * **Local mode** (``checkout_commit`` is None): clone with
      ``--branch <source_branch>``, which resolves to the host's local
      ``refs/heads/<source_branch>``. If ``create_new_branch`` is True,
      ``git checkout -b <branch>`` afterwards so the working tree ends
      up on the requested (new) branch; otherwise ``source_branch ==
      branch`` and the initial clone already checks it out.

    After the clone, rewrites `origin` to the host repo's real upstream URL
    (so `git push`/`fetch` reach the actual remote when network ACL allows)
    and explicitly sets `branch.<branch>.remote`/`merge` so the first
    `git push` works without `-u`.
    """
    uid = cfg.container_user.uid
    gid = cfg.container_user.gid

    if checkout_commit is not None:
        incus.exec(
            container,
            [
                "git",
                "clone",
                "--shared",
                "/mnt/host-source",
                repo_target,
            ],
            uid=uid,
            gid=gid,
        )
        incus.exec(
            container,
            ["git", "-C", repo_target, "checkout", "-B", branch, checkout_commit],
            uid=uid,
            gid=gid,
        )
    else:
        incus.exec(
            container,
            [
                "git",
                "clone",
                "--shared",
                "--branch",
                source_branch,
                "/mnt/host-source",
                repo_target,
            ],
            uid=uid,
            gid=gid,
        )
        if create_new_branch:
            incus.exec(
                container,
                ["git", "-C", repo_target, "checkout", "-b", branch],
                uid=uid,
                gid=gid,
            )

    _wire_origin_and_tracking(cfg, incus, container, branch, repo_target, uid=uid, gid=gid)

    # Seed the jailbee-managed base ref so `jailbee ls` AHEAD has a stable, fetch-proof
    # comparison point from day one. The container is a `git clone` of the host
    # repo, which carries only the host's local refs/heads/*; the base branch
    # (e.g. a PR's baseRefName) commonly exists on the host *only* as
    # refs/remotes/origin/<base>, so it is absent inside the container. Resolve
    # the base tip on the HOST and update-ref to that raw SHA — the object is
    # reachable through the --shared alternates, so no network fetch is needed.
    # Without this the probe's BASE resolution falls through to
    # origin/<default_branch> and reports AHEAD against the wrong branch.
    base_sha = rev_parse_remote(cfg.repo_root, cfg.upstream_remote, base_branch) or rev_parse(
        cfg.repo_root, f"refs/heads/{base_branch}"
    )
    if base_sha is not None:
        try:
            incus.exec(
                container,
                [
                    "git",
                    "-C",
                    repo_target,
                    "update-ref",
                    f"refs/jailbee/base/{base_branch}",
                    base_sha,
                ],
                uid=uid,
                gid=gid,
            )
        except IncusError:
            pass

    if cfg.new.submodules:
        from jailbee.submodules import init_submodules_in_container

        init_submodules_in_container(
            incus,
            container,
            repo_dir=repo_target,
            uid=uid,
            gid=gid,
            branch=branch,
            base_branch=base_branch,
        )


def _wire_origin_and_tracking(
    cfg: Config,
    incus: Incus,
    container: str,
    branch: str,
    repo_target: str,
    *,
    uid: int,
    gid: int,
) -> None:
    """Rewrite `origin` to upstream URL + set `branch.<branch>` tracking config.

    The container's remote is always named `origin`; only the `merge` ref is
    read from the host. See the comment at the tracking write below.

    Origin rewrite is skipped if the host repo has no `origin` remote (the
    in-container clone retains `/mnt/host-source` as origin — fallback).
    Branch tracking mirrors the host repo's config when present; otherwise
    defaults to `origin` + `refs/heads/<branch>` so `git push` Just Works
    once the user toggles loose-mode for the push.
    """
    from jailbee.git import get_branch_tracking, get_remote_url

    upstream = get_remote_url(cfg.repo_root, cfg.upstream_remote)
    if upstream is not None:
        incus.exec(
            container,
            ["git", "-C", repo_target, "remote", "set-url", "origin", upstream],
            uid=uid,
            gid=gid,
        )

    # The remote name is jailbee's own invariant, never the host's: the clone
    # above is `git clone --shared /mnt/host-source`, whose only remote is
    # `origin`. A host that calls its upstream something else would otherwise
    # leave the container tracking a remote that does not exist there. Only
    # `merge` is host-derived — that is a ref name on the shared upstream.
    tracking = get_branch_tracking(cfg.repo_root, branch)
    merge_ref = tracking[1] if tracking is not None else f"refs/heads/{branch}"
    incus.exec(
        container,
        ["git", "-C", repo_target, "config", f"branch.{branch}.remote", "origin"],
        uid=uid,
        gid=gid,
    )
    incus.exec(
        container,
        ["git", "-C", repo_target, "config", f"branch.{branch}.merge", merge_ref],
        uid=uid,
        gid=gid,
    )


def restart_container(cfg: Config, incus: Incus, name: str) -> None:
    """Restart a container, re-attaching GUI sockets in the right order.

    Detach happens *before* the reboot so the four /run/user/<uid>/*
    socket devices don't race with logind's tmpfs creation on next boot.
    Attach happens after restart returns, by which time PID 1 + logind
    are running. See the runtime_mounts module docstring.
    """
    from jailbee.runtime_mounts import (
        attach_runtime_devices,
        detach_runtime_devices,
    )

    state = "Stopped"
    for raw in incus.list_containers():
        if raw["name"] == name:
            state = raw.get("status", "Stopped")
            break

    detach_runtime_devices(cfg, incus, name)
    if state == "Running":
        incus.restart(name)
    else:
        incus.start(name)
    attach_runtime_devices(cfg, incus, name)


def destroy_container(
    cfg: Config,
    incus: Incus,
    name: str,
    *,
    force: bool,
    on_phase: Callable[[str], None] | None = None,
) -> None:
    """Stop (if running), release Chrome pool slot, clean refs/jailbee/*, and delete.

    ``on_phase``, if given, is invoked with ``"stopping"`` before
    ``incus.stop`` (only when the container is running) and ``"deleting"``
    before ``incus.delete``. ``on_phase=None`` leaves behaviour unchanged.
    """

    def _phase(label: str) -> None:
        if on_phase is not None:
            on_phase(label)

    if not incus.exists(name):
        raise ValueError(f"Container '{name}' does not exist")

    state = "Stopped"
    for raw in incus.list_containers():
        if raw["name"] == name:
            state = raw.get("status", "Stopped")
            break

    if state == "Running":
        _phase("stopping")
        incus.stop(name, force=force)

    # Release Chrome pool slot before deleting
    from jailbee.chrome_pool import release as chrome_pool_release

    chrome_pool_release(cfg, incus, name)

    # Clean refs/jailbee/<short>/* on the host. Best-effort: a failure here
    # (git missing, repo broken) must not block destroy — leftover refs
    # are not visible in `git branch` output, so leakage is cosmetic.
    try:
        from jailbee import git as git_helpers

        short = short_name(cfg, name)
        for ref in git_helpers.list_refs(cfg.repo_root, f"refs/jailbee/{short}/"):
            git_helpers.delete_ref(cfg.repo_root, ref)
        from jailbee.submodules import prune_host_submodule_refs

        prune_host_submodule_refs(cfg, short)
    except Exception:
        pass

    _phase("deleting")
    incus.delete(name, force=force)

    # Drop any background job tracking row so `jailbee ls` stops showing it.
    # Best-effort: a DB hiccup must not turn a successful destroy into a
    # failure.
    try:
        from sqlmodel import Session

        from jailbee import background
        from jailbee.db import get_engine

        with Session(get_engine()) as session:
            background.delete_job(session, name)
    except Exception:
        pass


def current_network_mode(
    cfg: Config,
    incus: Incus,
    name: str,
) -> str | None:
    """Return the container's current network mode ('strict'/'loose').

    Returns ``None`` if the container has no recognised jailbee-managed net
    profile attached (e.g. brand-new container before init, or user-
    customised profiles).
    """
    names = profile_names(cfg)
    mode_by_profile = {v: k for k, v in names.net_by_mode.items()}
    for raw in incus.list_containers():
        if raw["name"] == name:
            for p in raw["profiles"]:
                if p in mode_by_profile:
                    return mode_by_profile[p]
            return None
    return None


def switch_network(
    cfg: Config,
    incus: Incus,
    name: str,
    mode: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    """Replace the container's network profile with <repo>-net-<mode>.

    `mirror_endpoint=(ip, port)` is forwarded to ``apply_hosts`` when
    switching to strict, so the jailbee-registry-mirror.incus row stays
    pinned in /etc/hosts (the mirror lives on jailbee-loose, but strict
    containers query incusbr0's dnsmasq).
    """
    names = profile_names(cfg)
    if mode not in names.net_by_mode:
        raise ValueError(f"Unknown network mode: {mode}")

    target_profile = names.net_by_mode[mode]
    own_net_profiles = set(names.net_by_mode.values())
    new_profiles: list[str] = []
    found_net = False
    for raw in incus.list_containers():
        if raw["name"] == name:
            for p in raw["profiles"]:
                if p in own_net_profiles:
                    new_profiles.append(target_profile)
                    found_net = True
                else:
                    new_profiles.append(p)
            break
    else:
        raise ValueError(f"Container '{name}' not found")

    if not found_net:
        raise ValueError(f"Container '{name}' has no network profile attached — cannot switch.")

    incus.profile_assign(name, new_profiles)

    # Keep /etc/hosts in sync with the new profile. Strict mode pins
    # allowlisted hostnames so the container sees the same IPs the ACL
    # enforces; loose restores normal DNS resolution.
    from jailbee.hosts import apply_hosts, clear_hosts

    if mode == "strict":
        apply_hosts(cfg, incus, name, mirror_endpoint=mirror_endpoint)
    else:
        clear_hosts(cfg, incus, name)


def _stdin_is_interactive() -> bool:
    return sys.stdin.isatty() and not os.environ.get("JAILBEE_NONINTERACTIVE")


def _default_picker(containers: list[ContainerInfo]) -> str | None:
    from jailbee.tui import pick_container

    return pick_container(containers)


@dataclass(frozen=True)
class ResolvedContainer:
    """A resolved container name plus how it was chosen.

    ``auto_selected`` is True only when the caller passed no name *and* the
    picker never ran — i.e. jailbee silently settled on the single candidate. That
    is the case the bridge commands confirm before mutating anything, because
    nothing on screen told the user which container (or which branch) they were
    about to hit.
    """

    name: str
    auto_selected: bool


def resolve_container_for_interactive_detailed(
    cfg: Config,
    incus: Incus,
    name: str | None,
    *,
    picker: Callable[[list[ContainerInfo]], str | None] = _default_picker,
    is_interactive: Callable[[], bool] = _stdin_is_interactive,
    with_background: bool = False,
) -> ResolvedContainer:
    """Resolve a container name, reporting whether jailbee chose it unprompted.

    Same behaviour as :func:`resolve_container_for_interactive` — that is now a
    thin wrapper — plus the ``auto_selected`` flag callers need to decide
    whether to confirm the operation.

    When ``with_background`` is set, a named lookup that finds no live
    container falls back to an in-flight ``jailbee new --background`` op of the
    same name, and the picker includes in-flight-only rows.
    """
    if name is not None:
        try:
            return ResolvedContainer(
                name=resolve_container_name(cfg, incus, name), auto_selected=False
            )
        except ValueError:
            if with_background:
                row = lookup_background_job(cfg, name)
                if row is not None:
                    return ResolvedContainer(name=row.container_name, auto_selected=False)
            raise

    containers = list_containers(cfg, incus, with_git_status=True, with_background=with_background)
    if not containers:
        raise ValueError(f"no managed containers found for repo '{cfg.container_prefix}'")
    if len(containers) == 1:
        return ResolvedContainer(name=containers[0].name, auto_selected=True)
    if is_interactive():
        chosen = picker(containers)
        if chosen is None:
            raise ValueError("cancelled")
        return ResolvedContainer(name=chosen, auto_selected=False)
    names = ", ".join(c.display_name for c in containers)
    raise ValueError(
        f"multiple containers exist; specify <name> explicitly (or run in a TTY): {names}"
    )


def resolve_container_for_interactive(
    cfg: Config,
    incus: Incus,
    name: str | None,
    *,
    picker: Callable[[list[ContainerInfo]], str | None] = _default_picker,
    is_interactive: Callable[[], bool] = _stdin_is_interactive,
    with_background: bool = False,
) -> str:
    """Resolve a container name; auto-pick or prompt if name is omitted.

    Thin wrapper over :func:`resolve_container_for_interactive_detailed` for the
    callers that do not care how the container was chosen.
    """
    return resolve_container_for_interactive_detailed(
        cfg,
        incus,
        name,
        picker=picker,
        is_interactive=is_interactive,
        with_background=with_background,
    ).name


def _format_bytes(num: int) -> str:
    """Compact binary size: 4_000_000_000 -> '3.7G', 0 -> '0B'."""
    size = float(num)
    units = ("B", "K", "M", "G", "T", "P")
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}P"  # unreachable; satisfies type checker


def format_duration_short(delta: timedelta) -> str:
    """Render a duration compactly: ``4h``, ``3h 59m``, ``12m``, ``45s``.

    Truncates rather than rounds (a 2m30s remainder reads ``2m``), and clamps
    a non-positive delta to ``0s``. Public because `dashboard.py` and
    `cli.py` render the same loose TTL.
    """
    total = max(0, int(delta.total_seconds()))
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def repo_has_submodules(cfg: Config) -> bool:
    """True iff the repo declares submodules (a ``.gitmodules`` file exists)."""
    return (cfg.repo_root / ".gitmodules").exists()


def _sub_stat_str(ins: int, dels: int) -> str:
    """Render an ``(ins, del)`` pair as the table shows it."""
    if ins == 0 and dels == 0:
        return "clean"
    return f"+{ins} -{dels}"


def submodule_sub_rows(c: ContainerInfo) -> list[dict[str, str]]:
    """Continuation rows (one per changed submodule) for ``jailbee ls``.

    Keys match the ``ls`` field names (``name``/``wt``/``ahead_diff``/
    ``ahead_count``); other columns render blank. Returns ``[]`` when the
    container has no git status or no submodule changes.
    """
    if c.git_status is None:
        return []
    rows: list[dict[str, str]] = []
    for s in c.git_status.submodules:
        rows.append(
            {
                "name": f"  └ {s.path}",
                "wt": _sub_stat_str(s.wt_ins, s.wt_del),
                "ahead_diff": _sub_stat_str(s.ahead_ins, s.ahead_del),
                "ahead_count": str(s.ahead_commits) if s.ahead_commits else "",
            }
        )
    return rows


def ls_field_specs(
    *, now: datetime, all_repos: bool = False, show_submodules: bool = False
) -> list[table_format.FieldSpec[ContainerInfo]]:
    """Build the FieldSpec list for ``jailbee ls``.

    ``all_repos`` flips the REPO column on by default; users can also
    request it explicitly via ``--fields``.
    """

    def _ttl_cell(c: ContainerInfo) -> str:
        if c.network != "loose":
            return ""
        if c.loose_until is None:
            return "—"
        return format_duration_short(c.loose_until - now)

    def _ttl_json(c: ContainerInfo) -> int | None:
        if c.network != "loose" or c.loose_until is None:
            return None
        delta = c.loose_until - now
        return max(0, int(delta.total_seconds() // 60))

    def _git_cell(attr: str, zero_dim: bool = False) -> Callable[[ContainerInfo], str]:
        def cell(c: ContainerInfo) -> str:
            if c.git_status is None:
                return "[dim]—[/dim]"
            v: str = getattr(c.git_status, attr)
            if v == "clean":
                return "[dim]clean[/dim]"
            if v == "?":
                return "[yellow]?[/yellow]"
            if zero_dim and v == "0":
                return "[dim]0[/dim]"
            return v

        return cell

    def _git_json(attr: str) -> Callable[[ContainerInfo], str | None]:
        def get(c: ContainerInfo) -> str | None:
            if c.git_status is None:
                return None
            return getattr(c.git_status, attr)  # type: ignore[no-any-return]

        return get

    def _conflict_cell(c: ContainerInfo) -> str:
        if c.git_status is None:
            return "[dim]—[/dim]"
        v = c.git_status.conflict
        if v == "ok":
            return "[dim]ok[/dim]"
        if v == "conflict":
            return "[red]conflict[/red]"
        return "[yellow]?[/yellow]"

    def _git_status_json(c: ContainerInfo) -> dict[str, object] | None:
        if c.git_status is None:
            return None
        payload: dict[str, object] = {
            "wt": c.git_status.wt,
            "ahead_diff": c.git_status.ahead_diff,
            "ahead_count": c.git_status.ahead_count,
            "conflict": c.git_status.conflict,
            "head_sha": c.git_status.head_sha,
            "remote_contained": c.git_status.remote_contained,
            "local_diff": c.git_status.local_diff,
            "local_count": c.git_status.local_count,
        }
        if show_submodules:
            payload["submodules"] = [
                {
                    "path": s.path,
                    "ahead_ins": s.ahead_ins,
                    "ahead_del": s.ahead_del,
                    "ahead_commits": s.ahead_commits,
                    "wt_ins": s.wt_ins,
                    "wt_del": s.wt_del,
                    "status": s.status,
                }
                for s in c.git_status.submodules
            ]
        return payload

    def _job_dead(c: ContainerInfo) -> bool:
        """True when nothing is progressing for this row's job.

        Kept separate from the label text: `background.job_label` can now
        render a live `destroy`-kind job in `starting` as `"destroying"`, so
        `label != phase` is no longer a valid deadness test — this uses
        `background.clearable` instead (falling back to bare terminal-phase
        membership for a pid-less row, which is only reachable from a
        hand-built ContainerInfo; every populated row has a non-null pid).
        """
        from jailbee import background

        assert c.job_phase is not None
        if c.job_pid is None:
            return c.job_phase in background.TERMINAL_PHASES
        return background.clearable(c.job_phase, c.job_pid)

    def _job_cell(c: ContainerInfo) -> str:
        from jailbee import background

        if c.job_phase is None:
            return ""
        label = background.job_label_or_empty(c.job_phase, c.job_pid, kind=c.job_kind)
        if _job_dead(c):
            return f"[red]{label}[/red]"
        return f"[yellow]{label}[/yellow]"

    def _job_json(c: ContainerInfo) -> str | None:
        from jailbee import background

        if c.job_phase is None:
            return None
        return background.job_label_or_empty(c.job_phase, c.job_pid, kind=c.job_kind)

    def _created_cell(c: ContainerInfo) -> str:
        if c.created_at is None:
            return "[dim]—[/dim]"
        return c.created_at.astimezone().strftime("%Y-%m-%d %H:%M")

    def _mem_cell(c: ContainerInfo) -> str:
        if c.state != "Running" or c.memory_usage is None:
            return c.memory_limit or "[dim]—[/dim]"
        used = _format_bytes(c.memory_usage)
        return f"{used} / {c.memory_limit}" if c.memory_limit else used

    def _pr_cell(c: ContainerInfo) -> str:
        if c.pr_number is None:
            return ""
        return f"#{c.pr_number}" if c.pr_author else f"#{c.pr_number}↓"

    def _pr_json(c: ContainerInfo) -> dict[str, object] | None:
        if c.pr_number is None:
            return None
        return {"number": c.pr_number, "role": "author" if c.pr_author else "review"}

    return [
        table_format.FieldSpec(
            name="name",
            header="NAME",
            cell=lambda c: c.display_name,
            json=lambda c: c.display_name,
        ),
        table_format.FieldSpec(
            name="full_name",
            header="FULL NAME",
            cell=lambda c: c.name,
            json=lambda c: c.name,
            default_table=False,
            default_json=False,
        ),
        table_format.FieldSpec(
            name="repo",
            header="REPO",
            cell=lambda c: c.repo or "-",
            json=lambda c: c.repo,
            default_table=all_repos,
            default_json=all_repos,
        ),
        table_format.FieldSpec(
            name="mode",
            header="MODE",
            cell=lambda c: c.mode,
            json=lambda c: c.mode,
            # `clone` is both the default and the overwhelmingly common mode,
            # so on most hosts this column is a constant — pure width. It
            # appears as soon as one mount-mode container exists, which is
            # exactly when telling the two apart starts to matter.
            show_if=lambda rows: any(c.mode != "clone" for c in rows),
        ),
        table_format.FieldSpec(
            name="base",
            header="BASE",
            cell=lambda c: c.base_branch if c.base_branch else "—",
            json=lambda c: c.base_branch,
        ),
        table_format.FieldSpec(
            name="state",
            header="STATE",
            cell=lambda c: c.state,
            json=lambda c: c.state,
        ),
        table_format.FieldSpec(
            name="created",
            header="CREATED",
            cell=_created_cell,
            json=lambda c: c.created_at.isoformat() if c.created_at else None,
        ),
        table_format.FieldSpec(
            name="job",
            header="JOB",
            cell=_job_cell,
            json=_job_json,
            default_json=False,
            show_if=lambda rows: any(c.job_phase is not None for c in rows),
        ),
        table_format.FieldSpec(
            name="network",
            header="NETWORK",
            cell=lambda c: c.network or "-",
            json=lambda c: c.network,
        ),
        table_format.FieldSpec(
            name="ttl",
            header="TTL",
            cell=_ttl_cell,
            json=_ttl_json,
            default_json=False,
            # TTL column shows when at least one container is in loose mode —
            # this also lets `--no-revert` users see the explicit "—" indicator.
            show_if=lambda rows: any(c.network == "loose" for c in rows),
        ),
        table_format.FieldSpec(
            name="loose_until",
            header="LOOSE UNTIL",
            cell=lambda c: c.loose_until.isoformat() if c.loose_until else "—",
            json=lambda c: c.loose_until.isoformat() if c.loose_until else None,
            default_table=False,
            default_json=False,
        ),
        table_format.FieldSpec(
            name="ip",
            header="IP",
            cell=lambda c: c.ip or "-",
            json=lambda c: c.ip,
            # `jailbee apply` writes /etc/hosts entries, so the address is
            # rarely what you reach a container by — it costs 15 columns in
            # every `ls` to answer a question most runs never ask. Still on
            # by default in the dashboards, where a glance is free, and in
            # JSON, where scripts depend on it.
            default_table=False,
            default_dashboard=True,
        ),
        table_format.FieldSpec(
            name="memory_limit",
            header="MEMORY LIMIT",
            cell=lambda c: c.memory_limit or "-",
            json=lambda c: c.memory_limit,
            # The default table shows `mem` (used / limit) instead; the bare
            # limit stays available via `--fields memory_limit`. JSON keeps
            # this key by default for backward-compatible scripting.
            default_table=False,
        ),
        table_format.FieldSpec(
            name="mem",
            header="MEM",
            cell=_mem_cell,
            json=lambda c: {"usage": c.memory_usage, "limit": c.memory_limit},
            # A live number, and therefore a dashboard column rather than an
            # `ls` one: in a one-shot listing it is a single stale sample,
            # while in a view that refreshes it is the reason to keep the
            # view open. `--fields mem` still reaches it from `ls`.
            default_table=False,
            default_dashboard=True,
            default_json=False,
        ),
        table_format.FieldSpec(
            name="wt",
            header="WT",
            cell=_git_cell("wt"),
            json=_git_json("wt"),
            default_json=False,
        ),
        table_format.FieldSpec(
            name="ahead_diff",
            header="AHEAD ±",
            cell=_git_cell("ahead_diff"),
            json=_git_json("ahead_diff"),
            default_json=False,
        ),
        table_format.FieldSpec(
            name="ahead_count",
            header="↑",
            cell=_git_cell("ahead_count", zero_dim=True),
            json=_git_json("ahead_count"),
            justify="right",
            default_json=False,
        ),
        table_format.FieldSpec(
            name="conflict",
            header="MERGE",
            cell=_conflict_cell,
            json=lambda c: c.git_status.conflict if c.git_status else None,
            default_json=False,
        ),
        table_format.FieldSpec(
            name="local_diff",
            header="LOCAL ±",
            cell=_git_cell("local_diff"),
            json=_git_json("local_diff"),
            # Off by default: AHEAD ± already carries the pinned-base answer,
            # and the default table is wide. Opt in via --fields or `ls.fields`.
            default_table=False,
            default_json=False,
        ),
        table_format.FieldSpec(
            name="local_count",
            header="L↑",
            cell=_git_cell("local_count", zero_dim=True),
            json=_git_json("local_count"),
            justify="right",
            default_table=False,
            default_json=False,
        ),
        table_format.FieldSpec(
            name="git_status",
            header="GIT STATUS",
            cell=lambda c: (
                f"wt={c.git_status.wt} ±={c.git_status.ahead_diff} "
                f"↑={c.git_status.ahead_count} merge={c.git_status.conflict}"
                if c.git_status
                else "—"
            ),
            json=_git_status_json,
            default_table=False,
        ),
        table_format.FieldSpec(
            name="pr",
            header="PR",
            cell=_pr_cell,
            json=_pr_json,
            show_if=lambda rows: any(c.pr_number is not None for c in rows),
        ),
    ]
