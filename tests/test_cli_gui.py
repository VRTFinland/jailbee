from pathlib import Path

from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config import ConfigNotFoundError

runner = CliRunner()


def test_dashboard_gui_flag_detaches(mocker):
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.qtui.app.preflight", return_value=[Path("/tmp/x")])
    qrun = mocker.patch("jailbee.qtui.app.run", return_value=0)
    popen = mocker.patch("subprocess.Popen")

    result = runner.invoke(app, ["dashboard", "--gui"])

    assert result.exit_code == 0, result.output
    popen.assert_called_once()
    argv = popen.call_args.args[0]
    assert "gui" in argv
    assert "--foreground" in argv
    assert popen.call_args.kwargs["start_new_session"] is True
    qrun.assert_not_called()


def test_gui_alias_detaches(mocker):
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.qtui.app.preflight", return_value=[Path("/tmp/x")])
    qrun = mocker.patch("jailbee.qtui.app.run", return_value=0)
    popen = mocker.patch("subprocess.Popen")

    result = runner.invoke(app, ["gui"])

    assert result.exit_code == 0, result.output
    popen.assert_called_once()
    argv = popen.call_args.args[0]
    assert "gui" in argv
    assert "--foreground" in argv
    assert popen.call_args.kwargs["start_new_session"] is True
    qrun.assert_not_called()


def test_gui_foreground_runs_inprocess(mocker):
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    mocker.patch("jailbee.incus.Incus")
    qrun = mocker.patch("jailbee.qtui.app.run", return_value=0)
    popen = mocker.patch("subprocess.Popen")

    result = runner.invoke(app, ["gui", "--foreground"])

    assert result.exit_code == 0, result.output
    qrun.assert_called_once()
    popen.assert_not_called()


def test_gui_no_configs_errors_before_detach(mocker):
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.qtui.app.preflight", return_value=None)
    popen = mocker.patch("subprocess.Popen")

    result = runner.invoke(app, ["gui"])

    assert result.exit_code == 1
    assert "config" in result.output.lower() or "repos" in result.output.lower()
    popen.assert_not_called()


def test_dashboard_without_flag_uses_tui(mocker):
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    mocker.patch("jailbee.incus.Incus")
    qrun = mocker.patch("jailbee.qtui.app.run", return_value=0)
    drun = mocker.patch("jailbee.dashboard.run", return_value=0)
    popen = mocker.patch("subprocess.Popen")

    result = runner.invoke(app, ["dashboard"])

    assert result.exit_code == 0
    drun.assert_called_once()
    qrun.assert_not_called()
    popen.assert_not_called()


def test_gui_missing_pyside_shows_install_hint(mocker):
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    mocker.patch("jailbee.incus.Incus")
    popen = mocker.patch("subprocess.Popen")
    # Simulate PySide6 not installed: importing qtui.app raises ImportError.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "jailbee.qtui.app" or name.startswith("PySide6"):
            raise ImportError("No module named 'PySide6'")
        return real_import(name, *args, **kwargs)

    mocker.patch("builtins.__import__", side_effect=fake_import)
    result = runner.invoke(app, ["gui"])
    assert result.exit_code == 1
    assert "PySide6" in result.output
    # The reader hitting this is usually a PyPI install, not a repo
    # checkout, so the command they can actually run comes first.
    assert "uv tool install 'jailbee[gui]'" in result.output
    popen.assert_not_called()


def test_gui_detach_omits_interval_when_not_given(mocker):
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.qtui.app.preflight", return_value=[Path("/tmp/x")])
    popen = mocker.patch("subprocess.Popen")

    runner.invoke(app, ["gui"])
    argv = popen.call_args.args[0]
    assert "--interval" not in argv  # let the persisted value win downstream


def test_gui_detach_forwards_explicit_interval(mocker):
    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigNotFoundError("none"))
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.qtui.app.preflight", return_value=[Path("/tmp/x")])
    mocker.patch("jailbee.qtui.app.run", return_value=0)
    popen = mocker.patch("subprocess.Popen")

    runner.invoke(app, ["gui", "--interval", "5"])
    argv = popen.call_args.args[0]
    assert "--interval" in argv
    assert "5.0" in argv or "5" in argv
