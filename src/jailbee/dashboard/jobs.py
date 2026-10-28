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

import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from jailbee.publish_progress import PROGRESS_ENV, parse_progress

_T = TypeVar("_T")

# What `jailbee new` says when it needed an answer a detached run cannot give:
# the branch-reuse confirmation (`typer.confirm` on EOF aborts) and the
# privilege-widening gate (`create_flow.preflight_background`). Pinned against
# `cli.py` / `create_flow.py` by a test, because the fallback below is only as
# good as this wording.
NEEDS_TERMINAL_MARKERS: tuple[str, ...] = (
    "no terminal to ask on",
    "already exists in source repo",
)


@dataclass(frozen=True)
class JobResult:
    returncode: int
    stderr: str
    stdout: str = ""

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
        # Each finished job: its key, its callback and what the callback gets
        # (a `JobResult` from `start`, the function's value from `call`).
        self._finished: list[tuple[str, Callable[[Any], None], Any]] = []
        self._progress: dict[str, tuple[Callable[[int, int], None], int, int]] = {}

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
        *,
        capture_stdout: bool = False,
        on_progress: Callable[[int, int], None] | None = None,
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
                stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                cwd=cwd,
                start_new_session=True,
                env={**os.environ, PROGRESS_ENV: "1"} if on_progress is not None else None,
            )
        except OSError:
            with self._lock:
                del self._labels[key]
            raise

        def wait() -> None:
            if on_progress is None:
                stdout, stderr = proc.communicate()
            else:
                output: list[str] = []
                def read_stdout() -> None:
                    assert proc.stdout is not None
                    with proc.stdout:
                        output.append(proc.stdout.read())

                reader = None
                if proc.stdout is not None:
                    reader = threading.Thread(target=read_stdout, daemon=True)
                    reader.start()
                lines: list[str] = []
                assert proc.stderr is not None
                with proc.stderr:
                    for line in proc.stderr:
                        progress = parse_progress(line)
                        if progress is None:
                            lines.append(line)
                        else:
                            with self._lock:
                                self._progress[key] = (on_progress, *progress)
                proc.wait()
                if reader is not None:
                    reader.join()
                stdout, stderr = "".join(output), "".join(lines)
            result = JobResult(proc.returncode, stderr or "", stdout or "")
            with self._lock:
                self._finished.append((key, on_done, result))

        threading.Thread(target=wait, name=f"jailbee-dashboard-job-{key}", daemon=True).start()

    def call(
        self, key: str, label: str, fn: Callable[[], _T], on_done: Callable[[_T], None]
    ) -> None:
        """Run ``fn`` on a daemon thread; ``on_done`` gets its return value from a later ``poll``.

        For a blocking call that already captures a child's output (`run_cli_quiet`).
        ``fn`` must not raise. Raises ``ValueError`` if ``key`` is already running.
        """
        with self._lock:
            if key in self._labels:
                raise ValueError(f"job '{key}' is already running")
            self._labels[key] = label

        def run() -> None:
            value = fn()
            with self._lock:
                self._finished.append((key, on_done, value))

        threading.Thread(target=run, name=f"jailbee-dashboard-call-{key}", daemon=True).start()

    def poll(self) -> None:
        """Deliver every finished job's result to its callback."""
        with self._lock:
            finished, self._finished = self._finished, []
            progress, self._progress = self._progress, {}
        for callback, n, total in progress.values():
            callback(n, total)
        for key, on_done, result in finished:
            with self._lock:
                self._labels.pop(key, None)
            on_done(result)
