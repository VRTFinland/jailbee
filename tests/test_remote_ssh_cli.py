"""CLI coverage for restricted remote SSH administration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.remote_ssh.keys import (
    AuthorizedKey,
    SSHDependencyError,
    SSHKeyError,
)
from jailbee.remote_ssh.service import ServiceStatus
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


def _status(*, problems: tuple[str, ...] = ()) -> ServiceStatus:
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


@pytest.mark.parametrize(
    ("problem", "expected_exit"),
    [
        ("The SSH service unit is not installed.", 0),
        ("The SSH service is not enabled.", 0),
        ("The SSH service is not active.", 0),
        ("No authorized client keys are configured.", 0),
        ("Global config is invalid: broken YAML", 1),
        ("Host key is missing.", 1),
        ("Host key mode is 0644; expected 0600.", 1),
        ("Could not inspect host key: permission denied", 1),
    ],
)
def test_remote_ssh_status_exit_reflects_only_unsafe_configuration(
    problem: str, expected_exit: int, mocker: MockerFixture
) -> None:
    mocker.patch("jailbee.remote_ssh.service.status", return_value=_status(problems=(problem,)))

    result = CliRunner().invoke(app, ["remote", "ssh", "status"])

    assert result.exit_code == expected_exit
    assert f"problem: {problem}" in result.stdout


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

    assert result.exit_code == 2
    assert not add.called


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
    global_config = mocker.Mock()
    load = mocker.patch("jailbee.cli._load_global", return_value=global_config)
    ensure = mocker.patch("jailbee.remote_ssh.keys.ensure_key_files")
    serve = mocker.patch("jailbee.remote_ssh.server.serve")

    result = CliRunner().invoke(app, ["remote", "ssh", "serve"])

    assert result.exit_code == 0, result.stdout
    load.assert_called_once_with()
    ensure.assert_called_once_with()
    serve.assert_called_once_with(global_config.remote.ssh)


def test_remote_console_is_hidden_but_delegates(mocker: MockerFixture) -> None:
    run = mocker.patch("jailbee.remote_ssh.console.run", return_value=7)

    result = CliRunner().invoke(app, ["_remote-console", "--repo", "project"])

    assert result.exit_code == 7
    run.assert_called_once_with("project")
    assert "_remote-console" not in CliRunner().invoke(app, ["--help"]).stdout


def test_setup_help_still_names_only_the_original_steps() -> None:
    result = CliRunner().invoke(app, ["setup", "--help"])

    assert result.exit_code == 0, result.stdout
    output = flat_output(result.stdout).lower()
    assert all(step in output for step in ("completions", "timer", "skills"))
    assert "remote" not in output
    assert "ssh" not in output
