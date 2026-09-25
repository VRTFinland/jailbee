"""CLI coverage for restricted remote SSH administration."""

from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config import ConfigError
from jailbee.remote_ssh.keys import (
    AuthorizedKey,
    SSHDependencyError,
    SSHKeyError,
)
from jailbee.remote_ssh.service import Problem, ProblemSeverity, ServiceStatus
from tests.conftest import flat_output


def test_remote_ssh_help_exposes_the_management_surface() -> None:
    runner = CliRunner()

    remote = runner.invoke(app, ["remote", "--help"])
    ssh = runner.invoke(app, ["remote", "ssh", "--help"])
    keys = runner.invoke(app, ["remote", "ssh", "key", "--help"])

    assert remote.exit_code == ssh.exit_code == keys.exit_code == 0
    assert "ssh" in remote.stdout
    assert all(
        command in ssh.stdout for command in ("enable", "disable", "restart", "status", "serve")
    )
    assert all(command in keys.stdout for command in ("add", "ls", "rm"))


@pytest.mark.parametrize("command", ["enable", "disable", "restart"])
def test_remote_ssh_lifecycle_commands_delegate(command: str, mocker: MockerFixture) -> None:
    action = mocker.patch(f"jailbee.remote_ssh.service.{command}")

    result = CliRunner().invoke(app, ["remote", "ssh", command])

    assert result.exit_code == 0, result.stdout
    action.assert_called_once_with()


def test_remote_ssh_lifecycle_domain_error_is_concise(mocker: MockerFixture) -> None:
    mocker.patch(
        "jailbee.remote_ssh.service.enable",
        side_effect=SSHDependencyError("install jailbee[ssh]"),
    )

    result = CliRunner().invoke(app, ["remote", "ssh", "enable"])

    assert result.exit_code == 1
    assert "install jailbee[ssh]" in result.stderr
    assert "Traceback" not in result.stderr


def _status(*, problems: tuple[Problem, ...] = ()) -> ServiceStatus:
    return ServiceStatus(
        unit_path=Path("/home/user/.config/systemd/user/jailbee-ssh.service"),
        installed=True,
        enabled=False,
        active=False,
        listen="127.0.0.1",
        port=8022,
        entrypoints=("dashboard", "shell"),
        authorized_keys=2,
        problems=problems,
    )


@pytest.fixture(autouse=True)
def _no_real_linger_probe(mocker: MockerFixture) -> MagicMock:
    # `status` prints the same linger tip `enable`'s install path does; none
    # of the tests below want a real `loginctl` subprocess or its output.
    return mocker.patch("jailbee.setup_command.linger_tip")


def test_remote_ssh_status_has_stable_complete_output(mocker: MockerFixture) -> None:
    status = mocker.patch("jailbee.remote_ssh.service.status", return_value=_status())

    result = CliRunner().invoke(app, ["remote", "ssh", "status"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout.splitlines() == [
        "installed: yes",
        "enabled: no",
        "active: no",
        "listen: 127.0.0.1:8022",
        "entry points: dashboard, shell",
        "authorized keys: 2",
    ]
    status.assert_called_once_with()


def test_remote_ssh_status_prints_the_linger_tip(
    mocker: MockerFixture, _no_real_linger_probe: MagicMock
) -> None:
    mocker.patch("jailbee.remote_ssh.service.status", return_value=_status())

    result = CliRunner().invoke(app, ["remote", "ssh", "status"])

    assert result.exit_code == 0, result.stdout
    _no_real_linger_probe.assert_called_once_with()


@pytest.mark.parametrize(
    ("problem", "expected_exit"),
    [
        (Problem(ProblemSeverity.WARNING, "The SSH service unit is not installed."), 0),
        (Problem(ProblemSeverity.WARNING, "The SSH service is not enabled."), 0),
        (Problem(ProblemSeverity.WARNING, "The SSH service is not active."), 0),
        (Problem(ProblemSeverity.WARNING, "No authorized client keys are configured."), 0),
        (Problem(ProblemSeverity.WARNING, "Authorized keys file is missing."), 0),
        (Problem(ProblemSeverity.FATAL, "Global config is invalid: broken YAML"), 1),
        (Problem(ProblemSeverity.FATAL, "Host key is missing."), 1),
        (Problem(ProblemSeverity.FATAL, "Host key mode is 0644; expected 0600."), 1),
        (Problem(ProblemSeverity.FATAL, "Could not inspect host key: permission denied"), 1),
        # Final review finding M4: an unsafe authorized-keys mode must exit
        # nonzero exactly like an unsafe host-key mode.
        (Problem(ProblemSeverity.FATAL, "Authorized keys file mode is 0644; expected 0600."), 1),
        (
            Problem(ProblemSeverity.FATAL, "Could not inspect authorized keys file: denied"),
            1,
        ),
    ],
)
def test_remote_ssh_status_exit_reflects_only_the_problem_severity(
    problem: Problem, expected_exit: int, mocker: MockerFixture
) -> None:
    mocker.patch("jailbee.remote_ssh.service.status", return_value=_status(problems=(problem,)))

    result = CliRunner().invoke(app, ["remote", "ssh", "status"])

    assert result.exit_code == expected_exit
    assert f"problem: {problem.message}" in result.stdout


def test_remote_ssh_key_add_reads_and_delegates(tmp_path: Path, mocker: MockerFixture) -> None:
    source = tmp_path / "laptop.pub"
    source.write_text("ssh-ed25519 AAAA laptop\n")
    added = AuthorizedKey("ssh-ed25519", source.read_text().strip(), "laptop", "SHA256:abc")
    add = mocker.patch("jailbee.remote_ssh.keys.add_authorized_key", return_value=added)

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "add", str(source)])

    assert result.exit_code == 0, result.stdout
    assert result.stdout == "SHA256:abc  ssh-ed25519  laptop\n"
    add.assert_called_once_with("ssh-ed25519 AAAA laptop\n")


def test_remote_ssh_key_add_rejects_a_missing_source(mocker: MockerFixture) -> None:
    add = mocker.patch("jailbee.remote_ssh.keys.add_authorized_key")

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "add", "/gone/key.pub"])

    assert result.exit_code == 1
    assert "/gone/key.pub" in result.stderr
    assert "Traceback" not in result.stderr
    assert not add.called


def test_remote_ssh_key_add_rejects_a_directory_source(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    add = mocker.patch("jailbee.remote_ssh.keys.add_authorized_key")

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "add", str(tmp_path)])

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert not add.called


def test_remote_ssh_key_add_rejects_an_undecodable_source(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    source = tmp_path / "binary.pub"
    source.write_bytes(b"\xff\xfe\x00not-utf8")
    add = mocker.patch("jailbee.remote_ssh.keys.add_authorized_key")

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "add", str(source)])

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert result.stderr.strip() != ""
    assert not add.called


def test_remote_ssh_key_add_dash_reads_stdin(mocker: MockerFixture) -> None:
    added = AuthorizedKey("ssh-ed25519", "ssh-ed25519 AAAA laptop", "laptop", "SHA256:abc")
    add = mocker.patch("jailbee.remote_ssh.keys.add_authorized_key", return_value=added)

    result = CliRunner().invoke(
        app, ["remote", "ssh", "key", "add", "-"], input="ssh-ed25519 AAAA laptop\n"
    )

    assert result.exit_code == 0, result.stdout
    assert result.stdout == "SHA256:abc  ssh-ed25519  laptop\n"
    add.assert_called_once_with("ssh-ed25519 AAAA laptop\n")


def test_remote_ssh_key_add_no_argument_reads_piped_stdin(mocker: MockerFixture) -> None:
    added = AuthorizedKey("ssh-ed25519", "ssh-ed25519 AAAA laptop", "laptop", "SHA256:abc")
    add = mocker.patch("jailbee.remote_ssh.keys.add_authorized_key", return_value=added)
    mocker.patch("jailbee.cli._is_tty", return_value=False)

    result = CliRunner().invoke(
        app, ["remote", "ssh", "key", "add"], input="ssh-ed25519 AAAA laptop\n"
    )

    assert result.exit_code == 0, result.stdout
    assert result.stdout == "SHA256:abc  ssh-ed25519  laptop\n"
    add.assert_called_once_with("ssh-ed25519 AAAA laptop\n")


def test_remote_ssh_key_add_no_argument_prompts_on_a_tty(mocker: MockerFixture) -> None:
    added = AuthorizedKey("ssh-ed25519", "ssh-ed25519 AAAA laptop", "laptop", "SHA256:abc")
    add = mocker.patch("jailbee.remote_ssh.keys.add_authorized_key", return_value=added)
    mocker.patch("jailbee.cli._is_tty", return_value=True)

    result = CliRunner().invoke(
        app, ["remote", "ssh", "key", "add"], input="ssh-ed25519 AAAA laptop\n"
    )

    assert result.exit_code == 0, result.stdout
    assert result.stdout == "SHA256:abc  ssh-ed25519  laptop\n"
    assert "Paste the public key" in result.stderr
    add.assert_called_once_with("ssh-ed25519 AAAA laptop\n")


def test_remote_ssh_key_add_no_argument_empty_paste_is_an_error(mocker: MockerFixture) -> None:
    add = mocker.patch("jailbee.remote_ssh.keys.add_authorized_key")
    mocker.patch("jailbee.cli._is_tty", return_value=True)

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "add"], input="\n")

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert not add.called


def test_remote_ssh_key_add_no_argument_empty_pipe_is_an_error(mocker: MockerFixture) -> None:
    add = mocker.patch("jailbee.remote_ssh.keys.add_authorized_key")
    mocker.patch("jailbee.cli._is_tty", return_value=False)

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "add"], input="")

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert not add.called


def test_remote_ssh_key_add_stdin_invalid_key_reports_the_parser_error(
    mocker: MockerFixture,
) -> None:
    mocker.patch(
        "jailbee.remote_ssh.keys.add_authorized_key",
        side_effect=SSHKeyError("expected a plain OpenSSH public key"),
    )

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "add", "-"], input="not a key\n")

    assert result.exit_code == 1
    assert "expected a plain OpenSSH public key" in result.stderr
    assert "Traceback" not in result.stderr


def test_remote_ssh_key_list_emits_one_stable_line_per_key(mocker: MockerFixture) -> None:
    read = mocker.patch(
        "jailbee.remote_ssh.keys.read_authorized_keys",
        return_value=[
            AuthorizedKey("ssh-ed25519", "key one", "laptop", "SHA256:first"),
            AuthorizedKey("ssh-rsa", "key two", "", "SHA256:second"),
        ],
    )

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "ls"])

    assert result.exit_code == 0, result.stdout
    assert result.stdout == ("SHA256:first  ssh-ed25519  laptop\nSHA256:second  ssh-rsa  \n")
    read.assert_called_once_with()


def test_remote_ssh_key_remove_delegates(mocker: MockerFixture) -> None:
    remove = mocker.patch("jailbee.remote_ssh.keys.remove_authorized_key")
    fingerprint = "SHA256:" + "A" * 43

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "rm", fingerprint])

    assert result.exit_code == 0, result.stdout
    remove.assert_called_once_with(fingerprint)


def test_remote_ssh_key_remove_rejects_a_malformed_fingerprint(mocker: MockerFixture) -> None:
    remove = mocker.patch("jailbee.remote_ssh.keys.remove_authorized_key")

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "rm", "SHA256:short"])

    assert result.exit_code == 2
    assert "full SHA256 fingerprint" in result.stderr
    assert not remove.called


def test_remote_ssh_key_domain_error_is_concise(tmp_path: Path, mocker: MockerFixture) -> None:
    source = tmp_path / "bad.pub"
    source.write_text("not a key\n")
    mocker.patch(
        "jailbee.remote_ssh.keys.add_authorized_key",
        side_effect=SSHKeyError("expected a plain OpenSSH public key"),
    )

    result = CliRunner().invoke(app, ["remote", "ssh", "key", "add", str(source)])

    assert result.exit_code == 1
    assert "expected a plain OpenSSH public key" in result.stderr
    assert "Traceback" not in result.stderr


def test_remote_ssh_serve_loads_config_verifies_keys_and_starts_server(
    mocker: MockerFixture,
) -> None:
    from jailbee.remote_ssh.overrides import ServeOverrides

    global_config = mocker.Mock()
    load = mocker.patch("jailbee.cli._load_global", return_value=global_config)
    ensure = mocker.patch("jailbee.remote_ssh.keys.ensure_key_files")
    serve = mocker.patch("jailbee.remote_ssh.server.serve")

    result = CliRunner().invoke(app, ["remote", "ssh", "serve"])

    assert result.exit_code == 0, result.stdout
    load.assert_called_once_with()
    ensure.assert_called_once_with()
    serve.assert_called_once_with(global_config.remote.ssh, ServeOverrides())


def test_remote_ssh_serve_flags_are_parsed_and_passed_through(
    mocker: MockerFixture,
) -> None:
    from jailbee.config.models_remote import RemoteConfig, RemoteSSHConfig
    from jailbee.global_config import GlobalConfig
    from jailbee.remote_ssh.overrides import ServeOverrides

    global_config = GlobalConfig(remote=RemoteConfig(ssh=RemoteSSHConfig()))
    mocker.patch("jailbee.cli._load_global", return_value=global_config)
    mocker.patch("jailbee.remote_ssh.keys.ensure_key_files")
    serve = mocker.patch("jailbee.remote_ssh.server.serve")

    result = CliRunner().invoke(
        app,
        [
            "remote",
            "ssh",
            "serve",
            "--listen",
            "0.0.0.0",
            "--port",
            "18022",
            "--shell",
            "--commands",
            "allowlist",
            "--allow",
            "ls",
            "--allow",
            "new",
        ],
    )

    assert result.exit_code == 0, result.stdout
    expected_overrides = ServeOverrides(
        listen="0.0.0.0",
        port=18022,
        shell=True,
        commands_mode="allowlist",
        allow=["ls", "new"],
    )
    serve.assert_called_once_with(mocker.ANY, expected_overrides)
    effective = serve.call_args.args[0]
    assert isinstance(effective, RemoteSSHConfig)
    assert effective.listen == "0.0.0.0"
    assert effective.port == 18022
    assert effective.shell is True
    assert effective.commands.mode == "allowlist"
    assert effective.commands.allow == ["ls", "new"]
    # `dashboard` was not overridden and the base config left it enabled, so
    # `--shell` alone does not fail the "at least one entry point" rule and
    # a plain `--allow` replaces, rather than appends to, the empty base list.
    assert effective.dashboard is True


def test_remote_ssh_serve_allow_replaces_rather_than_appends(
    mocker: MockerFixture,
) -> None:
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteConfig, RemoteSSHConfig
    from jailbee.global_config import GlobalConfig

    base = RemoteSSHConfig(
        exec=True, commands=RemoteCommandPolicy(mode="allowlist", allow=["git pull"])
    )
    global_config = GlobalConfig(remote=RemoteConfig(ssh=base))
    mocker.patch("jailbee.cli._load_global", return_value=global_config)
    mocker.patch("jailbee.remote_ssh.keys.ensure_key_files")
    serve = mocker.patch("jailbee.remote_ssh.server.serve")

    result = CliRunner().invoke(app, ["remote", "ssh", "serve", "--allow", "ls"])

    assert result.exit_code == 0, result.stdout
    effective = serve.call_args.args[0]
    assert effective.commands.allow == ["ls"]


def test_remote_ssh_serve_empty_allowlist_override_is_a_clean_error(
    mocker: MockerFixture,
) -> None:
    from jailbee.config.models_remote import RemoteConfig, RemoteSSHConfig
    from jailbee.global_config import GlobalConfig

    global_config = GlobalConfig(remote=RemoteConfig(ssh=RemoteSSHConfig()))
    mocker.patch("jailbee.cli._load_global", return_value=global_config)
    ensure = mocker.patch("jailbee.remote_ssh.keys.ensure_key_files")
    serve = mocker.patch("jailbee.remote_ssh.server.serve")

    result = CliRunner().invoke(app, ["remote", "ssh", "serve", "--commands", "allowlist"])

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert result.stderr.strip() != ""
    assert not ensure.called
    assert not serve.called


def test_remote_ssh_serve_unknown_allow_leaf_is_a_clean_error(
    mocker: MockerFixture,
) -> None:
    from jailbee.config.models_remote import RemoteConfig, RemoteSSHConfig
    from jailbee.global_config import GlobalConfig

    global_config = GlobalConfig(remote=RemoteConfig(ssh=RemoteSSHConfig()))
    mocker.patch("jailbee.cli._load_global", return_value=global_config)
    ensure = mocker.patch("jailbee.remote_ssh.keys.ensure_key_files")
    serve = mocker.patch("jailbee.remote_ssh.server.serve")

    result = CliRunner().invoke(
        app,
        ["remote", "ssh", "serve", "--exec", "--commands", "allowlist", "--allow", "git explode"],
    )

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "unknown remote Jailbee command path(s): git explode" in result.stderr
    assert not ensure.called
    assert not serve.called


def test_remote_ssh_serve_invalid_commands_choice_is_a_clean_cli_error() -> None:
    result = CliRunner().invoke(app, ["remote", "ssh", "serve", "--commands", "bogus"])

    assert result.exit_code == 2
    assert "Traceback" not in result.stderr


def test_remote_ssh_serve_reports_global_config_error_concisely(
    mocker: MockerFixture,
) -> None:
    mocker.patch(
        "jailbee.cli.load_global_config",
        side_effect=ConfigError("global config is malformed"),
    )
    ensure = mocker.patch("jailbee.remote_ssh.keys.ensure_key_files")

    result = CliRunner().invoke(app, ["remote", "ssh", "serve"])

    assert result.exit_code == 1
    assert "global config is malformed" in result.stderr
    assert "Traceback" not in result.stderr
    assert not ensure.called


def test_remote_ssh_serve_translates_missing_asyncssh_dependency(
    mocker: MockerFixture,
) -> None:
    global_config = mocker.Mock()
    mocker.patch("jailbee.cli._load_global", return_value=global_config)
    mocker.patch("jailbee.remote_ssh.keys.ensure_key_files")
    real_import = builtins.__import__

    def import_without_asyncssh(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ):
        if name == "jailbee.remote_ssh" and "server" in fromlist:
            raise ModuleNotFoundError("No module named 'asyncssh'", name="asyncssh")
        return real_import(name, globals, locals, fromlist, level)

    mocker.patch("builtins.__import__", side_effect=import_without_asyncssh)

    result = CliRunner().invoke(app, ["remote", "ssh", "serve"])

    assert result.exit_code == 1
    error_output = flat_output(result.stderr)
    assert "requires the optional 'ssh' extra" in error_output
    assert "uv tool install 'jailbee[ssh]'" in error_output
    assert "Traceback" not in error_output


def test_remote_ssh_serve_ctrl_c_exit_propagates_cleanly_through_the_cli(
    mocker: MockerFixture,
) -> None:
    """`server.serve` converts Ctrl-C to SystemExit(130); the thin CLI must not mask it."""
    global_config = mocker.Mock()
    mocker.patch("jailbee.cli._load_global", return_value=global_config)
    mocker.patch("jailbee.remote_ssh.keys.ensure_key_files")
    mocker.patch("jailbee.remote_ssh.server.serve", side_effect=SystemExit(130))

    result = CliRunner().invoke(app, ["remote", "ssh", "serve"])

    assert result.exit_code == 130
    assert "Traceback" not in flat_output(result.stderr)


def test_remote_console_requires_policy_and_stays_hidden(mocker: MockerFixture) -> None:
    run = mocker.patch("jailbee.remote_ssh.console.run", return_value=7)

    result = CliRunner().invoke(app, ["_remote-console", "--repo", "project"])

    assert result.exit_code != 0
    run.assert_not_called()
    assert "_remote-console" not in CliRunner().invoke(app, ["--help"]).stdout


def test_remote_console_forwards_the_policy_json_flag(mocker: MockerFixture) -> None:
    run = mocker.patch("jailbee.remote_ssh.console.run", return_value=0)

    result = CliRunner().invoke(
        app, ["_remote-console", "--repo", "project", "--policy-json", '{"shell": true}']
    )

    assert result.exit_code == 0
    run.assert_called_once_with("project", '{"shell": true}')


def test_remote_console_policy_json_flag_stays_hidden() -> None:
    """The internal flag must never show up even in the hidden command's own --help."""
    result = CliRunner().invoke(app, ["_remote-console", "--help"])

    assert "--policy-json" not in flat_output(result.stdout)


def test_setup_help_still_names_only_the_original_steps() -> None:
    result = CliRunner().invoke(app, ["setup", "--help"])

    assert result.exit_code == 0, result.stdout
    output = flat_output(result.stdout).lower()
    assert all(step in output for step in ("completions", "timer", "skills"))
    assert "remote" not in output
    assert "ssh" not in output


def test_ordinary_help_imports_work_without_asyncssh() -> None:
    script = """
import importlib.abc
import sys

class BlockAsyncSSH(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "asyncssh" or fullname.startswith("asyncssh."):
            raise ModuleNotFoundError("No module named 'asyncssh'", name="asyncssh")
        return None

sys.meta_path.insert(0, BlockAsyncSSH())

from typer.testing import CliRunner
from jailbee.cli import app

runner = CliRunner()
for argv in (["--help"], ["setup", "--help"], ["remote", "ssh", "--help"]):
    result = runner.invoke(app, argv)
    if result.exit_code != 0:
        raise SystemExit(f"{argv!r}: {result.exit_code}: {result.output}")
print("ordinary help works without asyncssh")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ordinary help works without asyncssh\n"


def test_remote_ssh_serve_no_restrict_host_is_passed_through(mocker: MockerFixture) -> None:
    from jailbee.config.models_remote import RemoteConfig, RemoteSSHConfig
    from jailbee.global_config import GlobalConfig
    from jailbee.remote_ssh.overrides import ServeOverrides

    global_config = GlobalConfig(remote=RemoteConfig(ssh=RemoteSSHConfig()))
    mocker.patch("jailbee.cli._load_global", return_value=global_config)
    mocker.patch("jailbee.remote_ssh.keys.ensure_key_files")
    serve = mocker.patch("jailbee.remote_ssh.server.serve")

    result = CliRunner().invoke(app, ["remote", "ssh", "serve", "--no-restrict-host"])

    assert result.exit_code == 0, result.stdout
    serve.assert_called_once_with(mocker.ANY, ServeOverrides(restrict_host=False))
    assert serve.call_args.args[0].restrict_host is False
