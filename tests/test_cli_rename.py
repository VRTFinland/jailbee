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


@pytest.mark.parametrize("alias", ["login", "--clear", "-x"])
def test_rename_accepts_the_alias_after_the_separator(rig, alias):
    incus, set_alias, clear_alias = rig
    result = runner.invoke(app, ["rename", "a", "--config", CFG, "--", alias])
    assert result.exit_code == 0, result.output
    assert set_alias.call_args.args[1:] == (incus, "myrepo-a", alias)
    clear_alias.assert_not_called()


@pytest.mark.parametrize(("alias", "default"), [(None, "a"), ("login", "login")])
def test_rename_prompt_starts_on_the_alias_or_the_short_name(
    rig, mocker, alias, default, make_cfg, tmp_path
):
    cfg = make_cfg(tmp_path).model_copy(update={"container_prefix": "myrepo"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus, set_alias, _ = rig
    incus.config_get.side_effect = lambda name, key: (
        alias if (name, key) == ("myrepo-a", "user.jailbee.alias") else None
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    ask = mocker.patch("jailbee.prompting._ask", return_value="a-login")
    result = runner.invoke(app, ["rename", "a", "--config", CFG])
    assert result.exit_code == 0, result.output
    assert ask.call_args.args[1] == default
    assert set_alias.call_args.args[1:] == (incus, "myrepo-a", "a-login")
    assert ask.call_args.kwargs.get("clear_shortcut", False) is (alias is not None)


def test_rename_ctrl_r_clears_the_current_alias(rig, mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path).model_copy(update={"container_prefix": "myrepo"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus, set_alias, clear_alias = rig
    incus.config_get.return_value = "login"
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._ask", side_effect=["", None])
    result = runner.invoke(app, ["rename", "a", "--config", CFG])
    assert result.exit_code == 0, result.output
    clear_alias.assert_called_once_with(incus, "myrepo-a")
    set_alias.assert_not_called()
    assert "has no alias now" in result.output


def test_rename_without_alias_does_not_treat_empty_input_as_clear(rig, mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path).model_copy(update={"container_prefix": "myrepo"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus, set_alias, clear_alias = rig
    incus.config_get.return_value = None
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._ask", side_effect=["", "login"])
    result = runner.invoke(app, ["rename", "a", "--config", CFG])
    assert result.exit_code == 0, result.output
    assert set_alias.call_args.args[1:] == (incus, "myrepo-a", "login")
    clear_alias.assert_not_called()


def test_rename_explicit_empty_alias_is_not_clear(mocker):
    incus = mocker.Mock()
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-a"))
    result = runner.invoke(app, ["rename", "a", "", "--config", CFG])
    assert result.exit_code == 2
    assert "invalid alias" in result.output
    incus.config_unset.assert_not_called()
    incus.config_set.assert_not_called()
