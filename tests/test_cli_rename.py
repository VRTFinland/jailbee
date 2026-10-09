from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee import aliases
from jailbee.cli import app

runner = CliRunner()
CFG = str(Path(__file__).parent / "fixtures" / "full_config.yaml")


@pytest.fixture
def rig(mocker):
    incus = mocker.MagicMock()
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-a"))
    return (
        incus,
        mocker.patch("jailbee.aliases.set_alias"),
        mocker.patch("jailbee.aliases.clear_alias"),
    )


def test_rename_sets_alias(rig):
    incus, set_alias, _ = rig
    result = runner.invoke(app, ["rename", "a", "login", "--config", CFG])
    assert result.exit_code == 0, result.output
    assert set_alias.call_args.args[1:] == (incus, "myrepo-a", "login")
    assert "login" in result.output


def test_rename_clear(rig):
    incus, _, clear_alias = rig
    result = runner.invoke(app, ["rename", "a", "--clear", "--config", CFG])
    assert result.exit_code == 0, result.output
    clear_alias.assert_called_once_with(incus, "myrepo-a")


def test_rename_alias_and_clear_are_exclusive(rig):
    result = runner.invoke(app, ["rename", "a", "login", "--clear", "--config", CFG])
    assert result.exit_code == 2


def test_rename_without_alias_non_interactive(rig, mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = runner.invoke(app, ["rename", "a", "--config", CFG])
    assert result.exit_code == 2
    assert "alias" in result.output


def test_rename_alias_error_exits_2(rig):
    _, set_alias, _ = rig
    set_alias.side_effect = aliases.AliasError("nope-x")
    result = runner.invoke(app, ["rename", "a", "login", "--config", CFG])
    assert result.exit_code == 2
    assert "nope-x" in result.output
