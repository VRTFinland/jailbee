"""Tests for the restricted interactive remote Jailbee console."""

from __future__ import annotations

import os
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from jailbee.config.models_remote import RemoteCommandPolicy, RemoteConfig, RemoteSSHConfig
from jailbee.db.models import RegisteredRepo
from jailbee.global_config import GlobalConfig
from jailbee.remote_ssh import console
from jailbee.remote_ssh.router import RouteError


@dataclass
class ConsoleEnv:
    prompt: Mock
    repo_root: Path
    other_root: Path

    def lines(self, values: list[str | BaseException]) -> None:
        self.prompt.prompt.side_effect = values


@pytest.fixture
def console_env(tmp_path: Path, mocker) -> ConsoleEnv:
    repo_root = tmp_path / "project"
    other_root = tmp_path / "other"
    repo_root.mkdir()
    other_root.mkdir()
    repos = [
        console.RepoChoice("other", other_root),
        console.RepoChoice("project", repo_root),
    ]
    prompt = mocker.Mock()
    mocker.patch("jailbee.remote_ssh.console.PromptSession", return_value=prompt)
    mocker.patch("jailbee.remote_ssh.console.registered_repos", return_value=repos)
    mocker.patch(
        "jailbee.remote_ssh.console.resolve_repo",
        side_effect=lambda prefix: {repo.prefix: repo.root for repo in repos}[prefix],
    )
    config = GlobalConfig(
        remote=RemoteConfig(
            ssh=RemoteSSHConfig(
                shell=True,
                commands=RemoteCommandPolicy(mode="full"),
            )
        )
    )
    mocker.patch("jailbee.remote_ssh.console.load_global_config", return_value=(config, []))
    mocker.patch("jailbee.remote_ssh.console.default_global_config_path", return_value=tmp_path)
    mocker.patch("jailbee.remote_ssh.console.state_dir", return_value=tmp_path)
    mocker.patch(
        "jailbee.remote_ssh.console.known_command_paths",
        return_value=frozenset({"git pull", "ls"}),
    )
    return ConsoleEnv(prompt, repo_root, other_root)


def test_registered_repos_returns_only_existing_directories_sorted(
    tmp_path: Path, db_engine: Engine
) -> None:
    beta = tmp_path / "beta"
    alpha = tmp_path / "alpha"
    beta.mkdir()
    alpha.mkdir()
    with Session(db_engine) as session:
        for prefix, root in [
            ("beta", beta),
            ("stale", tmp_path / "missing"),
            ("alpha", alpha),
        ]:
            session.add(
                RegisteredRepo(
                    container_prefix=prefix,
                    repo_root=str(root),
                    registered_at=datetime(2026, 9, 17, tzinfo=UTC),
                )
            )
        session.commit()

    assert console.registered_repos(engine=db_engine) == [
        console.RepoChoice("alpha", alpha),
        console.RepoChoice("beta", beta),
    ]


def test_parse_console_line_uses_shell_quoting_without_interpreting_operators() -> None:
    assert console.parse_console_line('ls "two words" | cat && true') == (
        "ls",
        "two words",
        "|",
        "cat",
        "&&",
        "true",
    )


def test_parse_console_line_rejects_malformed_quotes() -> None:
    with pytest.raises(RouteError, match="cannot parse console command"):
        console.parse_console_line('ls "unterminated')


def test_console_runs_jailbee_argv_without_a_shell(console_env: ConsoleEnv, mocker) -> None:
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 0),
    )
    console_env.lines(["ls --all", "exit"])

    assert console.run("project") == 0
    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "ls", "--all"],
        cwd=console_env.repo_root,
        check=False,
    )


def test_shell_punctuation_remains_literal_argv(console_env: ConsoleEnv, mocker) -> None:
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 0),
    )
    console_env.lines(["ls | uname && whoami", "exit"])

    console.run("project")

    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "ls", "|", "uname", "&&", "whoami"],
        cwd=console_env.repo_root,
        check=False,
    )


def test_use_switches_the_prompt_repo(console_env: ConsoleEnv, mocker) -> None:
    console_env.lines(["use other", "ls", "exit"])
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 0),
    )

    console.run("project")

    assert run.call_args.kwargs["cwd"] == console_env.other_root
    prompts = [call.args[0] for call in console_env.prompt.prompt.call_args_list]
    assert prompts == ["jb[project]> ", "jb[other]> ", "jb[other]> "]


def test_console_without_initial_repo_prompts_for_a_registered_repo(
    console_env: ConsoleEnv,
) -> None:
    console_env.lines(["other", "exit"])

    assert console.run() == 0
    prompts = [call.args[0] for call in console_env.prompt.prompt.call_args_list]
    assert prompts == ["Select repository: ", "jb[other]> "]


def test_console_reports_when_no_registered_repos(console_env: ConsoleEnv, mocker, capsys) -> None:
    mocker.patch("jailbee.remote_ssh.console.registered_repos", return_value=[])

    assert console.run() == 1
    assert "No registered repositories" in capsys.readouterr().err
    console_env.prompt.prompt.assert_not_called()


def test_initial_stale_repo_is_rejected(console_env: ConsoleEnv, mocker, capsys) -> None:
    mocker.patch(
        "jailbee.remote_ssh.console.resolve_repo",
        side_effect=RouteError("registered repo directory is missing: project"),
    )

    assert console.run("project") == 1
    assert "registered repo directory is missing: project" in capsys.readouterr().err


def test_repos_lists_registered_prefixes_and_roots(console_env: ConsoleEnv, capsys) -> None:
    console_env.lines(["repos", "exit"])

    console.run("project")

    output = capsys.readouterr().out
    assert f"other\t{console_env.other_root}" in output
    assert f"project\t{console_env.repo_root}" in output


def test_help_lists_only_console_local_commands(console_env: ConsoleEnv, capsys) -> None:
    console_env.lines(["help", "exit"])

    console.run("project")

    output = capsys.readouterr().out
    for command in ["repos", "use PREFIX", "dashboard", "help", "exit"]:
        assert command in output


def test_dashboard_runs_registered_only_and_returns_to_prompt(
    console_env: ConsoleEnv, mocker
) -> None:
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 7),
    )
    console_env.lines(["dashboard", "exit"])

    assert console.run("project") == 7
    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "dashboard", "--registered-only"],
        cwd=console_env.repo_root,
        check=False,
    )
    assert console_env.prompt.prompt.call_count == 2


def test_disabled_dashboard_is_rejected_locally(console_env: ConsoleEnv, mocker, capsys) -> None:
    config = GlobalConfig(
        remote=RemoteConfig(
            ssh=RemoteSSHConfig(
                dashboard=False,
                shell=True,
                commands=RemoteCommandPolicy(mode="full"),
            )
        )
    )
    mocker.patch("jailbee.remote_ssh.console.load_global_config", return_value=(config, []))
    run = mocker.patch("jailbee.remote_ssh.console.subprocess.run")
    console_env.lines(["dashboard", "exit"])

    assert console.run("project") == 0
    assert "dashboard is disabled" in capsys.readouterr().err
    run.assert_not_called()


def test_console_reuses_policy_loaded_once(console_env: ConsoleEnv, mocker, capsys) -> None:
    config = GlobalConfig(
        remote=RemoteConfig(
            ssh=RemoteSSHConfig(
                shell=True,
                commands=RemoteCommandPolicy(mode="allowlist", allow=["ls"]),
            )
        )
    )
    load = mocker.patch(
        "jailbee.remote_ssh.console.load_global_config",
        return_value=(config, []),
    )
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 0),
    )
    console_env.lines(["ls", "git pull", "exit"])

    console.run("project")

    load.assert_called_once()
    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "ls"],
        cwd=console_env.repo_root,
        check=False,
    )
    assert "not allowed: git pull" in capsys.readouterr().err


def test_policy_json_wins_over_a_stricter_global_yaml(console_env: ConsoleEnv, mocker) -> None:
    """Regression: the console used to reload `global.yaml` itself and ignore

    the server's effective (post-override) policy entirely. `global.yaml`
    here says `commands.mode: disabled`; only the passed `--policy-json`
    (mode `full`) allows anything to run. `load_global_config` is also
    asserted un-called: a policy_json console must never reload
    `global.yaml` at all, matching "frozen at console startup".
    """
    load = mocker.patch(
        "jailbee.remote_ssh.console.load_global_config",
        return_value=(
            GlobalConfig(
                remote=RemoteConfig(
                    ssh=RemoteSSHConfig(
                        shell=True,
                        dashboard=True,
                        commands=RemoteCommandPolicy(mode="allowlist", allow=["repos"]),
                    )
                )
            ),
            [],
        ),
    )
    policy = RemoteSSHConfig(
        dashboard=True, shell=True, commands=RemoteCommandPolicy(mode="full")
    ).model_dump_json()
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 0),
    )
    console_env.lines(["ls", "exit"])

    assert console.run("project", policy) == 0

    load.assert_not_called()
    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "ls"],
        cwd=console_env.repo_root,
        check=False,
    )


def test_policy_json_dashboard_check_also_uses_the_passed_policy(
    console_env: ConsoleEnv, mocker, capsys
) -> None:
    """Same bug, on the console's own `dashboard` gate."""
    mocker.patch(
        "jailbee.remote_ssh.console.load_global_config",
        return_value=(
            GlobalConfig(
                remote=RemoteConfig(
                    ssh=RemoteSSHConfig(
                        dashboard=True,
                        shell=True,
                        commands=RemoteCommandPolicy(mode="allowlist", allow=["repos"]),
                    )
                )
            ),
            [],
        ),
    )
    policy = RemoteSSHConfig(
        dashboard=False, shell=True, commands=RemoteCommandPolicy(mode="full")
    ).model_dump_json()
    run = mocker.patch("jailbee.remote_ssh.console.subprocess.run")
    console_env.lines(["dashboard", "exit"])

    assert console.run("project", policy) == 0

    assert "dashboard is disabled" in capsys.readouterr().err
    run.assert_not_called()


def test_invalid_policy_json_fails_cleanly(console_env: ConsoleEnv, capsys) -> None:
    """Never trust the server's payload as-is: revalidate, and fail loudly."""
    assert console.run("project", '{"commands": {"mode": "not-a-mode"}}') == 1
    assert "invalid remote SSH policy" in capsys.readouterr().err
    console_env.prompt.prompt.assert_not_called()


def test_malformed_quotes_report_an_error_and_return_to_prompt(
    console_env: ConsoleEnv, mocker, capsys
) -> None:
    run = mocker.patch("jailbee.remote_ssh.console.subprocess.run")
    console_env.lines(['ls "unterminated', "exit"])

    assert console.run("project") == 0
    assert "cannot parse console command" in capsys.readouterr().err
    assert console_env.prompt.prompt.call_count == 2
    run.assert_not_called()


def test_ctrl_d_returns_the_last_child_status(console_env: ConsoleEnv, mocker) -> None:
    mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 4),
    )
    console_env.lines(["ls", EOFError()])

    assert console.run("project") == 4


def test_keyboard_interrupt_returns_to_the_prompt(console_env: ConsoleEnv) -> None:
    console_env.lines([KeyboardInterrupt(), "exit"])

    assert console.run("project") == 0
    assert console_env.prompt.prompt.call_count == 2


def test_ctrl_c_while_a_command_runs_does_not_interrupt_the_console(
    console_env: ConsoleEnv, mocker
) -> None:
    """Regression for final-review finding I1.

    A cooked-mode Ctrl-C delivers SIGINT to the whole foreground process
    group, including this console process, not just the child it started.
    On the old code (no signal disposition change around the wait), that
    SIGINT raises KeyboardInterrupt here while `subprocess.run` is blocked
    in `Popen.wait()`; `subprocess.run`'s own bare `except:` then kills the
    child before re-raising, defeating the child's own Ctrl-C handling. The
    console must ignore SIGINT while a command runs, like an interactive
    shell, and restore its previous disposition afterwards.
    """
    previous = signal.getsignal(signal.SIGINT)

    def fake_run(argv, cwd, check):
        # A real terminal delivers SIGINT to every process in the
        # foreground group at once; simulate that mid-wait.
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        os.kill(os.getpid(), signal.SIGINT)
        return CompletedProcess(argv, 0)

    run = mocker.patch("jailbee.remote_ssh.console.subprocess.run", side_effect=fake_run)
    console_env.lines(["ls", "exit"])

    assert console.run("project") == 0

    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "ls"],
        cwd=console_env.repo_root,
        check=False,
    )
    assert signal.getsignal(signal.SIGINT) is previous


def test_history_is_private_and_completion_is_restricted(
    console_env: ConsoleEnv, mocker, tmp_path: Path
) -> None:
    console_env.lines(["exit"])
    session_type = mocker.patch("jailbee.remote_ssh.console.PromptSession")
    session_type.return_value = console_env.prompt

    console.run("project")

    history = tmp_path / "ssh-console-history"
    assert history.stat().st_mode & 0o777 == 0o600
    kwargs = session_type.call_args.kwargs
    assert Path(kwargs["history"].filename) == history
    assert set(kwargs["completer"].words) == {
        "dashboard",
        "exit",
        "git pull",
        "help",
        "ls",
        "other",
        "project",
        "repos",
        "use",
    }
    assert os.fspath(console_env.repo_root) not in kwargs["completer"].words
