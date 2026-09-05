"""The `jailbee config edit` command surface.

The editor itself is mocked out — what is under test is which layer, which
file and which write policy the command resolves, and that it refuses to open
without a terminal.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


def _repo(tmp_path: Path) -> Path:
    cfg = tmp_path / ".jailbee" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("gpg:\n  enabled: false\n")
    return cfg


def test_it_opens_the_repo_layer_by_default(tmp_path, mocker):
    cfg = _repo(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit", "--config", str(cfg)])

    assert result.exit_code == 0
    kwargs = run.call_args.kwargs
    assert kwargs["layer"] == "repo"
    assert kwargs["layer_set"].repo_path == cfg
    assert kwargs["policy"] == "patch"


def test_global_opens_the_global_layer_and_regenerates(tmp_path, mocker):
    cfg = _repo(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit", "--global", "--config", str(cfg)])

    assert result.exit_code == 0
    assert run.call_args.kwargs["layer"] == "global"
    assert run.call_args.kwargs["policy"] == "regenerate"


def test_the_write_flag_wins(tmp_path, mocker):
    cfg = _repo(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    runner.invoke(app, ["config", "edit", "--config", str(cfg), "--write", "regenerate"])

    assert run.call_args.kwargs["policy"] == "regenerate"


def test_an_unknown_write_policy_is_a_usage_error(tmp_path, mocker):
    cfg = _repo(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    result = runner.invoke(app, ["config", "edit", "--config", str(cfg), "--write", "clobber"])
    assert result.exit_code == 2
    assert "patch" in result.output


def test_it_refuses_without_a_terminal(tmp_path, mocker):
    cfg = _repo(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit", "--config", str(cfg)])

    assert result.exit_code == 1
    assert "terminal" in result.output
    run.assert_not_called()


def test_a_directory_with_no_config_gets_the_path_it_would_create(tmp_path, mocker, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit"])

    assert result.exit_code == 0
    assert run.call_args.kwargs["layer_set"].repo_path == tmp_path / ".jailbee" / "config.yaml"


def test_config_init_offers_the_editor(tmp_path, mocker, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    mocker.patch("jailbee.cli.default_confirm", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0
    run.assert_called_once()


def test_config_init_does_not_offer_the_editor_without_a_terminal(tmp_path, mocker, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    runner.invoke(app, ["config", "init"])

    run.assert_not_called()
