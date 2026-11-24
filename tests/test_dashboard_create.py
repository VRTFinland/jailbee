"""Dashboard creation: detached preflight, prompt-channel retry and durable failures."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee import prompting
from jailbee.cli import app as cli_app
from jailbee.dashboard import runner
from jailbee.dashboard.jobs import JobResult, JobRunner, needs_terminal
from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.dashboard.tui import session as ts
from tests.dashboard_pilot import keys, make_app
from tests.test_cli_new_detach import _setup_new
from tests.test_dashboard_tui_runner import _until


@pytest.fixture
def existing_branch(mocker, tmp_path):
    cfg = _setup_new(tmp_path, mocker)
    (cfg.repo_root / ".git").mkdir()
    mocker.patch("jailbee.git.branch_exists_in_source", return_value=True)
    mocker.patch.object(prompting, "select_backend", return_value=prompting.NonInteractiveBackend())
    result = CliRunner().invoke(
        cli_app, ["new", "--background", "--autostart-profile", "none", "--", "dev-2", "dev"]
    )
    return cfg, JobResult(result.exit_code, result.stderr, result.stdout)


def test_real_cli_existing_branch_is_recognized_as_needing_a_question(existing_branch):
    _, result = existing_branch
    assert result.returncode == 2
    assert "already exists in source repo" in result.stdout
    assert "already exists in source repo" not in result.stderr
    assert "confirmation needed" in result.stderr
    assert needs_terminal(result)


@pytest.mark.parametrize("answer", ["y", "n"])
def test_existing_branch_create_asks_in_a_modal_and_preserves_decline_hint(
    mocker, tmp_path, existing_branch, answer
):
    cfg, detached = existing_branch
    group = RepoGroup(
        "myrepo", str(cfg.repo_root), None, [], autostart_profiles=("none", "all"),
        autostart_default_profile="all",
    )
    # JobRunner is real. Its stand-in CLI prints exactly the streams and status
    # produced above by the real non-interactive `jailbee new`.
    fake = tmp_path / "jailbee"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"sys.stdout.write({detached.stdout!r})\n"
        f"sys.stderr.write({detached.stderr!r})\n"
        f"sys.exit({detached.returncode})\n"
    )
    fake.chmod(0o755)
    mocker.patch.dict(os.environ, {"PATH": f"{tmp_path}:" + os.environ["PATH"]})
    mocker.patch.object(ts, "new_container_base_default", return_value="dev")
    mocker.patch.object(ts, "host_branches", return_value=())

    # Derive both retry outcomes from the real CLI too, with host effects
    # mocked at create_flow's public boundary.
    from jailbee import create_flow

    mocker.patch.object(create_flow, "host_extras", return_value=create_flow.HostExtras(None, None, None, ()))
    mocker.patch("jailbee.lifecycle.check_storage_pool")
    mocker.patch.object(create_flow, "ensure_profiles")
    mocker.patch.object(create_flow, "register_and_preflight")
    mocker.patch.object(create_flow, "preflight_background", side_effect=lambda cfg, opts, **kw: opts)
    created = mocker.patch.object(create_flow, "launch_background")
    created.return_value.pid = 123
    created.return_value.log_path = tmp_path / "create.log"
    import jailbee.incus

    jailbee.incus.Incus.return_value.exists.return_value = False

    from jailbee.prompt_channel import Answer, CommandEnd, Prompt

    backend = mocker.Mock(wraps=prompting.NonInteractiveBackend())
    backend.confirm.return_value = answer == "y"
    mocker.patch.object(prompting, "select_backend", return_value=backend)
    retry = CliRunner().invoke(
        cli_app, ["new", "--background", "--autostart-profile", "none", "--", "dev-2", "dev"]
    )
    assert retry.exit_code == 0, retry.output
    assert created.call_count == (1 if answer == "y" else 0)
    question = backend.confirm.call_args.args[0]
    answers = []
    calls = []

    def spawn(argv, *, cwd, env, prompt_sock):
        import threading

        read_fd, write_fd = os.pipe()
        calls.append(list(argv))

        def run():
            end = CommandEnd(prompt_sock)
            try:
                answers.append(end.ask(lambda i: Prompt(id=i, kind="confirm", question=question, default=True)))
                with os.fdopen(write_fd, "wb") as output:
                    output.write(retry.output.encode())
            finally:
                end.close()

        thread = threading.Thread(target=run)
        thread.start()

        class Child:
            stdout = os.fdopen(read_fd, "rb")

            def wait(self):
                thread.join(30)
                return retry.exit_code

            def kill(self):
                prompt_sock.close()

        return Child()

    app = make_app(mocker, [group], jobs=JobRunner)
    app.threaded_actions = True
    app.action_runner = runner.run_action
    app.action_spawn = spawn

    async def main():
        async with app.run_test() as pilot:
            await pilot.press("n", *keys("dev-2"), "enter", "enter", "down", "enter")
            def modal_ready():
                app.session.tick()
                return isinstance(app.screen, pm.ConfirmModal)

            await _until(pilot, modal_ready, "branch reuse modal")
            await pilot.press(answer)
            await _until(pilot, lambda: any(e.finished for e in app.session.queue), "create result")
            assert not any(e.state == "failed" for e in app.session.queue)
            if answer == "n":
                assert "Pick a different name" in app.session.notice
            await pilot.press("ctrl+c")

    asyncio.run(asyncio.wait_for(main(), 180))
    assert len(calls) == 1
    assert isinstance(answers[0], Answer)
    assert answers[0].value is (answer == "y")
    assert calls[0][-7:] == ["new", "--background", "--autostart-profile", "none", "--", "dev-2", "dev"]
