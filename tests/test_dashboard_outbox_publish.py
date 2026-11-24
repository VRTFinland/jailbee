"""Outbox publications use the shared prompt runner without a terminal frontend."""

import pytest

from jailbee.dashboard.jobs import JobResult
from jailbee.dashboard.notifications import NotificationQueue
from jailbee.dashboard.runner import ActionOutcome, ActionRun, RunResult
from tests.test_branch_pr import source_publisher as source_publisher
from tests.test_cli_outbox import env as env
from tests.test_cli_outbox import publication_env as publication_env


@pytest.mark.parametrize("transport", ["none", "ssh"])
@pytest.mark.parametrize("kind", ["pr", "pr-choice", "issue", "source", "optional-ai"])
def test_cli_declined_publication_is_not_success(
    request, mocker, monkeypatch, tmp_path, transport, kind
):
    import threading

    from typer.testing import CliRunner

    from jailbee import prompting
    from jailbee.cli import app
    from jailbee.config.models_remote import RemoteCommandPolicy, RemotePolicy
    from jailbee.dashboard.outbox_publish import publish
    from jailbee.dashboard.runner import run_actions
    from jailbee.remote_ssh.session import child_environment

    if kind == "optional-ai":
        from tests.conftest import mock_pr_agent
        from tests.test_cli_pr import _pr_created, _publish_result, _setup

        cfg, _ = _setup(mocker, tmp_path)
        mock_pr_agent(cfg, True)
        mocker.patch("jailbee.sync.publish_branch_from_container", return_value=_publish_result())
        mocker.patch("jailbee.git.commit_subject", return_value="feat: do thing")
        create = mocker.patch("jailbee.pr.create_pr", return_value=_pr_created())
        argv = ["pr", "push", "feat-foo"]
        mutations = []
    elif kind == "source":
        p = request.getfixturevalue("source_publisher")
        mocker.patch("jailbee.config.load_repo_config", return_value=p["cfg"])
        mocker.patch("jailbee.incus.Incus", return_value=p["incus"])
        argv = ["pr", "push", "full", "--source-manifest", "m.json"]
        mutations = [p["push"], p["create"]]
    else:
        _, *mutations = request.getfixturevalue("publication_env")
        proposal_kind = "pr" if kind == "pr-choice" else kind
        argv = ["outbox", "apply", "feature", f"{proposal_kind}/001.json"]
        if kind == "pr":
            argv.append("--draft")
    if transport == "ssh":
        for key, value in child_environment({}, transport="ssh", restricted=False).items():
            monkeypatch.setenv(key, value)

    cli_results = []
    answers = []

    class CliChild:
        stdout = None

        def __init__(self, args, prompt_sock):
            monkeypatch.setenv("JAILBEE_PROMPT_FD", str(prompt_sock.fileno()))
            self.sock = prompt_sock
            self.thread = threading.Thread(target=self.invoke, args=(args,))
            self.thread.start()

        def invoke(self, args):
            try:
                cli_results.append(CliRunner().invoke(app, args))
            finally:
                prompting.drop_channel()
                self.sock.close()

        def wait(self):
            self.thread.join(30)
            assert not self.thread.is_alive()
            return cli_results[0].exit_code

        def kill(self):
            self.sock.close()

    def questions(runs, done, progress):
        def answer(prompt):
            answers.append(prompt)
            return None if prompt.kind == "choose_one" else False

        outcome = run_actions(
            runs,
            transport=transport,
            policy=RemotePolicy(commands=RemoteCommandPolicy(mode="full"), restrict_host=False),
            on_prompt=answer,
            spawn=lambda args, **kw: CliChild(args[3:], kw["prompt_sock"]),
            audit=lambda record: None,
        )
        assert cli_results, outcome
        done(
            ActionOutcome(
                outcome.results, cli_results[0].output.replace("Nothing published.", "Declined.")
            )
        )

    completed = []
    queue = NotificationQueue()
    publish(
        argv,
        route=lambda argv: ActionRun(list(argv), tmp_path),
        target="feature",
        queue=queue,
        run_questions=questions,
        changed=lambda: None,
        done=completed.append,
    )
    assert answers and answers[-1].kind == ("choose_one" if kind == "pr-choice" else "confirm"), (
        cli_results[0].output
    )
    if kind == "optional-ai":
        create.assert_called_once()
        assert completed[0].returncode == 0, cli_results[0].output
        assert next(iter(queue)).state == "done"
        assert "https://github.com/acme/widgets/pull/123" in completed[0].stdout
    else:
        assert completed[0].returncode != 0, cli_results[0].output
        assert next(iter(queue)).state == "failed"
    for mutation in mutations:
        mutation.assert_not_called()


@pytest.mark.parametrize("reason", ["container is gone", "publication is forbidden"])
def test_route_refusal_is_retained_without_starting_questions(tmp_path, reason):
    from jailbee.dashboard.outbox_publish import publish
    from jailbee.remote_ssh.router import RouteError

    queue = NotificationQueue()
    completed = []
    changes = []

    def route(argv):
        raise RouteError(reason)

    def questions(*args):
        pytest.fail("a refused publication must not start")

    publish(
        ["pr", "push", "gone"],
        route=route,
        target="gone",
        queue=queue,
        run_questions=questions,
        changed=lambda: changes.append(True),
        done=completed.append,
    )
    (entry,) = queue
    assert entry.state == "failed" and entry.output == reason
    assert completed == [JobResult(1, "", reason)]
    assert changes


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
        route=lambda argv: ActionRun(list(argv), tmp_path),
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
        ["issue", "apply", "alpha-x", "a.json"],
        route=lambda argv: ActionRun(list(argv), tmp_path),
        target="alpha-x",
        queue=queue,
        run_questions=questions,
        changed=lambda: None,
        done=lambda result: None,
    )
    (entry,) = queue
    assert entry.state == "done" and entry.output == url


@pytest.mark.parametrize("status", [0, 1])
def test_status_is_only_the_outcome_while_queue_keeps_full_log(tmp_path, status):
    from jailbee.dashboard.outbox_publish import publish

    queue = NotificationQueue()
    completed = []
    conclusion = (
        "opened PR #42 Title: https://github.com/o/r/pull/42" if not status else "permission denied"
    )
    output = (
        f"Plan: https://github.com/o/r/pull/1\nDescription preview\nhttps://example.com/design\n{conclusion}\n"
        "Nothing published. Additional comments can be posted later.\n"
        if not status
        else f"Error: {conclusion}\nStopped here; still pending: b.json\n"
    )

    def questions(runs, done, progress):
        done(ActionOutcome([RunResult(status=status)], output))

    publish(
        ["pr", "push", "alpha-x"],
        route=lambda argv: ActionRun(list(argv), tmp_path),
        target="alpha-x",
        queue=queue,
        run_questions=questions,
        changed=lambda: None,
        done=completed.append,
    )
    assert next(iter(queue)).output == output
    assert completed == [JobResult(status, "", conclusion)]


def test_declining_optional_questions_does_not_cancel_successful_pr(tmp_path):
    from jailbee.dashboard.outbox_publish import publish

    completed = []
    queue = NotificationQueue()
    url = "https://github.com/o/r/pull/42"

    def questions(runs, done, progress):
        done(
            ActionOutcome(
                [RunResult(status=0, declined=True)],
                f"PR #42 created: {url}\nNothing published. Comments stay pending.\n",
            )
        )

    publish(
        ["pr", "push", "alpha-x"],
        route=lambda argv: ActionRun(list(argv), tmp_path),
        target="alpha-x",
        queue=queue,
        run_questions=questions,
        changed=lambda: None,
        done=completed.append,
    )
    assert next(iter(queue)).state == "done"
    assert completed == [JobResult(0, "", f"PR #42 created: {url}")]


@pytest.mark.parametrize("cancelled, declined", [(True, False), (False, True)])
def test_cancelled_apply_does_not_report_success(tmp_path, cancelled, declined):
    from jailbee.dashboard.outbox_publish import publish

    queue = NotificationQueue()
    completed = []

    def questions(runs, done, progress):
        done(
            ActionOutcome([RunResult(status=0, cancelled=cancelled, declined=declined)], "preview")
        )

    publish(
        ["outbox", "apply", "alpha-x", "pr/a.json"],
        route=lambda argv: ActionRun(list(argv), tmp_path),
        target="alpha-x",
        queue=queue,
        run_questions=questions,
        changed=lambda: None,
        done=completed.append,
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
            _proposal(
                "pr/source.json",
                state="awaiting-push",
                create_scope={"kind": "source", "manifest": "source.json"},
            ),
        ),
        allow=lambda argv: not remote or "-y" not in argv,
    )
    pending = []
    ctl = OutboxController(
        runner,
        title="Outbox",
        container=C,
        standalone=False,
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


@pytest.mark.parametrize("dashboard", [False, True])
def test_closing_during_creation_batch_keeps_dashboard_work_but_not_terminal_handoff(dashboard):
    from jailbee.dashboard.outbox_flow import OutboxController
    from tests.test_dashboard_outbox_flow import C, FakeRunner, _listing, _proposal

    runner = FakeRunner(
        _listing(
            _proposal("pr/a.json", state="awaiting-pr", create_scope={"kind": "repo"}),
            _proposal(
                "pr/b.json",
                state="awaiting-push",
                create_scope={"kind": "source", "manifest": "b.json"},
            ),
        )
    )
    pending = []
    ctl = OutboxController(
        runner,
        title="Outbox",
        container=C,
        standalone=False,
        on_change=lambda state: None,
        run_publish=(lambda argv, done: pending.append((list(argv), done))) if dashboard else None,
    )
    ctl.load()
    for pid in ("pr/a.json", "pr/b.json"):
        ctl.intent("mark", (C, pid, None, None))
    ctl.intent("publish", None)
    runner.hold_loads = True
    ctl.intent("answer", None, "publish")
    ctl.shown = lambda: False
    runner.finish_loads()
    if dashboard:
        assert len(pending) == 1
        pending[0][1](JobResult(0, "", "https://github.com/o/r/pull/42"))
        runner.finish_loads()
        assert len(pending) == 2
        pending[1][1](JobResult(0, "", "https://github.com/o/r/pull/43"))
        runner.finish_loads()
        assert "https://github.com/o/r/pull/43" in " ".join(ctl.state.status)
    else:
        runner.finish_loads()
        assert "outbox box closed" in " ".join(ctl.state.status)
    assert not any(kind == "hand_off" for kind, _ in runner.calls)
