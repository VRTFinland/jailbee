"""Start a dashboard create, retry questions through its UI, and retain failures.

The detached parent only preflights and launches a background worker. Successful
launches are tracked by the snapshot's job rows; failures before that point need
an ordinary notification entry. No frontend collects or duplicates CLI questions.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from jailbee.dashboard.jobs import JobResult, JobRunner, needs_terminal
from jailbee.dashboard.notifications import NotificationQueue
from jailbee.dashboard.runner import ActionOutcome, ActionRun, RunProgress


class QuestionRunner(Protocol):
    """Frontend adapter to the shared prompt-channel action runner.

    All callbacks, including progress, must be delivered on the frontend thread.
    """

    def __call__(
        self,
        runs: Sequence[ActionRun],
        done: Callable[[ActionOutcome], None],
        progress: RunProgress | None = None,
    ) -> None: ...


class _Progress:
    def __init__(self, queue: NotificationQueue, entry: int, changed: Callable[[], None]) -> None:
        self.queue = queue
        self.entry = entry
        self.changed = changed

    def output(self, index: int, chunk: str) -> None:
        self.queue.append(self.entry, chunk)
        self.changed()

    def question(self, index: int, asking: bool) -> None:
        self.queue.set_awaiting(self.entry, asking)
        self.changed()


def start_create(
    jobs: JobRunner,
    queue: NotificationQueue,
    *,
    key: str,
    label: str,
    argv: list[str],
    cwd: Path,
    target: str,
    run_questions: QuestionRunner,
    changed: Callable[[], None],
    done: Callable[[str | None], None],
) -> None:
    """Run detached, retry a missing answer through the prompt channel, retain errors.

    ``argv`` includes the executable and must already be policy-checked. The
    prompt-channel runner checks policy again on retry. ``jobs.poll()`` and the
    question runner deliver callbacks on the UI thread. ``done`` receives a
    notice, or None when the background worker took over. A duplicate job raises
    ValueError without creating a notification.
    """
    command = " ".join(argv[:2])

    def failed(result: JobResult) -> None:
        entry = queue.start(command, target)
        queue.finish(entry, result.returncode, result.stdout + result.stderr)
        changed()
        reason = result.failure_line() or f"exited {result.returncode}"
        done(f"{command} failed: {reason}")

    def retry() -> None:
        entry = queue.start(command, target)
        changed()

        def settled(outcome: ActionOutcome) -> None:
            result = outcome.results[-1] if outcome.results else None
            output = outcome.output
            if result is None or result.refused is not None:
                queue.drop(entry)
                changed()
                done(result.refused if result else "nothing ran")
                return
            status = result.status if result.status is not None else 1
            if result.error is not None:
                output += f"\n{result.error}"
            queue.finish(entry, status, output)
            changed()
            if status != 0:
                reason = JobResult(status, output).failure_line() or f"exited {status}"
                done(f"{command} failed: {reason}")
            else:
                # A declined reuse is exit 0, not a failed create. Rich may
                # wrap the CLI's hint: keep it readable as a one-line notice.
                text = " ".join(output.split())
                at = text.find("Aborted. Pick a different name")
                done(text[at:] if at >= 0 else f"'{command}' done")

        run_questions([ActionRun(argv[1:], cwd)], settled, _Progress(queue, entry, changed))

    def finish(result: JobResult) -> None:
        if needs_terminal(result):
            retry()
        elif result.returncode != 0:
            failed(result)
        else:
            done(None)

    try:
        jobs.start(key, label, argv, cwd, finish, capture_stdout=True)
    except OSError as exc:
        failed(JobResult(1, f"{cwd}: {exc}"))
