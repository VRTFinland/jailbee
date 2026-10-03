"""Detached runs of dashboard-built ``jailbee`` commands.

Some dashboard actions (an egress change, ``jailbee new``) take a while and
need nothing from the terminal. Running them through the dashboard's
``foreground`` helper blanks the screen and holds the operator until Enter;
here they run as a child with no stdin and a captured stderr instead, and the
dashboard is told when they end.

The child is started in its own session, so closing the dashboard mid-run
leaves a half-applied change to finish rather than killing it; only the
result is lost.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# What `jailbee new` says when it needed an answer a detached run cannot give:
# the branch-reuse confirmation (`typer.confirm` on EOF aborts) and the
# privilege-widening gate (`_preflight_background_new`). Pinned against
# `cli.py` by a test, because the fallback below is only as good as this
# wording.
NEEDS_TERMINAL_MARKERS: tuple[str, ...] = (
    "no terminal to ask on",
    "already exists in source repo",
)


@dataclass(frozen=True)
class JobResult:
    returncode: int
    stderr: str

    def failure_line(self) -> str:
        """The last non-empty stderr line: the reason, for a one-line notice."""
        lines = [line.strip() for line in self.stderr.splitlines() if line.strip()]
        return lines[-1] if lines else ""


def needs_terminal(result: JobResult) -> bool:
    """Whether a failed detached run stopped because it wanted to ask something."""
    return result.returncode != 0 and any(m in result.stderr for m in NEEDS_TERMINAL_MARKERS)


class JobRunner:
    """Runs commands detached; delivers each result to the polling thread.

    ``start`` is called from the dashboard's main thread, the waiting happens
    on a daemon thread per job, and ``poll`` — also main thread — invokes the
    callbacks. A callback therefore touches dashboard state without a lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._labels: dict[str, str] = {}
        self._finished: list[tuple[str, Callable[[JobResult], None], JobResult]] = []

    def busy(self, key: str) -> bool:
        """Whether ``key`` has a job that has not been delivered by ``poll`` yet."""
        with self._lock:
            return key in self._labels

    def active(self) -> list[str]:
        """Labels of the jobs not yet delivered, oldest first."""
        with self._lock:
            return list(self._labels.values())

    def start(
        self,
        key: str,
        label: str,
        argv: list[str],
        cwd: Path,
        on_done: Callable[[JobResult], None],
    ) -> None:
        """Spawn ``argv``; ``on_done`` gets the result from a later ``poll``.

        Raises ``OSError`` if the child cannot start (its ``cwd`` vanished, the
        binary is missing) and ``ValueError`` if ``key`` is already running.
        """
        with self._lock:
            if key in self._labels:
                raise ValueError(f"job '{key}' is already running")
            self._labels[key] = label
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                cwd=cwd,
                start_new_session=True,
            )
        except OSError:
            with self._lock:
                del self._labels[key]
            raise

        def wait() -> None:
            _, stderr = proc.communicate()
            result = JobResult(proc.returncode, stderr or "")
            with self._lock:
                self._finished.append((key, on_done, result))

        threading.Thread(target=wait, name=f"jailbee-dashboard-job-{key}", daemon=True).start()

    def poll(self) -> None:
        """Deliver every finished job's result to its callback."""
        with self._lock:
            finished, self._finished = self._finished, []
        for key, on_done, result in finished:
            with self._lock:
                self._labels.pop(key, None)
            on_done(result)
