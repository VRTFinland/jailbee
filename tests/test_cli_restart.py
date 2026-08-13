"""Tests for the `gie restart` CLI command."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def _common_mocks(mocker):
    """Patch out side-effectful dependencies shared by all restart tests."""
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    mocker.patch("jailbee.lifecycle.restart_container")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="loose",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=False)


def test_restart_runs_autostart_on_start_trigger(mocker):
    """Restart must run autostart, just like start."""
    _common_mocks(mocker)
    run_autostart = mocker.patch("jailbee.autostart.run_autostart")

    result = runner.invoke(
        app,
        ["restart", "myrepo-feat-x", "--config", str(FIXTURES / "full_config.yaml")],
    )

    assert result.exit_code == 0, result.output
    run_autostart.assert_called_once()
    # ON_START trigger — restart is conceptually a fresh boot.
    from jailbee.autostart import AutostartTrigger

    assert run_autostart.call_args.args[3] == AutostartTrigger.ON_START


def test_restart_no_autostart_flag_skips_autostart(mocker):
    """--no-autostart on restart should skip autostart, mirroring start."""
    _common_mocks(mocker)
    run_autostart = mocker.patch("jailbee.autostart.run_autostart")

    result = runner.invoke(
        app,
        [
            "restart",
            "myrepo-feat-x",
            "--no-autostart",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    run_autostart.assert_not_called()


def test_restart_pins_hosts_for_strict_container(mocker):
    """Restart on strict network must re-pin /etc/hosts (mirrors start)."""
    _common_mocks(mocker)
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="strict",
    )
    mocker.patch("jailbee.autostart.run_autostart")
    apply = mocker.patch("jailbee.hosts.apply_hosts")

    result = runner.invoke(
        app,
        [
            "restart",
            "myrepo-feat-x",
            "--no-autostart",
            "--config",
            str(FIXTURES / "full_config.yaml"),
        ],
    )

    assert result.exit_code == 0, result.output
    apply.assert_called_once()


def test_restart_launches_chrome_and_ide_when_gui_available(mocker):
    _common_mocks(mocker)
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=True)
    mocker.patch("jailbee.autostart.run_autostart")
    open_chrome = mocker.patch("jailbee.gui.open_chrome")
    open_ide = mocker.patch("jailbee.gui.open_ide")

    result = runner.invoke(
        app,
        ["restart", "myrepo-feat-x", "--config", str(FIXTURES / "full_config.yaml")],
    )

    assert result.exit_code == 0, result.output
    open_chrome.assert_called_once()
    open_ide.assert_called_once()


def test_restart_skips_ide_when_jetbrains_disabled(mocker, tmp_path):
    """jetbrains.enabled=false suppresses the autostart IDE launch even when
    jetbrains.autostart=true and a graphical session is detected."""
    _common_mocks(mocker)
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=True)
    mocker.patch("jailbee.autostart.run_autostart")

    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gie").mkdir()
    (repo / ".gie" / "config.yaml").write_text(
        "jetbrains:\n  enabled: false\n  autostart: true\nchrome:\n  autostart: false\n"
    )

    open_chrome = mocker.patch("jailbee.gui.open_chrome")
    open_ide = mocker.patch("jailbee.gui.open_ide")

    result = runner.invoke(
        app, ["restart", "myrepo-feat-x", "--config", str(repo / ".gie" / "config.yaml")]
    )

    assert result.exit_code == 0, result.output
    open_chrome.assert_not_called()
    open_ide.assert_not_called()


def test_restart_skips_chrome_when_chrome_disabled(mocker, tmp_path):
    """chrome.enabled=false suppresses the autostart Chrome launch even when
    chrome.autostart=true and a graphical session is detected."""
    _common_mocks(mocker)
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=True)
    mocker.patch("jailbee.autostart.run_autostart")

    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gie").mkdir()
    (repo / ".gie" / "config.yaml").write_text(
        "chrome:\n  enabled: false\n  autostart: true\njetbrains:\n  autostart: false\n"
    )

    open_chrome = mocker.patch("jailbee.gui.open_chrome")
    open_ide = mocker.patch("jailbee.gui.open_ide")

    result = runner.invoke(
        app, ["restart", "myrepo-feat-x", "--config", str(repo / ".gie" / "config.yaml")]
    )

    assert result.exit_code == 0, result.output
    open_chrome.assert_not_called()
    open_ide.assert_not_called()
