"""Outbox publications use the shared prompt runner without a terminal frontend."""

import pytest

from jailbee.dashboard.jobs import JobResult
from jailbee.dashboard.notifications import NotificationQueue
from jailbee.dashboard.runner import ActionOutcome, RunResult


def test_publish_retains_output_and_failure_in_queue(tmp_path):
    from jailbee.dashboard.outbox_publish import publish

    queue = NotificationQueue()
    pending = []
    completed = []

    def questions(runs, done, progress):
        assert runs[0].argv == ["outbox", "apply", "alpha-x", "issue/a.json"]
        assert runs[0].cwd == tmp_path
        progress.question(0, True)
        progress.output(0, "Publishing issue\n")
        pending.append((done, progress))

    publish(
        ["outbox", "apply", "alpha-x", "issue/a.json"],
        cwd=tmp_path,
        target="alpha-x",
        queue=queue,
        run_questions=questions,
        changed=lambda: None,
        done=completed.append,
    )
    (entry,) = queue
    assert entry.state == "awaiting"
    done, progress = pending[0]
    progress.question(0, False)
    done(ActionOutcome([RunResult(status=1)], "permission denied", ["permission denied"]))
    (entry,) = queue
    assert entry.state == "failed" and entry.output == "permission denied"
    assert completed == [JobResult(1, "", "permission denied")]


def test_success_keeps_published_url(tmp_path):
    from jailbee.dashboard.outbox_publish import publish

    queue = NotificationQueue()
    url = "https://github.com/o/r/issues/42"

    def questions(runs, done, progress):
        done(ActionOutcome([RunResult(status=0)], url, [url]))

    publish(
        ["issue", "apply", "alpha-x", "a.json"], cwd=tmp_path, target="alpha-x",
        queue=queue, run_questions=questions, changed=lambda: None, done=lambda result: None,
    )
    (entry,) = queue
    assert entry.state == "done" and entry.output == url


@pytest.mark.parametrize("cancelled, output", [(True, "Aborted."), (False, "Nothing published.")])
def test_cancelled_creation_does_not_report_success(tmp_path, cancelled, output):
    from jailbee.dashboard.outbox_publish import publish

    queue = NotificationQueue()
    completed = []

    def questions(runs, done, progress):
        done(ActionOutcome([RunResult(status=0, cancelled=cancelled)], output))

    publish(
        ["pr", "push", "alpha-x"], cwd=tmp_path, target="alpha-x", queue=queue,
        run_questions=questions, changed=lambda: None, done=completed.append,
    )
    assert completed[0].returncode != 0
    assert next(iter(queue)).state == "failed"


@pytest.mark.parametrize("remote", [False, True])
def test_marked_manifest_then_source_publish_without_handoff(remote):
    from jailbee.dashboard.outbox_flow import OutboxController
    from tests.test_dashboard_outbox_flow import C, FakeRunner, _listing, _plan, _proposal

    runner = FakeRunner(
        _listing(
            _proposal(),
            _proposal("pr/source.json", state="awaiting-push", create_scope={"kind": "source", "manifest": "source.json"}),
        ),
        allow=lambda argv: not remote or "-y" not in argv,
    )
    pending = []
    ctl = OutboxController(
        runner, title="Outbox", container=C, standalone=False,
        on_change=lambda state: None,
        run_publish=lambda argv, done: pending.append((list(argv), done)),
    )
    ctl.load()
    for pid in ("pr/a.json", "pr/source.json"):
        ctl.intent("mark", (C, pid, None, None))
    ctl.intent("publish", None)
    if not remote:
        runner.finish(JobResult(0, "", _plan()))
    ctl.intent("answer", None, "publish")
    assert pending[0][0][:2] == ["outbox", "apply"]
    pending[0][1](JobResult(0, "", "https://github.com/o/r/pull/42"))
    assert pending[1][0] == ["pr", "push", C, "--source-manifest", "source.json"]
    pending[1][1](JobResult(0, "", "https://github.com/o/r/pull/43"))
    assert not any(kind == "hand_off" for kind, _ in runner.calls)
    assert "https://github.com/o/r/pull/43" in " ".join(ctl.state.status)
