from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config.models_remote import RemoteConfig, RemoteSSHConfig
from jailbee.global_config import GlobalConfig
from jailbee.incus import IncusError
from jailbee.remote_display import DisplayError, DisplayStatus
from tests.conftest import make_cfg


@pytest.fixture
def display(mocker):
    mocker.patch("jailbee.cli._load_or_exit")
    mocker.patch("jailbee.cli._load_global", return_value=GlobalConfig())
    mocker.patch("jailbee.incus.Incus")
    return mocker


def test_up_starts_the_display_and_prints_the_recipe(display):
    up = display.patch("jailbee.remote_display.display_up")

    result = CliRunner().invoke(app, ["display", "up"])

    assert result.exit_code == 0, result.output
    assert up.call_args.kwargs["recreate"] is False
    assert callable(up.call_args.kwargs["on_step"])
    assert "ssh -N -L" in result.output


def test_up_warns_when_remote_ssh_gui_is_off(display):
    display.patch("jailbee.remote_display.display_up")

    result = CliRunner().invoke(app, ["display", "up"])

    assert result.exit_code == 0, result.output
    assert "remote.ssh.gui is off" in result.output


def test_up_does_not_warn_when_remote_ssh_gui_is_on(display):
    display.patch("jailbee.remote_display.display_up")
    gcfg = GlobalConfig(remote=RemoteConfig(ssh=RemoteSSHConfig(gui=True)))
    display.patch("jailbee.cli._load_global", return_value=gcfg)

    result = CliRunner().invoke(app, ["display", "up"])

    assert result.exit_code == 0, result.output
    assert "remote.ssh.gui is off" not in result.output


def test_up_recreate_is_passed_through(display):
    up = display.patch("jailbee.remote_display.display_up")

    result = CliRunner().invoke(app, ["display", "up", "--recreate"])

    assert result.exit_code == 0, result.output
    assert up.call_args.kwargs["recreate"] is True


def test_up_reports_a_display_error_and_exits_1(display):
    display.patch("jailbee.remote_display.display_up", side_effect=DisplayError("weston is dead"))

    result = CliRunner().invoke(app, ["display", "up"])

    assert result.exit_code == 1
    assert "weston is dead" in result.output


def test_down_stops_the_display(display):
    down = display.patch("jailbee.remote_display.display_down")

    result = CliRunner().invoke(app, ["display", "down"])

    assert result.exit_code == 0, result.output
    down.assert_called_once()


def test_status_running_also_prints_the_recipe(display):
    display.patch("jailbee.remote_display.display_status", return_value=DisplayStatus.RUNNING)

    result = CliRunner().invoke(app, ["display", "status"])

    assert result.exit_code == 0, result.output
    assert "running" in result.output
    assert "ssh -N -L" in result.output


def test_status_stopped_prints_no_recipe(display):
    display.patch("jailbee.remote_display.display_status", return_value=DisplayStatus.STOPPED)

    result = CliRunner().invoke(app, ["display", "status"])

    assert result.exit_code == 0, result.output
    assert "stopped" in result.output
    assert "ssh -N -L" not in result.output


def test_down_reports_an_incus_error_and_exits_1(display):
    display.patch("jailbee.remote_display.display_down", side_effect=IncusError("stop failed"))

    result = CliRunner().invoke(app, ["display", "down"])

    assert result.exit_code == 1
    assert "stop failed" in result.output
    assert "Traceback" not in result.output


def test_status_reports_an_incus_error_and_exits_1(display):
    display.patch("jailbee.remote_display.display_status", side_effect=IncusError("incus gone"))

    result = CliRunner().invoke(app, ["display", "status"])

    assert result.exit_code == 1
    assert "incus gone" in result.output
    assert "Traceback" not in result.output


runner = CliRunner()


@pytest.fixture
def host(tmp_path, mocker):
    """A host-side invocation against one running container `c1`."""
    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "c1", "status": "Running"}]
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "c1"))
    return cfg, incus


@pytest.mark.parametrize(
    ("result", "text"),
    [
        ("attached", "Attached the host display to c1"),
        ("unchanged", "already attached"),
        ("reattached", "compositor restarted"),
    ],
)
def test_attach_reports_what_it_did(host, mocker, result, text) -> None:
    from jailbee.runtime_mounts import EnsureResult

    cfg, incus = host
    ensure = mocker.patch(
        "jailbee.runtime_mounts.ensure_host_display", return_value=EnsureResult(result)
    )

    out = runner.invoke(app, ["display", "attach", "c1"])

    assert out.exit_code == 0, out.output
    assert text in out.output
    ensure.assert_called_once_with(cfg, incus, "c1")


def test_attach_refuses_a_stopped_container(host, mocker) -> None:
    _, incus = host
    incus.list_containers.return_value = [{"name": "c1", "status": "Stopped"}]
    ensure = mocker.patch("jailbee.runtime_mounts.ensure_host_display")

    out = runner.invoke(app, ["display", "attach", "c1"])

    assert out.exit_code == 1
    assert "not running" in out.output
    ensure.assert_not_called()


def test_attach_reports_a_missing_host_socket(host, mocker) -> None:
    mocker.patch(
        "jailbee.runtime_mounts.ensure_host_display",
        side_effect=DisplayError("cannot attach the host display to c1: not a Wayland session"),
    )

    out = runner.invoke(app, ["display", "attach", "c1"])

    assert out.exit_code == 1
    assert "not a Wayland session" in out.output


def test_attach_reports_an_incus_failure(host, mocker) -> None:
    mocker.patch(
        "jailbee.runtime_mounts.ensure_host_display",
        side_effect=IncusError("device add failed"),
    )

    out = runner.invoke(app, ["display", "attach", "c1"])

    assert out.exit_code == 1
    assert "device add failed" in out.output


def test_attach_is_refused_from_an_ssh_session(host, mocker, monkeypatch) -> None:
    monkeypatch.setenv("JAILBEE_SSH_SESSION", "1")
    monkeypatch.setenv("JAILBEE_SSH_GUI", "8022")
    monkeypatch.setenv("JAILBEE_SSH_EXCLUDED_REPOS", "[]")
    ensure = mocker.patch("jailbee.runtime_mounts.ensure_host_display")

    out = runner.invoke(app, ["display", "attach", "c1"])

    assert out.exit_code == 1
    assert "host" in out.output
    ensure.assert_not_called()
