"""In-place re-runs reuse the real stage executor and its cleanup."""

import pytest

from jailbee.autostart import AutostartCancelledError, AutostartStepError
from jailbee.autostart_plan import AutostartTrigger, RerunTarget, rerun_block
from jailbee.config import Autostart
from jailbee.tmux import TmuxStepError


def collision_cfg(tmp_path, step_name):
    """Creation-only collisions must remain valid through the real loader."""
    from pathlib import Path

    import yaml

    from jailbee.config.loader import _build_config_from_dict

    raw = yaml.safe_load((Path(__file__).parent / "fixtures/minimal_config.yaml").read_text())
    raw.update(
        container_prefix="myrepo",
        agents={"claude": {"autostart": True}},
        services={"db": {"run": "sleep infinity"}, "db_cache": {"run": "sleep infinity"}},
        autostart={
            "on_create": [
                {"stage": "setup", "steps": [{"name": step_name, "run": "true"}]},
                {"stage": "safe-setup", "steps": [{"name": "safe", "run": "true"}]},
            ]
        },
    )
    return _build_config_from_dict(raw, tmp_path / ".jailbee/config.yaml", skip_git=True)


@pytest.mark.parametrize("step_name", ["claude", "svc-db", "svc-db.cache"])
@pytest.mark.parametrize("selector", ["stage", "step", "failed"])
@pytest.mark.parametrize("wait", [False, True], ids=["detached", "foreground"])
def test_collision_selection_refused_before_any_execution(
    tmp_path, mocker, step_name, selector, wait
):
    from jailbee import autostart_outcomes as outcomes
    from jailbee.autostart_rerun import command

    cfg = collision_cfg(tmp_path, step_name)
    outcomes.record("c", "on_create", "setup", step_name, "fail")
    incus = mocker.MagicMock()
    incus.config_get.return_value = ""
    incus.config_get_checked.return_value = None
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    spawn = mocker.MagicMock()
    kill = mocker.patch("jailbee.tmux.kill_window")
    launch = mocker.patch("jailbee.tmux._new_window")
    execute, _, _, _ = execution_deps(mocker)
    kwargs = dict(
        stages=["setup"] if selector == "stage" else [],
        steps=[f"setup/{step_name}"] if selector == "step" else [],
        failed=selector == "failed",
        trigger=None,
        listing=False,
        json_output=False,
        wait=wait,
        spawn=spawn,
    )
    if selector == "failed":
        command(cfg, incus, "c", **kwargs)
    else:
        with pytest.raises(ValueError, match=r"protected.*window"):
            command(cfg, incus, "c", **kwargs)
    kill.assert_not_called()
    launch.assert_not_called()
    spawn.assert_not_called()
    execute.assert_not_called()
    incus.config_set.assert_not_called()


@pytest.mark.parametrize("step_name", ["claude", "svc-db", "svc-db.cache"])
def test_collision_list_and_picker_offer_only_safe_steps(tmp_path, mocker, step_name):
    from jailbee.autostart_rerun import command, list_rows

    cfg = collision_cfg(tmp_path, step_name)
    rows = list_rows(cfg, "c")
    assert [s["step"] for s in rows[0]["steps"]] == ["safe"]
    incus = mocker.MagicMock()
    incus.config_get.return_value = ""
    incus.config_get_checked.return_value = None
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    picker = mocker.patch(
        "jailbee.prompting._select", side_effect=lambda noun, options: options[0].value
    )
    spawn = mocker.MagicMock()
    command(
        cfg,
        incus,
        "c",
        stages=[],
        steps=[],
        failed=False,
        trigger=None,
        listing=False,
        json_output=False,
        wait=False,
        spawn=spawn,
    )
    assert len(picker.call_args.args[1]) == 2
    block = spawn.call_args.args[1]
    assert [s.name for c in block.on_create[0].all_chains() for s in c.steps] == ["safe"]


@pytest.mark.parametrize("step_name", ["claude", "svc-db", "svc-db.cache"])
@pytest.mark.parametrize("detached", [False, True])
def test_collision_execution_defense_rejects_unfiltered_block(
    tmp_path, mocker, step_name, detached
):
    from dataclasses import replace

    from jailbee.autostart import run_detached
    from jailbee.autostart_rerun import run_foreground
    from tests.test_autostart_supervisor import _spec

    cfg = collision_cfg(tmp_path, step_name)
    incus = mocker.MagicMock()
    step, _, _, _ = execution_deps(mocker)
    kill = mocker.patch("jailbee.tmux.kill_window")
    launch = mocker.patch("jailbee.tmux._new_window")
    with pytest.raises(ValueError, match=r"protected.*window"):
        if detached:
            run_detached(
                cfg,
                incus,
                replace(_spec(tmp_path, cfg.autostart, from_trigger="on_create"), rerun=True),
            )
        else:
            run_foreground(cfg, incus, "c", cfg.autostart)
    step.assert_not_called()
    kill.assert_not_called()
    launch.assert_not_called()
    incus.config_set.assert_not_called()


@pytest.mark.parametrize("step_name", ["claude", "svc-db", "svc-db.cache"])
def test_collision_spawn_defense_refuses_before_job_write(tmp_path, mocker, step_name):
    from jailbee import cli
    from jailbee.db import state_dir

    cfg = collision_cfg(tmp_path, step_name)
    encode = mocker.patch("jailbee.background.autostart_job_to_dict")
    with pytest.raises(ValueError, match=r"protected.*window"):
        cli._spawn_autostart_worker(
            cfg,
            None,
            "c",
            incus=mocker.MagicMock(),
            autostart=cfg.autostart,
            from_trigger="on_create",
            repo_dir="/r",
            mirror_endpoint=None,
            rerun=True,
        )
    encode.assert_not_called()
    assert not list(state_dir().rglob("*.job.json"))


def rerun_cfg(make_cfg, tmp_path):
    return make_cfg(
        tmp_path,
        agents={"claude": {"autostart": True}},
        autostart={
            "on_create": [{"stage": "setup", "steps": [{"name": "init", "run": "true"}]}],
            "on_start": [
                {
                    "stage": "deps",
                    "network": "loose",
                    "mounts": ["m"],
                    "steps": [{"name": "sync", "run": "true"}, {"name": "other", "run": "true"}],
                }
            ],
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
        AutostartCancelledError(container="c", signum=15)
        if cancelled
        else TmuxStepError("failed", step_name="sync", reason="exit", exit_code=1)
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
    agents = mocker.patch(
        "jailbee.autostart._on_start_agent_steps", side_effect=AssertionError("agent helper")
    )
    services = mocker.patch(
        "jailbee.services.autostart.services_stage_wanted",
        side_effect=AssertionError("services helper"),
    )
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

    for trigger, stage, step in [
        ("on_create", "setup", "init"),
        ("on_start", "deps", "sync"),
        ("on_start", "deps", "gone"),
        ("on_start", "old", "sync"),
        ("unknown", "deps", "sync"),
        ("on_start", "agents", "claude"),
    ]:
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

    with pytest.raises(ValueError, match=r"No.*stage"):
        first_trigger(Autostart())


def test_concurrent_command_refuses_contender_without_clearing_owner(make_cfg, tmp_path, mocker):
    import threading

    from jailbee.autostart_rerun import command

    cfg = rerun_cfg(make_cfg, tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.return_value = ""
    incus.config_get_checked.return_value = None
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    entered = threading.Event()
    release = threading.Event()
    errors = []
    spawns = []

    def spawn(_cfg, _block):
        spawns.append(True)
        entered.set()
        assert release.wait(5)
        incus.config_set("c", "user.jailbee.autostart_in_progress", "12345")

    def invoke():
        try:
            command(
                cfg,
                incus,
                "c",
                stages=["setup"],
                steps=[],
                failed=False,
                trigger=None,
                listing=False,
                json_output=False,
                wait=False,
                spawn=spawn,
            )
        except Exception as exc:
            errors.append(exc)

    owner = threading.Thread(target=invoke)
    owner.start()
    try:
        assert entered.wait(5)
        contender = threading.Thread(target=invoke)
        contender.start()
        contender.join(5)
        assert not contender.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    finally:
        release.set()
        owner.join(5)
    assert len(spawns) == 1
    incus.config_unset.assert_not_called()
    assert incus.config_set.call_args.args[-1] == "12345"


@pytest.mark.parametrize("claim", ["1", "999999999999999999999999", "invalid", "-2", ""])
def test_stale_or_invalid_claim_does_not_block_rerun(make_cfg, tmp_path, mocker, claim):
    from jailbee.autostart_rerun import command

    cfg = rerun_cfg(make_cfg, tmp_path)
    incus = mocker.MagicMock()
    incus.config_get_checked.return_value = claim
    incus.list_containers.return_value = [{"name": "c", "status": "Running"}]
    spawn = mocker.MagicMock()
    command(
        cfg,
        incus,
        "c",
        stages=["setup"],
        steps=[],
        failed=False,
        trigger=None,
        listing=False,
        json_output=False,
        wait=False,
        spawn=spawn,
    )
    spawn.assert_called_once()


@pytest.mark.parametrize("profile", [None, "all"])
@pytest.mark.parametrize("marker", [None, ","])
@pytest.mark.parametrize("fail", [False, True])
def test_rerun_create_attempt_tracking_uses_persisted_marker(
    make_cfg, tmp_path, mocker, profile, marker, fail
):
    from jailbee.autostart_profiles import CREATED_KEY, PROFILE_KEY
    from jailbee.autostart_rerun import run_foreground
    from tests.test_autostart_profiles import state_incus

    cfg = make_cfg(
        tmp_path,
        autostart={
            "on_create": [{"stage": "setup", "steps": [{"name": "init", "run": "true"}]}],
        },
    )
    state = {}
    if profile is not None:
        state[PROFILE_KEY] = profile
    if marker is not None:
        state[CREATED_KEY] = marker
    incus, state = state_incus(mocker, state)
    step, _, _, _ = execution_deps(mocker)
    if fail:
        step.side_effect = RuntimeError("failed")
    block = rerun_block(cfg.autostart, [RerunTarget(AutostartTrigger.ON_CREATE, "setup", None)])
    if fail:
        with pytest.raises(RuntimeError, match="failed"):
            run_foreground(cfg, incus, "c", block)
    else:
        run_foreground(cfg, incus, "c", block)
    assert state.get(CREATED_KEY) == ("setup" if marker is not None else None)
