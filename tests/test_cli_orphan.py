"""CLI tests for `jailbee orphan …`: thin delegation to `jailbee.orphans`."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.lifecycle import ContainerInfo


def _ci(name: str, state: str = "Running", repo: str = "gamma") -> ContainerInfo:
    return ContainerInfo(name, state, "strict", None, None, repo=repo)


@pytest.fixture
def ctx(mocker):
    """Patch the base config, Incus and registered roots; return the Incus mock."""
    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._orphan_context", return_value=(mocker.MagicMock(), incus, []))
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    return incus


def test_ls_lists_orphans(ctx, mocker):
    mocker.patch("jailbee.orphans.list_orphans", return_value=[_ci("gamma-x", "Stopped")])
    result = CliRunner().invoke(app, ["orphan", "ls"])
    assert result.exit_code == 0, result.output
    assert "gamma-x" in result.output and "Stopped" in result.output


def test_ls_empty(ctx, mocker):
    mocker.patch("jailbee.orphans.list_orphans", return_value=[])
    result = CliRunner().invoke(app, ["orphan", "ls"])
    assert result.exit_code == 0
    assert "No orphan containers" in result.output


def test_tmux_attaches(ctx, mocker):
    mocker.patch("jailbee.orphans.resolve_orphan", return_value=_ci("gamma-x"))
    attach = mocker.patch("jailbee.orphans.attach_tmux", return_value=0)
    result = CliRunner().invoke(app, ["orphan", "tmux", "gamma-x"])
    assert result.exit_code == 0, result.output
    attach.assert_called_once_with(ctx, "gamma-x")


def test_tmux_refuses_a_stopped_orphan(ctx, mocker):
    mocker.patch("jailbee.orphans.resolve_orphan", return_value=_ci("gamma-x", "Stopped"))
    attach = mocker.patch("jailbee.orphans.attach_tmux")
    result = CliRunner().invoke(app, ["orphan", "tmux", "gamma-x"])
    assert result.exit_code == 1
    assert "jailbee orphan start gamma-x" in result.output
    attach.assert_not_called()


def test_not_an_orphan_exits_1(ctx, mocker):
    mocker.patch("jailbee.orphans.resolve_orphan", side_effect=ValueError("belongs to repo"))
    result = CliRunner().invoke(app, ["orphan", "shell", "alpha-one"])
    assert result.exit_code == 1
    assert "belongs to repo" in result.output


def test_missing_name_single_orphan_is_taken(ctx, mocker):
    mocker.patch("jailbee.orphans.list_orphans", return_value=[_ci("gamma-x")])
    attach = mocker.patch("jailbee.orphans.attach_shell", return_value=0)
    result = CliRunner().invoke(app, ["orphan", "shell"])
    assert result.exit_code == 0, result.output
    attach.assert_called_once_with(ctx, "gamma-x", "dev")


def test_missing_name_for_destroy_is_never_assumed(ctx, mocker):
    mocker.patch("jailbee.orphans.list_orphans", return_value=[_ci("gamma-x")])
    destroy = mocker.patch("jailbee.orphans.destroy_orphan")
    result = CliRunner().invoke(app, ["orphan", "destroy", "--force"])
    assert result.exit_code == 2
    assert "gamma-x" in result.output
    destroy.assert_not_called()


def test_destroy_force(ctx, mocker):
    mocker.patch("jailbee.orphans.resolve_orphan", return_value=_ci("gamma-x"))
    destroy = mocker.patch("jailbee.orphans.destroy_orphan")
    result = CliRunner().invoke(app, ["orphan", "destroy", "gamma-x", "--force"])
    assert result.exit_code == 0, result.output
    destroy.assert_called_once_with(ctx, "gamma-x", force=True)


def test_destroy_without_force_off_tty_refuses(ctx, mocker):
    mocker.patch("jailbee.orphans.resolve_orphan", return_value=_ci("gamma-x"))
    destroy = mocker.patch("jailbee.orphans.destroy_orphan")
    result = CliRunner().invoke(app, ["orphan", "destroy", "gamma-x"])
    assert result.exit_code == 2
    destroy.assert_not_called()


def test_destroy_asks_and_honours_no(ctx, mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting.confirm", return_value=False)
    mocker.patch("jailbee.orphans.resolve_orphan", return_value=_ci("gamma-x"))
    destroy = mocker.patch("jailbee.orphans.destroy_orphan")
    result = CliRunner().invoke(app, ["orphan", "destroy", "gamma-x"])
    assert result.exit_code == 1
    destroy.assert_not_called()


def test_start_starts_a_stopped_orphan(ctx, mocker):
    mocker.patch("jailbee.orphans.resolve_orphan", return_value=_ci("gamma-x", "Stopped"))
    result = CliRunner().invoke(app, ["orphan", "start", "gamma-x"])
    assert result.exit_code == 0, result.output
    ctx.start.assert_called_once_with("gamma-x")


def test_stop_stops_a_running_orphan(ctx, mocker):
    mocker.patch("jailbee.orphans.resolve_orphan", return_value=_ci("gamma-x"))
    stop = mocker.patch("jailbee.stopping.stop_container")
    result = CliRunner().invoke(app, ["orphan", "stop", "gamma-x"])
    assert result.exit_code == 0, result.output
    stop.assert_called_once_with(ctx, "gamma-x", force=False, force_fallback=True)
