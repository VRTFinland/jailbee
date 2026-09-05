"""The `jailbee config edit` command surface.

The editor itself is mocked out — what is under test is which layer, which
file and which write policy the command resolves, that it refuses to open
without a terminal, and that `config init` hands off to it on the right
layer for each of its two branches.
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
    mocker.patch("jailbee.cli._is_full_screen_tty", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit", "--config", str(cfg)])

    assert result.exit_code == 0
    kwargs = run.call_args.kwargs
    assert kwargs["layer"] == "repo"
    assert kwargs["layer_set"].repo_path == cfg
    assert kwargs["policy"] == "patch"


def test_global_opens_the_global_layer_and_regenerates(tmp_path, mocker):
    cfg = _repo(tmp_path)
    mocker.patch("jailbee.cli._is_full_screen_tty", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit", "--global", "--config", str(cfg)])

    assert result.exit_code == 0
    assert run.call_args.kwargs["layer"] == "global"
    assert run.call_args.kwargs["policy"] == "regenerate"


def test_the_write_flag_wins(tmp_path, mocker):
    cfg = _repo(tmp_path)
    mocker.patch("jailbee.cli._is_full_screen_tty", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    runner.invoke(app, ["config", "edit", "--config", str(cfg), "--write", "regenerate"])

    assert run.call_args.kwargs["policy"] == "regenerate"


def test_an_unknown_write_policy_is_a_usage_error(tmp_path, mocker):
    cfg = _repo(tmp_path)
    mocker.patch("jailbee.cli._is_full_screen_tty", return_value=True)
    result = runner.invoke(app, ["config", "edit", "--config", str(cfg), "--write", "clobber"])
    assert result.exit_code == 2
    assert "patch" in result.output


def test_it_refuses_without_a_terminal(tmp_path, mocker):
    cfg = _repo(tmp_path)
    mocker.patch("jailbee.cli._is_full_screen_tty", return_value=False)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit", "--config", str(cfg)])

    assert result.exit_code == 1
    assert "terminal" in result.output
    run.assert_not_called()


def test_it_refuses_a_directory_whose_config_is_synthesized(tmp_path, mocker, monkeypatch):
    """A directory with no config file still has one — `global.yaml`'s
    `scratch.config`, merged in by `scratch_repo_layer`. The editor knows two
    layers, not that third one, so every row would show a value the directory
    does not use, and the first save would create a config file that stops the
    layer being used at all (the shared scratch base image included).
    """
    monkeypatch.chdir(tmp_path)
    mocker.patch("jailbee.cli._is_full_screen_tty", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit"])

    assert result.exit_code == 1
    assert "scratch.config" in result.output
    assert "config init" in result.output
    run.assert_not_called()
    assert not (tmp_path / ".jailbee").exists()


def test_a_directory_with_no_config_and_no_synthesis_gets_the_path_it_would_create(
    tmp_path, mocker, monkeypatch
):
    """With `scratch.enabled: false` there is no third layer to lose, so the
    editor opens on an empty repo layer and a save creates the file — the same
    answer `jailbee config init` would give for this directory.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    from jailbee.global_config import default_global_config_path

    gpath = default_global_config_path()
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text("scratch:\n  enabled: false\n")
    mocker.patch("jailbee.cli._is_full_screen_tty", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit"])

    assert result.exit_code == 0
    assert run.call_args.kwargs["layer_set"].repo_path == tmp_path / ".jailbee" / "config.yaml"


def test_the_global_layer_is_editable_from_a_synthesized_directory(tmp_path, mocker, monkeypatch):
    """`--global` edits `global.yaml`, which is exactly where such a
    directory's settings live — the repo-layer refusal must not reach it.
    """
    monkeypatch.chdir(tmp_path)
    mocker.patch("jailbee.cli._is_full_screen_tty", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit", "--global"])

    assert result.exit_code == 0
    assert run.call_args.kwargs["layer"] == "global"


def test_it_refuses_when_stdout_is_not_a_terminal(tmp_path, mocker):
    """`jailbee config edit > out.txt` has a usable stdin and would still paint
    a full-screen application into the file. `_is_tty` alone cannot see that.
    """
    cfg = _repo(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    # `CliRunner` redirects stdout to a buffer whose `isatty()` is False —
    # exactly the shape a shell redirect produces, and the reason this needs
    # no patch of its own.
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "edit", "--config", str(cfg)])

    assert result.exit_code == 1
    run.assert_not_called()


def test_config_init_offers_the_editor(tmp_path, mocker, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    mocker.patch("jailbee.cli._is_full_screen_tty", return_value=True)
    mocker.patch("jailbee.cli.default_confirm", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "init"])

    assert result.exit_code == 0
    run.assert_called_once()
    assert run.call_args.kwargs["layer"] == "repo"


def test_config_init_global_offers_the_editor(tmp_path, mocker, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    mocker.patch("jailbee.cli._is_full_screen_tty", return_value=True)
    mocker.patch("jailbee.cli.default_confirm", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    result = runner.invoke(app, ["config", "init", "--global"])

    assert result.exit_code == 0
    run.assert_called_once()
    assert run.call_args.kwargs["layer"] == "global"


def test_config_init_does_not_offer_the_editor_without_a_terminal(tmp_path, mocker, monkeypatch):
    """The `_is_tty` gate is what stops `echo y | jailbee config init` from
    launching a full-screen application on a pipe.

    The question must not even be *asked* there, which is what this asserts.
    `run.assert_not_called()` alone cannot see the gate: under `CliRunner`
    stdin is empty, so `Confirm.ask` raises `EOFError` and `default_confirm`
    returns False by itself — and `config_edit_cmd`'s own full-screen gate
    would refuse the launch besides. `default_confirm` is therefore both
    patched to say yes *and* asserted against.
    """
    monkeypatch.chdir(tmp_path)
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    confirm = mocker.patch("jailbee.cli.default_confirm", return_value=True)
    run = mocker.patch("jailbee.config_edit.app.run_editor", return_value=0)

    runner.invoke(app, ["config", "init"])

    confirm.assert_not_called()
    run.assert_not_called()
