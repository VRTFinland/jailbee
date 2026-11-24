"""Publish through the dashboard's prompt channel and retain its result, without a terminal."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from jailbee.dashboard.create import QuestionRunner
from jailbee.dashboard.jobs import JobResult
from jailbee.dashboard.notifications import NotificationQueue
from jailbee.dashboard.runner import ActionOutcome, ActionRun, RunResult
from jailbee.remote_ssh.router import RouteError


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
    route: Callable[[Sequence[str]], ActionRun],
    target: str,
    queue: NotificationQueue,
    run_questions: QuestionRunner,
    changed: Callable[[], None],
    done: Callable[[JobResult], None],
) -> None:
    """Route and run a publication; callbacks run on the frontend thread.

    ``route`` addresses and policy-checks the command, raising RouteError on
    refusal. Questions and streamed output belong to the injected frontend runner.
    The queue retains URLs and failure reasons even when the Outbox box is closed.
    """
    entry = queue.start("jailbee " + " ".join(argv), target)
    changed()

    def settled(outcome: ActionOutcome) -> None:
        result = outcome.results[0] if outcome.results else None
        output = outcome.outputs[0] if outcome.outputs else outcome.output
        reason = (result.refused or result.error) if result else "publication did not run"
        status = result.status if result and result.status is not None else 1
        if result and (result.cancelled or result.declined):
            status = status or 1
            reason = reason or "publication cancelled"
        if reason:
            status = status or 1
            output = "\n".join(part for part in (output.rstrip(), reason) if part)
        queue.finish(entry, status, output)
        changed()
        summary = reason or JobResult(status, output).failure_line()
        done(JobResult(status, "", summary or ("published" if status == 0 else f"publication exited {status}")))

    try:
        run = route(argv)
    except RouteError as exc:
        settled(ActionOutcome([RunResult(refused=str(exc))], ""))
        return
    run_questions([run], settled, _Progress(queue, entry, changed))
