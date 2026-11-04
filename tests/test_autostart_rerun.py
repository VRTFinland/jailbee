"""In-place re-runs reuse the real stage executor and its cleanup."""

import pytest

from jailbee.autostart import AutostartCancelledError, AutostartStepError
from jailbee.autostart_plan import AutostartTrigger, RerunTarget, rerun_block
from jailbee.config import Autostart
from jailbee.tmux import TmuxStepError


def rerun_cfg(make_cfg, tmp_path):
    return make_cfg(
        tmp_path,
        agents={"claude": {"autostart": True}},
        autostart={
            "on_create": [{"stage": "setup", "steps": [{"name": "init", "run": "true"}]}],
            "on_start": [{"stage": "deps", "network": "loose", "mounts": ["m"],
                          "steps": [{"name": "sync", "run": "true"},
                                    {"name": "other", "run": "true"}]}],
            "profiles": {"full": "all", "light": {"stages": ["setup"]}},
            "default_profile": "full",
        },
    )


def execution_deps(mocker):
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/r")
    mocker.patch("jailbee.docker_daemon.mirror_endpoint_or_none", return_value=None)
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    switch = mocker.patch("jailbee.lifecycle.switch_network")
    mocker.patch("jailbee.autostart.is_attached", return_value=False)
    add = mocker.patch("jailbee.autostart.add_optional_mount")
    remove = mocker.patch("jailbee.autostart.remove_optional_mount")
    mocker.patch("jailbee.autostart.tmux.ensure_session")
    mocker.patch("jailbee.autostart.tmux.interrupt_window")
    step = mocker.patch("jailbee.autostart.tmux.run_step")
    return step, switch, add, remove


@pytest.mark.parametrize("cancelled", [False, True], ids=["failure", "cancellation"])
def test_foreground_step_rerun_restores_network_and_mounts_on_failure(
    make_cfg, tmp_path, mocker, cancelled
):
    from jailbee.autostart_rerun import run_foreground

    cfg = rerun_cfg(make_cfg, tmp_path)
    incus = mocker.MagicMock()
    step, switch, add, remove = execution_deps(mocker)
    step.side_effect = (
        AutostartCancelledError(container="c", signum=15) if cancelled else
        TmuxStepError("failed", step_name="sync", reason="exit", exit_code=1)
    )
    block = rerun_block(cfg.autostart, [RerunTarget(AutostartTrigger.ON_START, "deps", "sync")])
    with pytest.raises(AutostartCancelledError if cancelled else AutostartStepError):
        run_foreground(cfg, incus, "c", block)
    assert [call.args[3] for call in switch.call_args_list] == ["loose", "strict"]
    add.assert_called_once()
    remove.assert_called_once_with(mocker.ANY, incus, "c", "m")
    assert step.call_args.kwargs["name"] == "sync"


def test_rerun_never_launches_agents(make_cfg, tmp_path, mocker):
    from jailbee.autostart_rerun import run_foreground

    cfg = rerun_cfg(make_cfg, tmp_path)
    step, _, _, _ = execution_deps(mocker)
    agents = mocker.patch("jailbee.autostart._on_start_agent_steps", side_effect=AssertionError("agent helper"))
    services = mocker.patch("jailbee.services.autostart.services_stage_wanted", side_effect=AssertionError("services helper"))
    block = rerun_block(cfg.autostart, [RerunTarget(AutostartTrigger.ON_START, "deps", "sync")])
    run_foreground(cfg, mocker.MagicMock(), "c", block)
    assert [call.kwargs["name"] for call in step.call_args_list] == ["sync"]
    agents.assert_not_called()
    services.assert_not_called()
    assert cfg.agents["claude"].autostart is True


def test_refused_while_supervisor_live(make_cfg, tmp_path, mocker):
    from jailbee.autostart_rerun import prepare

    cfg = rerun_cfg(make_cfg, tmp_path)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    mocker.patch("jailbee.autostart_status.refuse_while_running", side_effect=RuntimeError("live"))
    execute = mocker.patch("jailbee.autostart.run_autostart")
    with pytest.raises(RuntimeError, match="live"):
        prepare(cfg, incus, "c", [])
    execute.assert_not_called()
    assert not list(tmp_path.rglob("*.job.json"))


def test_refused_when_stopped(make_cfg, tmp_path, mocker):
    from jailbee.autostart_rerun import prepare

    incus = mocker.MagicMock()
    incus.list_containers.return_value = [{"name": "c", "status": "Stopped"}]
    with pytest.raises(RuntimeError, match="running"):
        prepare(rerun_cfg(make_cfg, tmp_path), incus, "c", [])


def test_prepare_uses_stored_profile(make_cfg, tmp_path, mocker):
    from jailbee.autostart_rerun import prepare

    cfg = rerun_cfg(make_cfg, tmp_path)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    incus.config_get.return_value = "light"
    block = prepare(cfg, incus, "c", [RerunTarget(AutostartTrigger.ON_START, "deps", None)])
    assert block.on_start == []
    assert cfg.autostart.on_start


def test_effective_config_resolves_profile_before_selection(make_cfg, tmp_path, mocker):
    from jailbee.autostart_rerun import effective_config

    cfg = rerun_cfg(make_cfg, tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.return_value = "light"
    effective = effective_config(cfg, incus, "c")
    assert [stage.stage for stage in effective.autostart.on_create] == ["setup"]
    assert effective.autostart.on_start == []
    assert cfg.autostart.on_start


def test_failed_targets_drop_stale_names(make_cfg, tmp_path):
    from jailbee import autostart_outcomes as outcomes
    from jailbee.autostart_profiles import profiled_config
    from jailbee.autostart_rerun import failed_targets

    for trigger, stage, step in [("on_create", "setup", "init"), ("on_start", "deps", "sync"),
                                 ("on_start", "deps", "gone"), ("on_start", "old", "sync"),
                                 ("unknown", "deps", "sync"), ("on_start", "agents", "claude")]:
        outcomes.record("c", trigger, stage, step, "fail")
    cfg = rerun_cfg(make_cfg, tmp_path)
    assert failed_targets(profiled_config(cfg, "full"), "c") == [
        RerunTarget(AutostartTrigger.ON_CREATE, "setup", "init"),
        RerunTarget(AutostartTrigger.ON_START, "deps", "sync"),
    ]
    assert failed_targets(profiled_config(cfg, "light"), "c") == [
        RerunTarget(AutostartTrigger.ON_CREATE, "setup", "init"),
    ]


@pytest.mark.parametrize("fail", [False, True])
def test_on_create_rerun_records_created(make_cfg, tmp_path, mocker, fail):
    from jailbee.autostart_rerun import first_trigger, run_foreground

    cfg = rerun_cfg(make_cfg, tmp_path)
    step, _, _, _ = execution_deps(mocker)
    record = mocker.patch("jailbee.autostart_profiles.record_created")
    block = rerun_block(cfg.autostart, [RerunTarget(AutostartTrigger.ON_CREATE, "setup", None)])
    assert not block.profiles
    assert first_trigger(block) is AutostartTrigger.ON_CREATE
    if fail:
        step.side_effect = RuntimeError("failed")
        with pytest.raises(RuntimeError, match="failed"):
            run_foreground(cfg, mocker.MagicMock(), "c", block)
    else:
        run_foreground(cfg, mocker.MagicMock(), "c", block)
    record.assert_called_once_with(mocker.ANY, "c", "setup")


def test_successful_rerun_clears_only_selected_failure(make_cfg, tmp_path, mocker):
    from jailbee import autostart_outcomes as outcomes
    from jailbee.autostart_rerun import first_trigger, run_foreground

    cfg = rerun_cfg(make_cfg, tmp_path)
    execution_deps(mocker)
    outcomes.record("c", "on_start", "deps", "sync", "fail")
    outcomes.record("c", "on_start", "deps", "other", "fail")
    block = rerun_block(cfg.autostart, [RerunTarget(AutostartTrigger.ON_START, "deps", "sync")])
    assert first_trigger(block) is AutostartTrigger.ON_START
    run_foreground(cfg, mocker.MagicMock(), "c", block)
    assert outcomes.failed_steps(outcomes.read("c")) == [("on_start", "deps", "other")]


def test_first_trigger_rejects_empty_block():
    from jailbee.autostart_rerun import first_trigger

    with pytest.raises(ValueError, match="No.*stage"):
        first_trigger(Autostart())
