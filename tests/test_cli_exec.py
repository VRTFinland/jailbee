"""Tests for `jailbee exec`."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


def test_exec_always_passes_the_gui_environment(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    ex = mocker.patch.object(Incus, "exec_interactive", return_value=0)
    runner.invoke(app, ["exec", "c1", "--", "true"])
    env = ex.call_args.kwargs["env"]
    assert env["HOME"] == "/home/dev"
    assert "WAYLAND_DISPLAY" in env and "DISPLAY" in env


def test_exec_returns_the_commands_exit_code(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch.object(Incus, "exec_interactive", return_value=3)
    assert runner.invoke(app, ["exec", "c1", "--", "false"]).exit_code == 3


def test_exec_detach_returns_immediately_and_names_the_log(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="c1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    interactive = mocker.patch.object(Incus, "exec_interactive")
    detached = mocker.patch("jailbee.gui.launch_detached")
    result = runner.invoke(app, ["exec", "-d", "c1", "--", "firefox"])
    assert result.exit_code == 0
    assert not interactive.called
    assert detached.called
    assert "/tmp/jailbee-exec-" in result.output
