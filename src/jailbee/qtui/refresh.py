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

from jailbee.dashboard import _refresh_due, carry_forward_git_status, gather_live

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
        cwd_config: Path | None,
        *,
        interval: float,
        git_interval: float,
        git_enabled: bool,
    ) -> None:
        super().__init__()
        self._incus = incus
        self._cwd_config = cwd_config
        self._interval = interval
        self._git_interval = git_interval
        self._git_enabled = git_enabled
        self._stop = False
        self._force = False
        self._paused = False
        self._prev_groups: list[RepoGroup] = []

    def gather_once(self, do_git: bool) -> list[RepoGroup]:
        """Gather one snapshot (blocking). Wraps ``gather_live``, so each
        gather sees the repos registered *now* — see its docstring for why a
        launch-time path list leaves new repos menu-less."""
        return gather_live(
            self._incus,
            self._cwd_config,
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

    def set_paused(self, paused: bool) -> None:
        """Pause/resume periodic gathers. A paused worker still honors
        :meth:`force` and the initial gather."""
        self._paused = paused

    @Slot()
    def run_loop(self) -> None:
        """The gather loop. Runs until :meth:`request_stop` is observed."""
        last_base = 0.0
        last_full = 0.0
        first = True
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
