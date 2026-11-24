"""Creation policy is shared core, with no terminal or frontend dependency."""

from __future__ import annotations

import sys

import pytest

from jailbee.dashboard.create import start_create
from jailbee.dashboard.jobs import JobRunner
from jailbee.dashboard.notifications import NotificationQueue, QueueCounts
from jailbee.dashboard.runner import ActionOutcome, RunResult
from tests.test_dashboard_jobs import _drain


def _start(tmp_path, *, code="", argv=None, run_questions=None):
    jobs, queue = JobRunner(), NotificationQueue()
    notices, changes = [], []
    kwargs = dict(
        key="new:alpha:work",
        label="creating work",
        argv=argv or [sys.executable, "-c", code],
        cwd=tmp_path,
        target="work",
        run_questions=run_questions or (lambda *args: pytest.fail("unexpected question")),
        changed=lambda: changes.append(queue.counts()),
        done=notices.append,
    )
    start_create(jobs, queue, **kwargs)
    _drain(jobs)
    return queue, notices, changes


@pytest.mark.parametrize("spawn_error", [False, True])
def test_pre_worker_failure_is_retained_until_dismissed(tmp_path, spawn_error):
    queue, notices, changes = _start(
        tmp_path,
        code="import sys; print('cannot resolve base', file=sys.stderr); sys.exit(2)",
        argv=[str(tmp_path / "missing-jailbee"), "new"] if spawn_error else None,
    )
    (entry,) = queue
    assert entry.state == "failed"
    assert entry.target == "work"
    reason = "missing-jailbee" if spawn_error else "cannot resolve base"
    assert reason in entry.output
    assert reason in notices[0]
    assert changes[-1] == QueueCounts(0, 1)
    queue.set_jobs(())  # No worker ever recorded a job; refreshing cannot lose the error.
    assert queue.get(entry.id) == entry
    assert queue.dismiss(entry.id)
    assert queue.empty


def test_successful_handoff_leaves_no_parent_entry(tmp_path):
    queue, notices, _ = _start(tmp_path, code="print('worker started')")
    assert queue.empty
    assert notices == [None]


@pytest.mark.parametrize(
    "result,output,state,hint",
    [
        (RunResult(status=0), "worker started", None, None),
        (
            RunResult(status=1, cancelled=True),
            "warning: prior notice\nAborted.\n",
            None,
            "cancelled",
        ),
        (
            RunResult(status=1, cancelled=True),
            "╭─ Error ─╮\n│ disk failure │\n╰──────────────╯\n",
            "failed",
            "disk failure",
        ),
        (
            RunResult(status=1, cancelled=True),
            "╭─ Error ─╮\n│ cancelled │\n╰───────────╯\n"
            "╭─ Error ─╮\n│ disk failure │\n╰──────────────╯\n",
            "failed",
            "disk failure",
        ),
        (RunResult(status=3, cancelled=True), "Aborted.\n", "failed", "Aborted."),
        (
            RunResult(status=1, cancelled=True, error="runner failed"),
            "Aborted.\n",
            "failed",
            "runner failed",
        ),
        (RunResult(status=7, cancelled=True), "error: disk failure", "failed", "disk failure"),
        (RunResult(status=2), "confirmation needed", "failed", "confirmation needed"),
        (
            RunResult(status=0),
            "Aborted. Pick a different name to branch off 'dev' instead.",
            None,
            "Pick a different name",
        ),
        (RunResult(status=2), "error: base vanished", "failed", "base vanished"),
        (RunResult(error="could not spawn"), "", "failed", "could not spawn"),
        (RunResult(refused="policy changed"), "", None, "policy changed"),
    ],
)
def test_missing_answer_retries_once_with_live_queue_state(tmp_path, result, output, state, hint):
    seen = []

    def questions(runs, done, progress):
        seen.extend(runs)
        progress.question(0, True)
        progress.output(0, "asking\n")
        progress.question(0, False)
        done(ActionOutcome([result], output, [output]))

    queue, notices, changes = _start(
        tmp_path,
        # The marker is deliberately in stdout, not stderr.
        code="import sys; print('confirmation needed'); sys.exit(2)",
        run_questions=questions,
    )
    assert len(seen) == 1
    assert seen[0].cwd == tmp_path
    assert seen[0].argv[0] == "-c"
    assert QueueCounts(0, 1) in changes  # awaiting a modal answer
    assert notices[0] is None if hint is None else hint in notices[0]
    if state is None:
        assert queue.empty
    else:
        (entry,) = queue
        assert entry.state == state
        assert entry.status == (result.status if result.status is not None else 1)
        assert (result.error or output) in entry.output


@pytest.mark.parametrize(
    "output,expected",
    [
        ("╭─ Error ─╮\n│ disk failure │\n╰──────────────╯\n", "disk failure"),
        ("╭─ Error ─╮\n│ disk failure │\n│ on volume 1 │\n╰──────────────╯\n", "disk failure on volume 1"),
    ],
)
def test_create_failure_notice_contains_full_unboxed_message(tmp_path, output, expected):
    def questions(runs, done, progress):
        done(ActionOutcome([RunResult(status=1)], output, [output]))

    _, notices, _ = _start(
        tmp_path,
        code="import sys; print('confirmation needed'); sys.exit(2)",
        run_questions=questions,
    )
    assert notices == [f"{sys.executable} -c failed: {expected}"]


def test_bare_cancelled_failure_is_not_a_cancellation(tmp_path):
    def questions(runs, done, progress):
        done(ActionOutcome([RunResult(status=1, cancelled=True)], "cancelled\n", ["cancelled\n"]))

    queue, notices, _ = _start(
        tmp_path,
        code="import sys; print('confirmation needed'); sys.exit(2)",
        run_questions=questions,
    )
    assert notices == [f"{sys.executable} -c failed: cancelled"]
    assert next(iter(queue)).state == "failed"


def test_retry_keeps_create_busy_until_the_prompt_channel_finishes(tmp_path):
    jobs, queue = JobRunner(), NotificationQueue()
    callbacks = []
    kwargs = dict(
        key="new:work",
        label="creating",
        argv=[
            sys.executable,
            "-c",
            "import sys; print('confirmation needed', file=sys.stderr); sys.exit(2)",
        ],
        cwd=tmp_path,
        target="work",
        run_questions=lambda runs, done, progress: callbacks.append(done),
        changed=lambda: None,
        done=lambda notice: None,
    )
    start_create(jobs, queue, **kwargs)
    import time

    deadline = time.monotonic() + 10
    while not callbacks:
        assert time.monotonic() < deadline
        jobs.poll()
        time.sleep(0.01)
    assert jobs.busy("new:work")
    with pytest.raises(ValueError):
        start_create(jobs, queue, **kwargs)
    callbacks[0](ActionOutcome([RunResult(status=0)], "worker started"))
    assert not jobs.busy("new:work")
    assert queue.empty


def test_busy_create_does_not_add_a_failure(tmp_path):
    jobs, queue = JobRunner(), NotificationQueue()
    jobs.call("key", "pending", lambda: None, lambda result: None)
    with pytest.raises(ValueError):
        start_create(
            jobs,
            queue,
            key="key",
            label="creating",
            argv=["jailbee", "new"],
            cwd=tmp_path,
            target="work",
            run_questions=lambda *args: None,
            changed=lambda: None,
            done=lambda notice: None,
        )
    _drain(jobs)
    assert queue.empty
