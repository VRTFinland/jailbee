"""Profile CLI status, errors, and missing-value resolution."""

from typer.testing import CliRunner

from jailbee.cli import app
from tests.test_autostart_profiles import profile_cfg, state_incus


def setup(make_cfg, tmp_path, mocker, current="light"):
    from jailbee.autostart_profiles import PROFILE_KEY, CREATED_KEY
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
