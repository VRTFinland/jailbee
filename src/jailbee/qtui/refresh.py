"""Background container-state gather worker for the Qt dashboard.

Mirrors the TUI's daemon-thread refresher: a two-tier schedule (cheap base
gather every ``interval``, expensive git tier every ``git_interval``). Runs in
a QThread; delivers snapshots to the UI thread via signals — no shared state
behind locks.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal, Slot

from jailbee.dashboard import (
    _refresh_due,
    carry_forward_git_status,
    gather_live,
    sample_activity,
)
from jailbee.procstat import ActivitySampler

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.dashboard import RepoGroup
    from jailbee.incus import Incus


class RefreshWorker(QObject):
    """Gathers container state on a schedule and emits it to the UI thread."""

    groupsReady = Signal(object)  # noqa: N815 - Qt signal naming convention (camelCase); payload: list[RepoGroup]
    failed = Signal(str)

    def __init__(
        self,
        incus: Incus,
        cwd_root: Path | None,
        *,
        interval: float,
        git_interval: float,
        git_enabled: bool,
    ) -> None:
        super().__init__()
        self._incus = incus
        self._cwd_root = cwd_root
        self._interval = interval
        self._git_interval = git_interval
        self._git_enabled = git_enabled
        self._stop = False
        self._force = False
        self._paused = False
        self._prev_groups: list[RepoGroup] = []
        self._seeded_at: float | None = None
        # One sampler for the worker's lifetime: a rate is the difference
        # between two readings, and a per-call sampler would never have the
        # first one.
        self._sampler = ActivitySampler()

    def gather_once(self, do_git: bool) -> list[RepoGroup]:
        """Gather one snapshot (blocking). Wraps ``gather_live``, so each
        gather sees the repos registered *now* — see its docstring for why a
        launch-time root list leaves new repos menu-less."""
        return gather_live(
            self._incus,
            self._cwd_root,
            with_git=do_git and self._git_enabled,
        )

    @Slot()
    def request_stop(self) -> None:
        self._stop = True

    @Slot()
    def force(self) -> None:
        self._force = True

    def set_interval(self, value: float) -> None:
        """Change the base-gather cadence and resume auto-refresh.

        Plain (non-``@Slot``) method, called from the main thread the same
        way as :meth:`force`/:meth:`request_stop`; the next loop tick reads
        the new value."""
        self._interval = max(0.5, value)
        self._paused = False

    def seed(self, groups: list[RepoGroup], *, at: float) -> None:
        """Adopt a snapshot gathered before the window was shown.

        ``app.run`` surveys the cheap tier synchronously so the window is
        never seen blank; this hands the result over so the loop *continues*
        that schedule instead of restarting it. ``at`` is the monotonic
        timestamp of that gather. Must be called before the thread starts.
        """
        self._prev_groups = groups
        self._seeded_at = at

    def sample_activity(self, groups: list[RepoGroup]) -> None:
        """Fill ``groups``' CPU/DOING fields from one reading.

        Public because ``app.run`` primes the sampler with two calls before
        the window appears. Like :meth:`seed`, those calls must happen
        before the thread starts — and this must never be connected to a
        signal: ``run_loop`` has no event loop, so a direct connection would
        execute it on the wrong thread.
        """
        sample_activity(groups, self._sampler)

    def set_paused(self, paused: bool) -> None:
        """Pause/resume periodic gathers. A paused worker still honors
        :meth:`force` and the initial gather."""
        self._paused = paused

    @Slot()
    def run_loop(self) -> None:
        """The gather loop. Runs until :meth:`request_stop` is observed."""
        # A seeded worker continues the pre-gather's schedule: `first` forces
        # an immediate git-inclusive gather, which is exactly what the cheap
        # seed is still missing — but with git disabled there is nothing left
        # to fetch, and a `first` there would just repeat the seed.
        last_base = 0.0 if self._seeded_at is None else self._seeded_at
        last_full = 0.0
        first = True if self._seeded_at is None else self._git_enabled
        while not self._stop:
            forced = self._force
            do_base, do_git = _refresh_due(
                now=time.monotonic(),
                last_base=last_base,
                last_full=last_full,
                interval=self._interval,
                git_interval=self._git_interval,
                git_enabled=self._git_enabled,
                first=first,
                forced=forced,
            )
            if self._paused and not forced and not first:
                # Manual mode: suppress periodic gathers, but a `force()`
                # (forced=True) or the initial gather must still go through.
                do_base = False
            if do_base:
                self._force = False
                try:
                    groups = self.gather_once(do_git)
                except Exception as exc:  # surface any gather failure to the UI and keep polling
                    # A persistent error (e.g. incus daemon unreachable) must
                    # not freeze the window, so we retry at the normal cadence
                    # below instead of returning out of the loop.
                    self.failed.emit(str(exc))
                else:
                    if not do_git:
                        # A base gather has no git status; fill it in from the
                        # last git-tier snapshot so the columns don't flicker
                        # blank until the next git-tier refresh lands.
                        carry_forward_git_status(groups, self._prev_groups)
                    self.sample_activity(groups)
                    self.groupsReady.emit(groups)
                    self._prev_groups = groups
                # Bookkeeping runs on both success and failure so a failing
                # gather retries once per `interval`, not in a hot loop.
                ts = time.monotonic()
                last_base = ts
                if do_git:
                    last_full = ts
                first = False
            time.sleep(0.1)
