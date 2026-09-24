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


def test_console_without_initial_repo_opens_the_arrow_key_menu(
    console_env: ConsoleEnv, mocker
) -> None:
    select = mocker.patch(
        "jailbee.remote_ssh.console._select_repo",
        return_value=console.RepoChoice("other", console_env.other_root),
    )
    console_env.lines(["exit"])

    assert console.run() == 0
    assert select.call_args.args[0] == [
        console.RepoChoice("other", console_env.other_root),
        console.RepoChoice("project", console_env.repo_root),
    ]
    prompts = [call.args[0] for call in console_env.prompt.prompt.call_args_list]
    assert prompts == ["jb[other]> "]


def test_console_without_initial_repo_exits_cleanly_when_the_menu_is_cancelled(
    console_env: ConsoleEnv, mocker
) -> None:
    """Esc/Ctrl-C/Ctrl-D in the start menu all answer `None` from `_select_repo`."""
    mocker.patch("jailbee.remote_ssh.console._select_repo", return_value=None)

    assert console.run() == 0
    console_env.prompt.prompt.assert_not_called()


def test_console_with_a_single_registered_repo_skips_the_menu(tmp_path: Path, mocker) -> None:
    root = tmp_path / "solo"
    root.mkdir()
    solo = console.RepoChoice("solo", root)
    mocker.patch("jailbee.remote_ssh.console.registered_repos", return_value=[solo])
    prompt = mocker.Mock()
    prompt.prompt.side_effect = ["exit"]
    mocker.patch("jailbee.remote_ssh.console.PromptSession", return_value=prompt)
    mocker.patch("jailbee.remote_ssh.console.state_dir", return_value=tmp_path)
    config = GlobalConfig(
        remote=RemoteConfig(
            ssh=RemoteSSHConfig(shell=True, commands=RemoteCommandPolicy(mode="full"))
        )
    )
    mocker.patch("jailbee.remote_ssh.console.load_global_config", return_value=(config, []))
    mocker.patch("jailbee.remote_ssh.console.default_global_config_path", return_value=tmp_path)
    select = mocker.patch("jailbee.remote_ssh.console._select_repo")

    assert console.run() == 0
    select.assert_not_called()
    assert prompt.prompt.call_args_list[0].args[0] == "jb[solo]> "


def test_use_with_no_argument_opens_the_arrow_key_menu(console_env: ConsoleEnv, mocker) -> None:
    select = mocker.patch(
        "jailbee.remote_ssh.console._select_repo",
        return_value=console.RepoChoice("other", console_env.other_root),
    )
    console_env.lines(["use", "exit"])

    console.run("project")

    assert select.call_args.args[0] == [
        console.RepoChoice("other", console_env.other_root),
        console.RepoChoice("project", console_env.repo_root),
    ]
    prompts = [call.args[0] for call in console_env.prompt.prompt.call_args_list]
    assert prompts == ["jb[project]> ", "jb[other]> "]


def test_use_with_no_argument_stays_put_when_the_menu_is_cancelled(
    console_env: ConsoleEnv, mocker
) -> None:
    mocker.patch("jailbee.remote_ssh.console._select_repo", return_value=None)
    console_env.lines(["use", "exit"])

    assert console.run("project") == 0
    prompts = [call.args[0] for call in console_env.prompt.prompt.call_args_list]
    assert prompts == ["jb[project]> ", "jb[project]> "]


def test_use_with_prefix_argument_does_not_open_a_menu(console_env: ConsoleEnv, mocker) -> None:
    select = mocker.patch("jailbee.remote_ssh.console._select_repo")
    console_env.lines(["use other", "exit"])

    console.run("project")

    select.assert_not_called()


def test_use_with_no_argument_and_no_registered_repos_reports_an_error(
    console_env: ConsoleEnv, mocker, capsys
) -> None:
    # First call is `run()`'s own startup check (must still see the repos
    # `console_env` registered, since `initial_repo` is given below); the
    # second is the fresh lookup `use` with no argument makes for itself.
    mocker.patch(
        "jailbee.remote_ssh.console.registered_repos",
        side_effect=[
            [
                console.RepoChoice("other", console_env.other_root),
                console.RepoChoice("project", console_env.repo_root),
            ],
            [],
        ],
    )
    select = mocker.patch("jailbee.remote_ssh.console._select_repo")
    console_env.lines(["use", "exit"])

    assert console.run("project") == 0
    select.assert_not_called()
    assert "No registered repositories" in capsys.readouterr().err


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


def test_help_renders_console_panel_and_runs_real_jailbee_help_in_full_mode(
    console_env: ConsoleEnv, mocker, capsys
) -> None:
    """`console_env`'s policy is `commands.mode: full`.

    `full` mode delegates the Jailbee half of `help` entirely to a real
    `python -m jailbee --help` child (run the same way every other console
    command is: through `_run_foreground`, i.e. `subprocess.run` under the
    hood) instead of rendering its own approximation of Typer's command
    list.
    """
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 0),
    )
    console_env.lines(["help", "exit"])

    console.run("project")

    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "--help"],
        cwd=console_env.repo_root,
        check=False,
    )
    output = capsys.readouterr().out
    assert "Console" in output
    for command in ["repos", "use [PREFIX]", "dashboard", "help", "exit"]:
        assert command in output
    assert "Allowed Jailbee commands" not in output
    assert "--help" in output


def test_help_lists_only_the_allowed_commands_in_allowlist_mode(
    console_env: ConsoleEnv, mocker, capsys
) -> None:
    """`allow: [ls]` renders a second panel with `ls`'s own Click short help,
    and never falls through to a real `jailbee --help` child."""
    config = GlobalConfig(
        remote=RemoteConfig(
            ssh=RemoteSSHConfig(
                shell=True,
                commands=RemoteCommandPolicy(mode="allowlist", allow=["ls"]),
            )
        )
    )
    mocker.patch("jailbee.remote_ssh.console.load_global_config", return_value=(config, []))
    run = mocker.patch("jailbee.remote_ssh.console.subprocess.run")
    console_env.lines(["help", "exit"])

    console.run("project")

    run.assert_not_called()
    output = capsys.readouterr().out
    assert "Allowed Jailbee commands" in output
    assert "ls" in output
    assert "List managed containers." in output  # `ls`'s real Click short help
    assert "git" not in output


def test_help_says_commands_are_disabled_in_disabled_mode(
    console_env: ConsoleEnv, mocker, capsys
) -> None:
    config = GlobalConfig(remote=RemoteConfig(ssh=RemoteSSHConfig(dashboard=True)))
    mocker.patch("jailbee.remote_ssh.console.load_global_config", return_value=(config, []))
    run = mocker.patch("jailbee.remote_ssh.console.subprocess.run")
    console_env.lines(["help", "exit"])

    console.run("project")

    run.assert_not_called()
    output = capsys.readouterr().out
    assert "Jailbee commands are disabled by the remote.ssh policy." in output
    assert "Allowed Jailbee commands" not in output


def test_help_hides_the_dashboard_row_when_dashboard_is_disabled(
    console_env: ConsoleEnv, mocker, capsys
) -> None:
    config = GlobalConfig(
        remote=RemoteConfig(
            ssh=RemoteSSHConfig(
                shell=True,
                dashboard=False,
                commands=RemoteCommandPolicy(mode="full"),
            )
        )
    )
    mocker.patch("jailbee.remote_ssh.console.load_global_config", return_value=(config, []))
    mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 0),
    )
    console_env.lines(["help", "exit"])

    console.run("project")

    output = capsys.readouterr().out
    assert "dashboard" not in output
    for command in ["repos", "use [PREFIX]", "help", "exit"]:
        assert command in output


def test_dashboard_runs_and_returns_to_prompt(console_env: ConsoleEnv, mocker) -> None:
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 7),
    )
    console_env.lines(["dashboard", "exit"])

    assert console.run("project") == 7
    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "dashboard"],
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


def test_alias_command_runs_via_its_literal_argv_in_full_mode(
    console_env: ConsoleEnv, mocker
) -> None:
    """`merge` is a hidden alias for `git merge`; full mode permits it (Problem A)."""
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 0),
    )
    console_env.lines(["merge --into main", "exit"])

    console.run("project")

    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "merge", "--into", "main"],
        cwd=console_env.repo_root,
        check=False,
    )


def test_bare_group_help_runs_instead_of_being_rejected(console_env: ConsoleEnv, mocker) -> None:
    """A bare public group path is a pure help invocation (Problem B)."""
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 0),
    )
    console_env.lines(["git", "exit"])

    console.run("project")

    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "git"],
        cwd=console_env.repo_root,
        check=False,
    )


def test_unknown_command_is_delegated_to_jailbee_for_its_own_error(
    console_env: ConsoleEnv, mocker
) -> None:
    """Problem C: a genuinely unknown name gets Jailbee's own error, not ours."""
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 2),
    )
    console_env.lines(["nosuchcmd --flag", "exit"])

    assert console.run("project") == 2
    run.assert_called_once_with(
        [sys.executable, "-m", "jailbee", "nosuchcmd", "--flag"],
        cwd=console_env.repo_root,
        check=False,
    )


def test_hidden_internal_command_is_still_rejected_by_the_console(
    console_env: ConsoleEnv, mocker
) -> None:
    """A first token naming a hidden internal command is not delegated."""
    run = mocker.patch("jailbee.remote_ssh.console.subprocess.run")
    console_env.lines(["_remote-console", "exit"])

    console.run("project")

    run.assert_not_called()


def test_console_errors_use_jailbees_own_error_style(console_env: ConsoleEnv, capsys) -> None:
    """Problem D: console-side rejections look like Jailbee's own errors."""
    console_env.lines(["_remote-console", "exit"])

    console.run("project")

    err = capsys.readouterr().err
    assert "✗" in err  # the '✗' marker `jailbee.tui.error_plain` uses
    assert "unknown Jailbee command" in err


def test_history_is_private_and_completion_is_nested(
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
    completer = kwargs["completer"]
    assert isinstance(completer, console.NestedCompleter)
    assert set(completer.options) == {
        "dashboard",
        "exit",
        "git",
        "help",
        "ls",
        "repos",
        "use",
    }
    assert completer.options["git"] is not None
    assert set(completer.options["use"].options) == {"other", "project"}


def _completions(completer, text: str) -> set[str]:
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    return {c.text for c in completer.get_completions(Document(text, len(text)), CompleteEvent())}


def test_completer_offers_the_second_word_of_a_multi_word_command() -> None:
    completer = console._completer(
        frozenset({"git pull", "git push", "ls"}),
        [console.RepoChoice("proj", Path("/tmp/proj"))],
    )

    assert _completions(completer, "git p") == {"pull", "push"}


def test_completer_omits_a_command_not_in_the_active_policy() -> None:
    completer = console._completer(
        frozenset({"ls"}),  # allowlist mode: "git pull" is not allowed
        [],
    )

    assert _completions(completer, "") == {"ls", *console._LOCAL_COMMANDS}
    assert _completions(completer, "git ") == set()


def test_completer_completes_registered_repo_prefixes_after_use() -> None:
    completer = console._completer(
        frozenset(),
        [console.RepoChoice("alpha", Path("/tmp/a")), console.RepoChoice("beta", Path("/tmp/b"))],
    )

    assert _completions(completer, "use ") == {"alpha", "beta"}


# --- _select_repo(): drive the real Application via a pipe, no mocking ---

_SELECT_REPO_CHOICES = [
    console.RepoChoice("alpha", Path("/tmp/alpha")),
    console.RepoChoice("beta", Path("/tmp/beta")),
]


@pytest.fixture
def _select_repo_io():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe_input:

        def run(keys: str):
            pipe_input.send_text(keys)
            return console._select_repo(
                _SELECT_REPO_CHOICES, input=pipe_input, output=DummyOutput()
            )

        yield run


def test_select_repo_bare_enter_returns_the_first_repo(_select_repo_io) -> None:
    assert _select_repo_io("\r") == _SELECT_REPO_CHOICES[0]


def test_select_repo_arrow_down_then_enter_returns_the_second_repo(_select_repo_io) -> None:
    assert _select_repo_io("\x1b[B\r") == _SELECT_REPO_CHOICES[1]


def test_select_repo_ctrl_c_cancels(_select_repo_io) -> None:
    assert _select_repo_io("\x03") is None


def test_select_repo_ctrl_d_cancels(_select_repo_io) -> None:
    """Regression: upstream `questionary.select` leaves Ctrl-D hanging forever."""
    assert _select_repo_io("\x04") is None


def test_select_repo_escape_cancels(_select_repo_io) -> None:
    """Regression: upstream `questionary.select` leaves a bare Esc hanging forever."""
    assert _select_repo_io("\x1b") is None


def test_console_refuses_a_host_path_argument_without_running_anything(
    console_env: ConsoleEnv, mocker, capsys
) -> None:
    config = GlobalConfig(
        remote=RemoteConfig(
            ssh=RemoteSSHConfig(shell=True, commands=RemoteCommandPolicy(mode="full"))
        )
    )
    mocker.patch("jailbee.remote_ssh.console.load_global_config", return_value=(config, []))
    run = mocker.patch(
        "jailbee.remote_ssh.console.subprocess.run",
        return_value=CompletedProcess([], 0),
    )
    console_env.lines(["ls --config /etc/passwd", "new feat -m", "exit"])

    console.run("project")

    run.assert_not_called()
    err = capsys.readouterr().err
    assert "may not set --config: ls" in err
    assert "may not set --mount: new" in err
