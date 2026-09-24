"""SSH systemd user-service lifecycle without a real user manager."""

from __future__ import annotations

import builtins
import subprocess
import sys
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBsz47IcK4hPdHS7xOXNGafb/Uw3epmEsD7xIJn434n6"


@pytest.fixture
def ssh_home(private_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(private_home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(private_home / ".local" / "share"))
    return private_home


def _write_keys(*, authorized: str = PUBLIC_KEY, mode: int = 0o600) -> None:
    from jailbee.remote_ssh.keys import ssh_paths

    paths = ssh_paths()
    paths.config_dir.mkdir(parents=True)
    paths.data_dir.mkdir(parents=True)
    paths.authorized_keys.write_text(authorized)
    paths.host_key.write_text("host")
    paths.authorized_keys.chmod(mode)
    paths.host_key.chmod(mode)


def test_enable_writes_and_starts_user_service(
    ssh_home: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.remote_ssh.service import SSH_SERVICE, enable

    mocker.patch("jailbee.remote_ssh.service._ssh_dependency_available", return_value=True)
    mocker.patch("shutil.which", return_value="/usr/local/bin/jailbee")
    ensure = mocker.patch("jailbee.remote_ssh.service.ensure_key_files")
    run = mocker.patch("subprocess.run")

    enable()

    unit = ssh_home / ".config" / "systemd" / "user" / SSH_SERVICE
    assert "ExecStart=/usr/local/bin/jailbee remote ssh serve" in unit.read_text()
    assert "UMask=0077" in unit.read_text()
    # `--background` workers launched over SSH sit in the same cgroup as this
    # unit but under their own detached process group; only `KillMode=process`
    # keeps `jb remote ssh restart`/`disable` (and an ordinary `systemctl
    # --user stop`) from killing them too (final review finding I2).
    assert "KillMode=process" in unit.read_text()
    ensure.assert_called_once_with()
    assert [call.args[0] for call in run.call_args_list] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", SSH_SERVICE],
    ]
    assert all(call.kwargs == {"check": True} for call in run.call_args_list)


def test_enable_rejects_missing_optional_dependency(
    ssh_home: Path,
    mocker: MockerFixture,
) -> None:
    _ = ssh_home
    from jailbee.remote_ssh.keys import SSHDependencyError
    from jailbee.remote_ssh.service import enable

    mocker.patch("jailbee.remote_ssh.service._ssh_dependency_available", return_value=False)
    which = mocker.patch("shutil.which", return_value="/usr/local/bin/jailbee")
    mocker.patch("jailbee.remote_ssh.service.ensure_key_files")
    run = mocker.patch("subprocess.run")

    with pytest.raises(SSHDependencyError, match=r"jailbee\[ssh\]"):
        enable()

    which.assert_not_called()
    run.assert_not_called()


def test_enable_rejects_missing_jailbee_executable(
    ssh_home: Path,
    mocker: MockerFixture,
) -> None:
    _ = ssh_home
    from jailbee.remote_ssh.service import enable

    mocker.patch("jailbee.remote_ssh.service._ssh_dependency_available", return_value=True)
    mocker.patch("shutil.which", return_value=None)
    ensure = mocker.patch("jailbee.remote_ssh.service.ensure_key_files")
    run = mocker.patch("subprocess.run")

    with pytest.raises(RuntimeError, match="not on PATH"):
        enable()

    ensure.assert_not_called()
    run.assert_not_called()


def test_enable_skips_reload_when_unit_is_unchanged(
    ssh_home: Path,
    mocker: MockerFixture,
) -> None:
    _ = ssh_home
    from jailbee.remote_ssh.service import SSH_SERVICE, enable

    mocker.patch("jailbee.remote_ssh.service._ssh_dependency_available", return_value=True)
    mocker.patch("shutil.which", return_value="/usr/local/bin/jailbee")
    mocker.patch("jailbee.remote_ssh.service.ensure_key_files")
    run = mocker.patch("subprocess.run")

    enable()
    run.reset_mock()
    enable()

    run.assert_called_once_with(
        ["systemctl", "--user", "enable", "--now", SSH_SERVICE],
        check=True,
    )


def test_disable_stops_service_without_deleting_keys(
    ssh_home: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.remote_ssh.keys import ssh_paths
    from jailbee.remote_ssh.service import SSH_SERVICE, disable

    paths = ssh_paths()
    paths.config_dir.mkdir(parents=True)
    paths.data_dir.mkdir(parents=True)
    paths.authorized_keys.write_text("client")
    paths.host_key.write_text("host")
    run = mocker.patch("subprocess.run")

    disable()

    run.assert_called_once_with(
        ["systemctl", "--user", "disable", "--now", SSH_SERVICE],
        check=True,
    )
    assert paths.authorized_keys.read_text() == "client"
    assert paths.host_key.read_text() == "host"
    _ = ssh_home


def test_restart_requires_installed_unit(
    ssh_home: Path,
    mocker: MockerFixture,
) -> None:
    _ = ssh_home
    from jailbee.remote_ssh.service import restart

    run = mocker.patch("subprocess.run")

    with pytest.raises(RuntimeError, match="not installed"):
        restart()

    run.assert_not_called()


def test_restart_restarts_installed_user_service(
    ssh_home: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.remote_ssh.service import SSH_SERVICE, restart

    unit = ssh_home / ".config" / "systemd" / "user" / SSH_SERVICE
    unit.parent.mkdir(parents=True)
    unit.write_text("unit")
    run = mocker.patch("subprocess.run")

    restart()

    run.assert_called_once_with(
        ["systemctl", "--user", "restart", SSH_SERVICE],
        check=True,
    )


def test_status_reports_active_service_configuration_and_key_count(
    ssh_home: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.global_config import default_global_config_path
    from jailbee.remote_ssh.service import SSH_SERVICE, status

    unit = ssh_home / ".config" / "systemd" / "user" / SSH_SERVICE
    unit.parent.mkdir(parents=True)
    unit.write_text("unit")
    config = default_global_config_path()
    config.parent.mkdir(parents=True)
    config.write_text(
        "remote:\n"
        "  ssh:\n"
        "    listen: '::1'\n"
        "    port: 2200\n"
        "    dashboard: true\n"
        "    shell: true\n"
        "    exec: false\n"
        "    commands:\n"
        "      mode: full\n"
    )
    _write_keys()
    mocker.patch("jailbee.remote_ssh.service._ssh_dependency_available", return_value=True)
    run = mocker.patch(
        "subprocess.run",
        side_effect=lambda command, **kwargs: subprocess.CompletedProcess(command, 0),
    )

    result = status()

    assert result.unit_path == unit
    assert result.installed is True
    assert result.enabled is True
    assert result.active is True
    assert result.listen == "::1"
    assert result.port == 2200
    assert result.entrypoints == ("dashboard", "shell")
    assert result.authorized_keys == 1
    assert result.problems == ()
    assert [call.args[0] for call in run.call_args_list] == [
        ["systemctl", "--user", "is-enabled", SSH_SERVICE],
        ["systemctl", "--user", "is-active", SSH_SERVICE],
        # No server record yet, so the staleness check asks for the pid.
        ["systemctl", "--user", "show", "--property=MainPID", "--value", SSH_SERVICE],
    ]
    assert all(
        call.kwargs == {"check": False, "capture_output": True, "text": True}
        for call in run.call_args_list
    )


def test_status_reports_missing_unit_dependency_keys_and_invalid_config(
    ssh_home: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.global_config import default_global_config_path
    from jailbee.remote_ssh.service import ProblemSeverity, status

    config = default_global_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("remote:\n  ssh:\n    listen: localhost\n")
    mocker.patch("jailbee.remote_ssh.service._ssh_dependency_available", return_value=False)
    mocker.patch(
        "subprocess.run",
        side_effect=lambda command, **kwargs: subprocess.CompletedProcess(command, 1),
    )

    result = status()

    assert result.installed is False
    assert result.enabled is False
    assert result.active is False
    assert result.listen == "127.0.0.1"
    assert result.port == 8022
    assert result.entrypoints == ("dashboard",)
    assert result.authorized_keys == 0
    by_message = {problem.message.lower(): problem.severity for problem in result.problems}
    messages = "\n".join(by_message).lower()
    for expected in (
        "optional ssh dependency",
        "unit is not installed",
        "not enabled",
        "not active",
        "authorized keys file is missing",
        "host key is missing",
        "no authorized client keys",
        "global config",
    ):
        assert expected in messages

    # A missing authorized-keys file reads as zero keys, same as an
    # intentionally empty one (a valid configuration per docs/security.md),
    # so it warns rather than failing status. A missing host key has no such
    # safe reading and an invalid global config leaves the service
    # unusable, so both are fatal.
    warning, fatal = ProblemSeverity.WARNING, ProblemSeverity.FATAL
    for substring, expected_severity in (
        ("optional ssh dependency", warning),
        ("unit is not installed", warning),
        ("not enabled", warning),
        ("not active", warning),
        ("authorized keys file is missing", warning),
        ("no authorized client keys", warning),
        ("host key is missing", fatal),
        ("global config", fatal),
    ):
        (severity,) = (v for k, v in by_message.items() if substring in k)
        assert severity is expected_severity, substring
    assert not (ssh_home / ".local" / "share" / "jailbee" / "ssh").exists()


def test_status_reports_inactive_service_and_unsafe_key_modes(
    ssh_home: Path,
    mocker: MockerFixture,
) -> None:
    from jailbee.remote_ssh.keys import ssh_paths
    from jailbee.remote_ssh.service import SSH_SERVICE, ProblemSeverity, status

    unit = ssh_home / ".config" / "systemd" / "user" / SSH_SERVICE
    unit.parent.mkdir(parents=True)
    unit.write_text("unit")
    _write_keys(authorized="", mode=0o644)
    paths = ssh_paths()
    paths.host_key.chmod(0o666)
    mocker.patch("jailbee.remote_ssh.service._ssh_dependency_available", return_value=True)

    def probe(command, **kwargs):
        return subprocess.CompletedProcess(command, 0 if "is-enabled" in command else 3)

    mocker.patch("subprocess.run", side_effect=probe)

    result = status()

    assert result.installed is True
    assert result.enabled is True
    assert result.active is False
    assert result.authorized_keys == 0
    by_message = {problem.message.lower(): problem.severity for problem in result.problems}
    messages = "\n".join(by_message)
    assert "not active" in messages
    assert "authorized keys file mode is 0644; expected 0600" in messages
    assert "host key mode is 0666; expected 0600" in messages
    assert "no authorized client keys" in messages

    # Regression for final-review finding M4: an unsafe authorized-keys mode
    # must be exactly as fatal as an unsafe host-key mode.
    assert by_message["authorized keys file mode is 0644; expected 0600."] is ProblemSeverity.FATAL
    assert by_message["host key mode is 0666; expected 0600."] is ProblemSeverity.FATAL


def test_status_never_imports_asyncssh_or_starts_the_service(
    ssh_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker: MockerFixture,
) -> None:
    _ = ssh_home
    from jailbee.remote_ssh.service import SSH_SERVICE, status

    original_import = builtins.__import__

    def reject_asyncssh(name, *args, **kwargs):
        if name == "asyncssh" or name.startswith("asyncssh."):
            raise AssertionError("status imported AsyncSSH")
        return original_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "asyncssh", raising=False)
    monkeypatch.setattr(builtins, "__import__", reject_asyncssh)
    run = mocker.patch(
        "subprocess.run",
        side_effect=lambda command, **kwargs: subprocess.CompletedProcess(command, 1),
    )

    status()

    assert [call.args[0] for call in run.call_args_list] == [
        ["systemctl", "--user", "is-enabled", SSH_SERVICE],
        ["systemctl", "--user", "is-active", SSH_SERVICE],
    ]


def _install_unit(ssh_home: Path) -> None:
    from jailbee.remote_ssh.service import SSH_SERVICE

    unit = ssh_home / ".config" / "systemd" / "user" / SSH_SERVICE
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("unit")


def _systemd_main_pid(mocker: MockerFixture, pid: int) -> object:
    return mocker.patch(
        "subprocess.run",
        side_effect=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=f"{pid}\n"
        ),
    )


def test_no_unit_means_no_stale_service_and_no_subprocess(ssh_home, mocker) -> None:
    from jailbee.remote_ssh.service import stale_service_reason

    run = mocker.patch("subprocess.run")

    assert stale_service_reason("2.0.0") is None
    run.assert_not_called()


def test_a_current_record_costs_no_subprocess(ssh_home, mocker) -> None:
    import os

    from jailbee.remote_ssh.running import record_running
    from jailbee.remote_ssh.service import stale_service_reason

    _install_unit(ssh_home)
    record_running("2.0.0", pid=os.getpid())
    run = mocker.patch("subprocess.run")

    assert stale_service_reason("2.0.0") is None
    run.assert_not_called()


def test_a_service_with_no_record_predates_the_self_restart(ssh_home, mocker) -> None:
    import os

    from jailbee.remote_ssh.service import stale_service_reason

    _install_unit(ssh_home)
    _systemd_main_pid(mocker, os.getpid())

    reason = stale_service_reason("2.0.0")

    assert reason is not None
    assert "older than the one that restarts itself" in reason
    assert "jb remote ssh restart" in reason


def test_a_service_recorded_at_an_old_version_is_stale(ssh_home, mocker) -> None:
    import os

    from jailbee.remote_ssh.running import record_running
    from jailbee.remote_ssh.service import stale_service_reason

    _install_unit(ssh_home)
    record_running("1.9.0", pid=os.getpid())
    _systemd_main_pid(mocker, os.getpid())

    reason = stale_service_reason("2.0.0")

    assert reason is not None
    assert "JailBee 1.9.0" in reason


def test_an_inactive_service_is_never_stale(ssh_home, mocker) -> None:
    from jailbee.remote_ssh.service import stale_service_reason

    _install_unit(ssh_home)
    _systemd_main_pid(mocker, 0)

    assert stale_service_reason("2.0.0") is None
