"""Keep the host's copy of each bound PR's head current, for the state service.

A PR-bound container's ↑/↓ compare with `refs/jailbee/pr/<N>/head` on the host
(`host_target.resolve_sync_target`), which only moves when something fetches.
`PrHeadAutofetcher` is the something: once per gather it starts a background
fetch for every bound PR whose last attempt is older than the repo's
`pr.autofetch_interval`, and writes what it knows — when the head was last
fetched, the last failure — onto each container's `SyncTarget` so the card can
say so. The fetch never prompts (`pr.fetch_pr_head_unattended`), runs off the
gather's critical path, and a failure keeps the old ref and waits for the next
interval. With `pr.autofetch` off nothing is fetched.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoGroup

log = logging.getLogger(__name__)

POLICY_TTL = 30.0
"""Seconds a repo's autofetch policy is reused before its config is read again."""


@dataclass(frozen=True)
class AutofetchPolicy:
    """What a repo's config says about autofetching its PR heads."""

    repo_root: Path
    remote: str
    enabled: bool
    interval: float


@dataclass
class _PrState:
    last_attempt: float | None = None  # monotonic
    in_flight: bool = False
    fetched_at: float | None = None  # wall clock, epoch seconds
    error: str | None = None
    error_at: float | None = None
    error_sha: str | None = None  # the ref's sha when the failure was first shown


def read_policy(repo_root: Path) -> AutofetchPolicy | None:
    """The repo's autofetch policy from its config; None when it cannot be loaded."""
    from jailbee.config.loader import load_repo_config

    try:
        cfg = load_repo_config(repo_root)
    except Exception:
        log.debug("autofetch: config of %s not loadable", repo_root, exc_info=True)
        return None
    return AutofetchPolicy(
        repo_root=cfg.repo_root,
        remote=cfg.upstream_remote,
        enabled=cfg.pr.autofetch,
        interval=float(cfg.pr.autofetch_interval),
    )


def _fetch_unattended(repo_root: Path, number: int, remote: str) -> None:
    from jailbee.pr import fetch_pr_head_unattended

    fetch_pr_head_unattended(repo_root, number, remote=remote)


def _in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="pr-autofetch", daemon=True).start()


class PrHeadAutofetcher:
    """Schedules PR head fetches and annotates sync targets with their outcome.

    ``policy``, ``fetch``, ``submit`` and the two clocks are seams: the state
    service uses the defaults; tests pass a fake policy, a recording fetch and
    a synchronous ``submit``.
    """

    def __init__(
        self,
        *,
        policy: Callable[[Path], AutofetchPolicy | None] = read_policy,
        fetch: Callable[[Path, int, str], None] = _fetch_unattended,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        submit: Callable[[Callable[[], None]], None] = _in_thread,
    ) -> None:
        self._policy = policy
        self._fetch = fetch
        self._clock = clock
        self._wall = wall
        self._submit = submit
        self._lock = threading.Lock()
        self._states: dict[tuple[Path, int], _PrState] = {}
        self._policies: dict[str, tuple[float, AutofetchPolicy | None]] = {}

    def annotate(self, groups: Iterable[RepoGroup]) -> None:
        """Start the fetches that are due and write fetch facts onto the sync targets.

        Never raises into the gather: a repo whose policy cannot be read is
        left as it is.
        """
        for group in groups:
            if group.repo_root is None:
                continue
            policy = self._policy_for(group.repo_root)
            if policy is None:
                continue
            for number in {c.pr_number for c in group.containers if c.pr_number is not None}:
                self._maybe_fetch(policy, number)
            for c in group.containers:
                gs = c.git_status
                if c.pr_number is None or gs is None or gs.sync_target is None:
                    continue
                if gs.sync_target.parent is not None:
                    continue  # a child compares with its parent; no PR head facts apply
                state = self._state(policy.repo_root, c.pr_number)
                with self._lock:
                    self._settle_error(state, gs.sync_target.sha)
                    target = replace(
                        gs.sync_target,
                        autofetch=policy.enabled,
                        fetched_at=state.fetched_at,
                        fetch_error=state.error,
                        fetch_error_at=state.error_at,
                    )
                c.git_status = replace(gs, sync_target=target)

    def _settle_error(self, state: _PrState, sha: str | None) -> None:
        """Drop a failure the ref has moved past (a manual fetch succeeded). Lock held."""
        if state.error is None or sha is None:
            return
        if state.error_sha is None:
            state.error_sha = sha
        elif state.error_sha != sha:
            state.fetched_at = self._wall()
            state.error = state.error_at = state.error_sha = None

    def _policy_for(self, repo_root: str) -> AutofetchPolicy | None:
        now = self._clock()
        with self._lock:
            cached = self._policies.get(repo_root)
        if cached is not None and now - cached[0] < POLICY_TTL:
            return cached[1]
        policy = self._policy(Path(repo_root))
        with self._lock:
            self._policies[repo_root] = (now, policy)
        return policy

    def _state(self, repo_root: Path, number: int) -> _PrState:
        with self._lock:
            return self._states.setdefault((repo_root, number), _PrState())

    def _maybe_fetch(self, policy: AutofetchPolicy, number: int) -> None:
        if not policy.enabled:
            return
        state = self._state(policy.repo_root, number)
        now = self._clock()
        with self._lock:
            if state.in_flight or (
                state.last_attempt is not None and now - state.last_attempt < policy.interval
            ):
                return
            state.in_flight = True
            state.last_attempt = now

        def job() -> None:
            error: str | None = None
            try:
                self._fetch(policy.repo_root, number, policy.remote)
            except Exception as exc:  # recorded for the card; the service lives on
                error = str(exc) or type(exc).__name__
                log.debug("autofetch of PR #%s failed", number, exc_info=True)
            stamp = self._wall()
            with self._lock:
                if error is None:
                    state.fetched_at, state.error, state.error_at = stamp, None, None
                    state.error_sha = None
                else:
                    state.error, state.error_at, state.error_sha = error, stamp, None
                state.in_flight = False

        self._submit(job)
