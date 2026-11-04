"""Profile CLI status, errors, and missing-value resolution."""

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from tests.test_autostart_profiles import profile_cfg, state_incus


def setup(make_cfg, tmp_path, mocker, current="light"):
    from jailbee.autostart_profiles import CREATED_KEY, PROFILE_KEY

    cfg = profile_cfg(make_cfg, tmp_path)
    incus, state = state_incus(mocker, {PROFILE_KEY: current, CREATED_KEY: "deps"})
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "c"))
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    return state


def test_switch_show_lists_profiles_marks_default_and_current(make_cfg, tmp_path, mocker):
    setup(make_cfg, tmp_path, mocker)
    result = CliRunner().invoke(app, ["autostart", "profile", "c"])
    assert result.exit_code == 0, result.output
    assert "light" in result.output and "current" in result.output and "default" in result.output
    assert "full" in result.output and "deps" in result.output


def test_switch_to_lighter_runs_nothing_prints_deferred(make_cfg, tmp_path, mocker):
    setup(make_cfg, tmp_path, mocker, current="full")
    result = CliRunner().invoke(app, ["autostart", "profile", "c", "light"])
    assert result.exit_code == 0, result.output
    assert "nothing to start; takes effect on the next start" in result.output


def test_switch_unknown_name_exits_2(make_cfg, tmp_path, mocker):
    setup(make_cfg, tmp_path, mocker)
    result = CliRunner().invoke(app, ["autostart", "profile", "c", "bad"])
    assert result.exit_code == 2
    assert "autostart profile `bad` is not defined" in result.output


def test_switch_prompts_for_missing_container_and_name(make_cfg, tmp_path, mocker):
    state = setup(make_cfg, tmp_path, mocker)
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    picker = mocker.patch("jailbee.prompting._select", return_value="full")
    result = CliRunner().invoke(app, ["autostart", "profile", "--no-run"])
    assert result.exit_code == 0, result.output
    picker.assert_called_once()
    assert state["user.jailbee.autostart_profile"] == "full"


def test_profile_cmd_without_repo_profiles_switches_to_none(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import PROFILE_KEY

    cfg = make_cfg(tmp_path)
    incus, _ = state_incus(mocker)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "c"))
    mocker.patch("jailbee.autostart_status.live_run", return_value=None)
    result = CliRunner().invoke(app, ["autostart", "profile", "c", "none", "--no-run"])
    assert result.exit_code == 0, result.output
    incus.config_set.assert_any_call("c", PROFILE_KEY, "none")


@pytest.fixture
def restart_env(make_cfg, tmp_path, mocker):
    from jailbee.autostart_profiles import PROFILE_KEY, SERVICES_KEY

    cfg = profile_cfg(make_cfg, tmp_path)
    incus, state = state_incus(mocker, {PROFILE_KEY: "light", SERVICES_KEY: "old"})
    incus.config_unset.side_effect = lambda c, k: state.pop(k, None)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "c"))
    mocker.patch("jailbee.cli._refuse_while_autostart_runs")
    mocker.patch("jailbee.cli._preflight_cache_pools")
    boot = mocker.patch("jailbee.lifecycle.boot_container")
    spawn = mocker.patch("jailbee.cli._spawn_boot_worker")
    mocker.patch("jailbee.cli._post_start_actions")
    mocker.patch("jailbee.cli._autostart_detach_handler")
    mocker.patch("jailbee.cli._mirror_endpoint_or_none")
    mocker.patch("jailbee.cli._clear_superseded_boot_job")
    return incus, state, boot, spawn


@pytest.mark.parametrize("background", [False, True])
def test_restart_stores_profile_before_boot(restart_env, background):
    _, state, boot, spawn = restart_env
    target = spawn if background else boot

    def check_before_boot(*args, **kwargs):
        assert state["user.jailbee.autostart_profile"] == "none"

    target.side_effect = check_before_boot
    mode = "--background" if background else "--no-background"
    result = CliRunner().invoke(app, ["restart", "c", "--autostart-profile", "none", mode])
    assert result.exit_code == 0, result.output
    assert target.called


def test_restart_services_empty_stores_none(restart_env):
    _, state, _, _ = restart_env
    result = CliRunner().invoke(
        app, ["restart", "c", "--services", "", "--no-autostart", "--no-background"]
    )
    assert result.exit_code == 0, result.output
    assert state["user.jailbee.autostart_services"] == ","
    assert state["user.jailbee.autostart_profile"] == "light"


def test_restart_services_are_trimmed_and_stored_before_background_boot(restart_env):
    _, state, _, spawn = restart_env

    def check_before_boot(*args, **kwargs):
        assert state["user.jailbee.autostart_services"] == "api,web"

    spawn.side_effect = check_before_boot
    result = CliRunner().invoke(
        app, ["restart", "c", "--services", " api, web ", "--background"]
    )
    assert result.exit_code == 0, result.output
    assert spawn.called


def test_restart_reset_services_unsets_key(restart_env):
    incus, state, _, _ = restart_env
    result = CliRunner().invoke(app, ["restart", "c", "--reset-services", "--no-background"])
    assert result.exit_code == 0, result.output
    assert "user.jailbee.autostart_services" not in state
    incus.config_unset.assert_called_once_with("c", "user.jailbee.autostart_services")


@pytest.mark.parametrize(
    "args,message",
    [
        (["--services", "", "--reset-services"], "cannot be used together"),
        (["--autostart-profile", "bad"], "autostart profile `bad` is not defined"),
        (["--autostart-profile", "none", "--services", "bad name"], "Invalid service name"),
    ],
)
def test_restart_invalid_choice_has_no_side_effects(restart_env, args, message):
    incus, state, boot, spawn = restart_env
    result = CliRunner().invoke(app, ["restart", "c", "--no-background", *args])
    assert result.exit_code == 2, result.output
    assert message in result.output
    assert state == {
        "user.jailbee.autostart_profile": "light",
        "user.jailbee.autostart_services": "old",
    }
    incus.config_set.assert_not_called()
    incus.config_unset.assert_not_called()
    boot.assert_not_called()
    spawn.assert_not_called()


def test_restart_omitted_choices_preserve_existing_values(restart_env):
    incus, state, _, _ = restart_env
    result = CliRunner().invoke(app, ["restart", "c", "--no-background"])
    assert result.exit_code == 0, result.output
    assert state == {
        "user.jailbee.autostart_profile": "light",
        "user.jailbee.autostart_services": "old",
    }
    incus.config_set.assert_not_called()
    incus.config_unset.assert_not_called()
