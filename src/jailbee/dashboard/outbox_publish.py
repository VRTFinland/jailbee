"""Publish through the dashboard's prompt channel and retain its result, without a terminal."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from jailbee.dashboard.create import QuestionRunner
from jailbee.dashboard.jobs import JobResult
from jailbee.dashboard.notifications import NotificationQueue
from jailbee.dashboard.runner import ActionOutcome, ActionRun


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


def publish(
    argv: Sequence[str],
    *,
    cwd: Path,
    target: str,
    queue: NotificationQueue,
    run_questions: QuestionRunner,
    changed: Callable[[], None],
    done: Callable[[JobResult], None],
) -> None:
    """Run an addressed, policy-checked command; callbacks run on the frontend thread.

    Questions and streamed output belong to the injected frontend runner. The
    queue retains URLs and failure reasons even when the Outbox box is closed.
    """
    entry = queue.start("jailbee " + " ".join(argv), target)
    changed()

    def settled(outcome: ActionOutcome) -> None:
        result = outcome.results[0] if outcome.results else None
        output = outcome.outputs[0] if outcome.outputs else outcome.output
        reason = (result.refused or result.error) if result else "publication did not run"
        status = result.status if result and result.status is not None else 1
        if reason:
            status = status or 1
            output = "\n".join(part for part in (output.rstrip(), reason) if part)
        queue.finish(entry, status, output)
        changed()
        done(JobResult(status, "", output))

    run_questions([ActionRun(list(argv), cwd)], settled, _Progress(queue, entry, changed))
