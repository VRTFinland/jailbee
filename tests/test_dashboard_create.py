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


def run_create_child():
    """Real CLI + prompt channel; only host effects are replaced in the child."""
    import pickle
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    from jailbee import create_flow
    from jailbee.global_config import GlobalConfig

    cfg = pickle.loads(Path(sys.argv[1]).read_bytes())
    created_file = Path(sys.argv[2])

    def launch(cfg, opts, **kwargs):
        created_file.write_text(opts.container_branch)
        return SimpleNamespace(pid=123, log_path=created_file.with_suffix(".log"))

    with ExitStack() as stack:
        for name in ("_advise_setup", "_advise_upgrade", "_advise_ssh_restart", "_advise_update"):
            stack.enter_context(patch(f"jailbee.cli.{name}"))
        stack.enter_context(patch("jailbee.cli._load_or_exit", return_value=cfg))
        stack.enter_context(patch("jailbee.cli._load_global", return_value=GlobalConfig()))
        stack.enter_context(patch("jailbee.cli._resolve_config_path_or_none", return_value=None))
        incus = MagicMock()
        incus.exists.return_value = False
        stack.enter_context(patch("jailbee.incus.Incus", return_value=incus))
        stack.enter_context(patch("jailbee.git.branch_exists_in_source", return_value=True))
        stack.enter_context(patch("jailbee.lifecycle.check_storage_pool"))
        stack.enter_context(patch.object(create_flow, "host_extras", return_value=create_flow.HostExtras(None, None, None, ())))
        stack.enter_context(patch.object(create_flow, "ensure_profiles"))
        stack.enter_context(patch.object(create_flow, "register_and_preflight"))
        stack.enter_context(patch.object(create_flow, "preflight_background", side_effect=lambda cfg, opts, **kw: opts))
        stack.enter_context(patch.object(create_flow, "launch_background", side_effect=launch))
        prompting.adopt_channel()
        result = CliRunner().invoke(cli_app, sys.argv[3:])
        prompting.drop_channel()
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.exit_code)


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


@pytest.mark.parametrize("answer", ["y", "n", "escape"])
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

    # Retry runs the real CLI in a separate process so CliRunner's process-wide
    # stream capture cannot interfere with Textual's active event loop.
    import pickle

    config_file = tmp_path / "cfg.pickle"
    config_file.write_bytes(pickle.dumps(cfg))
    created_file = tmp_path / "created"
    calls = []

    def spawn(argv, *, cwd, env, prompt_sock):
        calls.append(list(argv))
        return runner.popen_spawn(
            [sys.executable, "-c",
             "from tests.test_dashboard_create import run_create_child; run_create_child()",
             str(config_file), str(created_file), *argv[3:]],
            cwd=Path(__file__).resolve().parents[1], env=env, prompt_sock=prompt_sock,
        )

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
    assert created_file.exists() is (answer == "y")
    assert calls[0][-7:] == ["new", "--background", "--autostart-profile", "none", "--", "dev-2", "dev"]
