"""Tests for the orchestration broker's systemd user service."""

from __future__ import annotations

import subprocess
from importlib.resources import files
from pathlib import Path

from pytest_mock import MockerFixture


def test_unit_template_has_killmode_process_and_broker_execstart() -> None:
    text = (files("jailbee.templates.systemd") / "jailbee-broker.service").read_text()

    assert "ExecStart={jailbee_bin} _broker" in text
    assert "KillMode=process" in text
    assert "WantedBy=default.target" in text


def test_install_writes_unit_and_reports_change(private_home: Path) -> None:
    from jailbee.orchestration import service

    assert service.install("/usr/bin/jailbee") is True
    unit = private_home / ".config" / "systemd" / "user" / service.BROKER_SERVICE
    assert "ExecStart=/usr/bin/jailbee _broker" in unit.read_text()
    assert service.install("/usr/bin/jailbee") is False


def test_ensure_running_enables_now(private_home: Path, mocker: MockerFixture) -> None:
    from jailbee.orchestration import service

    _ = private_home
    mocker.patch("shutil.which", return_value="/usr/bin/jailbee")
    run = mocker.patch("subprocess.run")

    assert service.ensure_running() is None

    cmds = [list(c.args[0]) for c in run.call_args_list]
    assert ["systemctl", "--user", "daemon-reload"] in cmds
    assert cmds[-1] == ["systemctl", "--user", "enable", "--now", "jailbee-broker.service"]


def test_ensure_running_never_raises_on_systemctl_failure(
    private_home: Path, mocker: MockerFixture
) -> None:
    from jailbee.orchestration import service

    _ = private_home
    mocker.patch("shutil.which", return_value="/usr/bin/jailbee")
    mocker.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "systemctl"))
    warn = mocker.patch("jailbee.orchestration.service.warn")

    assert service.ensure_running() is None

    assert "jb setup" in warn.call_args.args[0]


def test_ensure_running_never_raises_on_oserror(private_home: Path, mocker: MockerFixture) -> None:
    from jailbee.orchestration import service

    _ = private_home
    mocker.patch("shutil.which", return_value="/usr/bin/jailbee")
    mocker.patch("subprocess.run", side_effect=FileNotFoundError("systemctl"))
    warn = mocker.patch("jailbee.orchestration.service.warn")

    assert service.ensure_running() is None
    warn.assert_called_once()


def test_ensure_running_without_jailbee_on_path_calls_no_systemctl(
    mocker: MockerFixture,
) -> None:
    from jailbee.orchestration import service

    mocker.patch("shutil.which", return_value=None)
    run = mocker.patch("subprocess.run")
    warn = mocker.patch("jailbee.orchestration.service.warn")

    assert service.ensure_running() is None

    run.assert_not_called()
    assert "jb setup" in warn.call_args.args[0]


def test_status_reports_stdout_or_unknown(mocker: MockerFixture) -> None:
    from jailbee.orchestration import service

    run = mocker.patch("subprocess.run")
    run.return_value = mocker.Mock(stdout="active\n", returncode=0)
    assert service.status() == "active"
    assert run.call_args.args[0] == [
        "systemctl",
        "--user",
        "is-active",
        "jailbee-broker.service",
    ]
    run.return_value = mocker.Mock(stdout="", returncode=3)
    assert service.status() == "unknown"
    run.side_effect = OSError("no systemctl")
    assert service.status() == "unknown"
