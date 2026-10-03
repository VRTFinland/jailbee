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
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from jailbee.dashboard import carry_forward_git_status, gather_live, sample_activity
from jailbee.procstat import PRIME_INTERVAL_SECONDS, ActivitySampler
from jailbee.state_service.protocol import GatherError, Snapshot

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.dashboard import RepoGroup
    from jailbee.global_config import DashboardRefresh
    from jailbee.incus import Incus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Cadence:
    interval: float
    git_interval: float
    git: bool

    @classmethod
    def from_config(cls, refresh: DashboardRefresh) -> Cadence:
        """The configured cadence, floored: 0.5 s, and git no faster than base."""
        interval = max(0.5, refresh.interval)
        return cls(interval, max(refresh.git_interval, interval), refresh.git)


def refresh_due(
    *,
    now: float,
    last_base: float | None,
    last_full: float | None,
    cadence: Cadence,
    active: bool,
    refresh: bool,
) -> tuple[bool, bool]:
    """``(gather now, include the git tier)``.

    Nothing is gathered for nobody: with no active client only a `refresh`
    gets through. The very first gather is base-only so the first frame
    arrives fast; the git tier follows on the next tick (``last_full`` is
    still None then). ``now`` and the ``last_*`` stamps are monotonic.
    """
    if not (active or refresh):
        return False, False
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
    ) -> None:
        self._incus = incus
        self.cadence = cadence
        self._gather = gather
        self._clock = clock
        self._sleep = sleep
        self._wall_clock = wall_clock
        # One sampler for the service's lifetime: a rate needs the previous reading.
        self._sampler = ActivitySampler()
        self._primed = False
        self._last_base: float | None = None
        self._last_full: float | None = None
        self._prev: list[RepoGroup] = []
        self._seq = 0

    def tick(
        self, *, active: bool, refresh: bool, roots: Sequence[Path]
    ) -> Snapshot | GatherError | None:
        """Gather if due. None when nothing was due.

        ``roots`` are the connected clients' cwd repos, gathered on top of the
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
                sample_activity(groups, self._sampler)
                self._sleep(PRIME_INTERVAL_SECONDS)
                self._primed = True
            sample_activity(groups, self._sampler)
        except Exception as exc:  # reported to every client; the service lives on
            log.warning("gather failed", exc_info=True)
            result = GatherError(str(exc) or type(exc).__name__)
        else:
            self._prev = groups
            self._seq += 1
            result = Snapshot(self._seq, self._wall_clock(), self.cadence.git, groups)
        stamp = self._clock()
        self._last_base = stamp
        # A failure stamps the git tier too: otherwise a failed first (base-only)
        # gather leaves ``last_full`` unset and the git tier retries next tick.
        if do_git or isinstance(result, GatherError):
            self._last_full = stamp
        return result
