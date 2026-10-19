"""The two-tier gather schedule, in the one place it now lives.

A cheap base tier (`incus list`, background jobs, CPU readings) every
``interval``; the expensive git tier (one `incus exec` per running container)
every ``git_interval``. Nothing here knows about sockets: `server.StateServer`
calls `Gatherer.tick` and ships whatever it returns.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from jailbee.agent_activity import ActivityReader
from jailbee.dashboard.model import carry_forward_git_status, gather_live, sample_activity
from jailbee.dashboard.system import SystemReader, pool_names
from jailbee.procstat import PRIME_INTERVAL_SECONDS, ActivitySampler
from jailbee.state_service.protocol import GatherError, Snapshot
from jailbee.update_check import current_notice

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.dashboard.model import RepoGroup
    from jailbee.dashboard.system import SystemStatus
    from jailbee.global_config import DashboardRefresh
    from jailbee.incus import Incus
    from jailbee.update_check import UpdateNotice

log = logging.getLogger(__name__)

UPDATE_INTERVAL = 60.0
"""Seconds between reads of the update notice. A refresh reads it at once."""

FAST_INTERVAL = 1.0
FAST_GIT_INTERVAL = 3.0
"""The base and git tier intervals while a refresh's settle window is open."""


@dataclass(frozen=True)
class Cadence:
    interval: float
    git_interval: float
    git: bool
    activity: bool = True

    @classmethod
    def from_config(cls, refresh: DashboardRefresh) -> Cadence:
        """The configured cadence, floored: 0.5 s, and git no faster than base."""
        interval = max(0.5, refresh.interval)
        return cls(
            interval, max(refresh.git_interval, interval), refresh.git, refresh.agent_activity
        )


def refresh_due(
    *,
    now: float,
    last_base: float | None,
    last_full: float | None,
    cadence: Cadence,
    active: bool,
    refresh: bool,
    fast: bool = False,
) -> tuple[bool, bool]:
    """``(gather now, include the git tier)``.

    Nothing is gathered for nobody: with no active client only a `refresh`
    gets through. The very first gather is base-only so the first frame
    arrives fast; the git tier follows on the next tick (``last_full`` is
    still None then). ``now`` and the ``last_*`` stamps are monotonic.

    ``fast`` (a refresh's settle window is open) caps both intervals at
    `FAST_INTERVAL` / `FAST_GIT_INTERVAL`: the state an action set in motion
    needs a few seconds to settle, and a single gather right after it would
    show the half-way picture.
    """
    if not (active or refresh):
        return False, False
    if fast:
        cadence = replace(
            cadence,
            interval=min(cadence.interval, FAST_INTERVAL),
            git_interval=min(cadence.git_interval, FAST_GIT_INTERVAL),
        )
    if last_base is None:
        return True, False
    do_git = cadence.git and (
        refresh or last_full is None or now >= last_full + cadence.git_interval
    )
    do_base = refresh or do_git or now >= last_base + cadence.interval
    return do_base, do_git


class Gatherer:
    """Runs the schedule and turns each gather into a `Snapshot` or `GatherError`.

    Not thread-safe: the server calls `tick` from one worker thread at a time.
    """

    def __init__(
        self,
        incus: Incus,
        cadence: Cadence,
        *,
        gather: Callable[..., list[RepoGroup]] = gather_live,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        update: Callable[[], UpdateNotice | None] = current_notice,
        system: SystemReader | None = None,
    ) -> None:
        self._incus = incus
        self.cadence = cadence
        self._gather = gather
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        # One sampler for the service's lifetime: a rate needs the previous reading.
        self._sampler = ActivitySampler()
        # Owned for the service's lifetime: it remembers where each live
        # session's transcript is.
        self._reader = ActivityReader() if cadence.activity else None
        self._primed = False
        self._last_base: float | None = None
        self._last_full: float | None = None
        self._prev: list[RepoGroup] = []
        self._seq = 0
        self._update = update
        # Pool reads are throttled inside the reader; the base tier adds no incus call.
        self._system = system or SystemReader(interval=cadence.git_interval)
        self._update_notice: UpdateNotice | None = None
        self._last_update: float | None = None
        # The message of the failure being logged, so an outage that lasts
        # all night writes one traceback, not one every few seconds.
        self._failing: str | None = None

    def tick(
        self, *, active: bool, refresh: bool, roots: Sequence[Path], fast: bool = False
    ) -> Snapshot | GatherError | None:
        """Gather if due. None when nothing was due.

        ``fast`` is the settle window of `refresh_due`. ``roots`` are the
        connected clients' cwd repos, gathered on top of the
        registered ones. A failed gather is reported, never raised, and still
        moves the schedule on — so a dead incusd is retried once per
        ``interval``, not in a hot loop.
        """
        do_base, do_git = refresh_due(
            now=self._clock(),
            last_base=self._last_base,
            last_full=self._last_full,
            cadence=self.cadence,
            active=active,
            refresh=refresh,
            fast=fast,
        )
        if not do_base:
            return None
        result: Snapshot | GatherError
        try:
            groups = self._gather(self._incus, roots, with_git=do_git)
            if not do_git:
                carry_forward_git_status(groups, self._prev)
            if not self._primed:
                # A rate needs two readings; only the /proc read repeats.
                sample_activity(groups, self._sampler, self._reader)
                self._sleep(PRIME_INTERVAL_SECONDS)
                self._primed = True
            sample_activity(groups, self._sampler, self._reader)
        except Exception as exc:  # reported to every client; the service lives on
            message = str(exc) or type(exc).__name__
            if message != self._failing:
                log.warning("gather failed", exc_info=True)
                self._failing = message
            result = GatherError(message)
        else:
            if self._failing is not None:
                log.warning("gather recovered (was failing: %s)", self._failing)
                self._failing = None
            self._prev = groups
            self._seq += 1
            result = Snapshot(
                self._seq,
                self._wall_clock(),
                self.cadence.git,
                groups,
                self._update_notice_now(refresh=refresh),
                self._system_now(groups),
            )
        stamp = self._clock()
        self._last_base = stamp
        # A failure stamps the git tier too: otherwise a failed first (base-only)
        # gather leaves ``last_full`` unset and the git tier retries next tick.
        if do_git or isinstance(result, GatherError):
            self._last_full = stamp
        return result

    def _update_notice_now(self, *, refresh: bool) -> UpdateNotice | None:
        """The update notice, re-read once a minute or when a client asks.

        A courtesy never fails a gather: a read that raises keeps nothing and
        is retried on the next interval.
        """
        now = self._clock()
        due = self._last_update is None or refresh or now - self._last_update >= UPDATE_INTERVAL
        if due:
            self._last_update = now
            try:
                self._update_notice = self._update()
            except Exception:
                log.debug("update notice read failed", exc_info=True)
                self._update_notice = None
        return self._update_notice

    def _system_now(self, groups: list[RepoGroup]) -> SystemStatus | None:
        """Host memory and storage; a failed read ships None, never fails the gather."""
        try:
            return self._system.read(self._incus, pool_names(groups))
        except Exception:
            log.debug("system status read failed", exc_info=True)
            return None
