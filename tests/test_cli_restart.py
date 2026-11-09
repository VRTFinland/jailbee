"""Tests for the `gie restart` CLI command."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee.autostart_plan import AutostartPlan
from jailbee.cli import app

# `run_autostart` returns the plan the CLI hands to a detached worker; a bare
# MagicMock would be serialised into the job file.
_NO_DETACHED = AutostartPlan(blocking=[], detached=[])

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def _common_mocks(mocker):
    """Patch out side-effectful dependencies shared by all restart tests."""
    mocker.patch(
        "jailbee.cli._resolve_existing",
        return_value=(mocker.MagicMock(), "myrepo-feat-x"),
    )
    mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch(
        "jailbee.lifecycle.current_network_mode",
        return_value="loose",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=False)
    mocker.patch(
        "jailbee.branch_config.effective_autostart_config", side_effect=lambda cfg, *a, **kw: cfg
    )


@pytest.mark.parametrize("command", ["start", "restart"])
def test_boot_uses_committed_legacy_startup_with_host_services(command, tmp_path, make_cfg, mocker):
    from jailbee.config import AutostartStage, Service

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = make_cfg(repo).model_copy(update={"services": {"web": Service(run="serve")}})
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    incus.exec.return_value = (
        "autostart:\n  on_start:\n    - name: branch-step\n      run: branch-command\n"
    )
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat-x"))
    mocker.patch("jailbee.cli._resolve_config_path_or_none", return_value=None)
    mocker.patch("jailbee.lifecycle.boot_container")
    mocker.patch("jailbee.cli._clear_superseded_boot_job")
    mocker.patch("jailbee.cli._mirror_endpoint_or_none", return_value=None)
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.autostart.inject_github_token")
    mocker.patch("jailbee.hosts.sync_hosts")
    mocker.patch("jailbee.egress_proxy.sync_container")
    mocker.patch("jailbee.cli._post_create_gui_launches")
    profile = mocker.patch("jailbee.autostart_profiles.resolve_profile", return_value=None)
    stage = AutostartStage(stage="deferred", steps=[{"name": "s", "run": "true"}])
    run = mocker.patch(
        "jailbee.autostart.run_autostart",
        return_value=AutostartPlan(blocking=[], detached=[stage]),
    )
    spawn = mocker.patch("jailbee.cli._spawn_autostart_worker")

    result = runner.invoke(app, [command, "myrepo-feat-x", "--yes"])

    assert result.exit_code == 0, result.output
    incus.exec.assert_called_once_with(
        "myrepo-feat-x", ["git", "-C", "/repo", "show", "HEAD:.jailbee/config.yaml"]
    )
    assert profile.call_args.args[0].autostart.on_start[0].run == "branch-command"
    assert run.call_args.args[0].autostart.on_start[0].run == "branch-command"
    assert spawn.call_args.kwargs["autostart"].on_start[0].run == "branch-command"
    assert spawn.call_args.kwargs["services_present"] is False


def test_restart_runs_autostart_on_start_trigger(mocker):
    """Restart must run autostart, just like start."""
    _common_mocks(mocker)
    run_autostart = mocker.patch("jailbee.autostart.run_autostart", return_value=_NO_DETACHED)

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
    run_autostart = mocker.patch("jailbee.autostart.run_autostart", return_value=_NO_DETACHED)

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
    mocker.patch("jailbee.autostart.run_autostart", return_value=_NO_DETACHED)
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


def test_restart_syncs_the_proxy_with_the_current_mode(mocker):
    """Restart re-syncs the proxy env and rules next to the /etc/hosts pin."""
    from jailbee import egress_proxy

    _common_mocks(mocker)
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.autostart.run_autostart", return_value=_NO_DETACHED)
    mocker.patch("jailbee.hosts.apply_hosts")
    sync = mocker.patch.object(egress_proxy, "sync_container")

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
    sync.assert_called_once()
    assert sync.call_args.args[2:] == ("myrepo-feat-x", "strict")


def test_restart_launches_chrome_and_ide_when_gui_available(mocker):
    _common_mocks(mocker)
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=True)
    mocker.patch("jailbee.autostart.run_autostart", return_value=_NO_DETACHED)
    launch = mocker.patch("jailbee.apps.launch")

    result = runner.invoke(
        app,
        ["restart", "myrepo-feat-x", "--config", str(FIXTURES / "full_config.yaml")],
    )

    assert result.exit_code == 0, result.output
    launched = {c.args[3].name for c in launch.call_args_list}
    assert launched == {"ide", "chrome"}


def test_restart_continues_launching_chrome_after_ide_launcher_is_missing(mocker):
    """Finding 1 (review round): the IDE and Chrome autostart launches in
    `_post_start_actions` are independent — a missing Toolbox launcher for
    the IDE (an ordinary state, not a crash) must not also skip Chrome, and
    must not abort `jailbee restart`.

    Task 17: this containment now lives in `apps.launch_autostart_apps`, not
    in a `cli.py`-local wrapper, so the reported-failure assertion patches
    `jailbee.tui.error` (where `apps.py` calls it) rather than `jailbee.cli.error`.
    """
    _common_mocks(mocker)
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=True)
    mocker.patch("jailbee.autostart.run_autostart", return_value=_NO_DETACHED)
    error_mock = mocker.patch("jailbee.tui.error")

    def fake_launch(cfg, incus, container, spec, args=None, **kwargs):
        if spec.name == "ide":
            raise ValueError("No idea launcher found in /opt/jetbrains-toolbox/apps")

    launch = mocker.patch("jailbee.apps.launch", side_effect=fake_launch)

    result = runner.invoke(
        app,
        ["restart", "myrepo-feat-x", "--config", str(FIXTURES / "full_config.yaml")],
    )

    assert result.exit_code == 0, result.output
    error_mock.assert_called_once()
    assert "idea" in error_mock.call_args.args[0]
    launched = {c.args[3].name for c in launch.call_args_list}
    assert launched == {"ide", "chrome"}


def test_restart_skips_ide_when_jetbrains_disabled(mocker, tmp_path):
    """jetbrains.enabled=false suppresses the autostart IDE launch even when
    jetbrains.autostart=true and a graphical session is detected."""
    _common_mocks(mocker)
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=True)
    mocker.patch("jailbee.autostart.run_autostart", return_value=_NO_DETACHED)

    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gie").mkdir()
    (repo / ".gie" / "config.yaml").write_text(
        "jetbrains:\n  enabled: false\n  autostart: true\nchrome:\n  autostart: false\n"
    )

    launch = mocker.patch("jailbee.apps.launch")

    result = runner.invoke(
        app, ["restart", "myrepo-feat-x", "--config", str(repo / ".gie" / "config.yaml")]
    )

    assert result.exit_code == 0, result.output
    launch.assert_not_called()


def test_restart_skips_chrome_when_chrome_disabled(mocker, tmp_path):
    """chrome.enabled=false suppresses the autostart Chrome launch even when
    chrome.autostart=true and a graphical session is detected."""
    _common_mocks(mocker)
    mocker.patch("jailbee.autostart.has_graphical_session", return_value=True)
    mocker.patch("jailbee.autostart.run_autostart", return_value=_NO_DETACHED)

    repo = tmp_path / "myrepo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".gie").mkdir()
    (repo / ".gie" / "config.yaml").write_text(
        "chrome:\n  enabled: false\n  autostart: true\njetbrains:\n  autostart: false\n"
    )

    launch = mocker.patch("jailbee.apps.launch")

    result = runner.invoke(
        app, ["restart", "myrepo-feat-x", "--config", str(repo / ".gie" / "config.yaml")]
    )

    assert result.exit_code == 0, result.output
    launch.assert_not_called()
