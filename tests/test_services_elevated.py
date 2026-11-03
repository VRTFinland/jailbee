"""Refresh plan validation and elevation authorization."""

import copy
import json
from datetime import UTC

import pytest

from jailbee.config.models_services import Service
from jailbee.orchestration.grant import ChildRights, Grant, Grantable, RepoCap
from jailbee.services import graph
from jailbee.services.elevated import Plan, RefreshError, host_policy, orchestrator_policy
from tests.conftest import make_config

SID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def refresh_setup(tmp_path, mocker):
    from jailbee.incus import ExecResult
    from jailbee.services import elevated

    cfg, incus = make_config(tmp_path), mocker.Mock()
    values = {
        "user.jailbee.services_approval": json.dumps(
            {"version": 1, "services": {"old": {"network": None, "mounts": []}}}
        )
    }
    incus.config_get_checked.side_effect = lambda _, key: values.get(key)
    incus.config_set.side_effect = lambda _, key, value: values.__setitem__(key, value)
    incus.list_containers.return_value = [{"name": "box", "profiles": ["jb-net-strict"]}]
    incus.config_show.return_value = "devices: {}\n"
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    stage = mocker.patch("jailbee.autostart.run_stage")
    run = mocker.patch(
        "jailbee.container_app.run_in_container", return_value=ExecResult(0, b"", b"")
    )
    pause = mocker.patch.object(
        elevated, "pause_agent", return_value=elevated.PausedAgent("claude", SID, None)
    )
    resume = mocker.patch.object(elevated, "resume_agent", return_value=None)
    step = {
        "name": "setup",
        "run": "true",
        "env": {},
        "working_dir": "",
        "timeout": 3,
        "continue_on_error": False,
    }
    plan = elevated.Plan(
        ("db", "web"),
        ("db", "web"),
        (
            elevated.PlannedService("db", True, (step,), "loose", ()),
            elevated.PlannedService("web", True, (step,), None, ()),
        ),
    )
    return cfg, incus, values, plan, stage, run, pause, resume


def test_refresh_mixed_and_approval_preserved(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, values, plan, stage, run, _, resume = refresh_setup
    report = run_refresh(
        cfg, incus, "box", plan, who="requested by the user on the host", mirror_endpoint=None
    )
    assert report.ok
    assert [(c.args[3].network, c.args[3].mounts) for c in stage.call_args_list] == [
        ("loose", []),
        (None, []),
    ]
    assert run.call_args.args[3][-3:] == ["restart", "db", "web"]
    assert json.loads(values["user.jailbee.services_approval"])["services"] == {
        "old": {"network": None, "mounts": []},
        "db": {"network": "loose", "mounts": []},
        "web": {"network": None, "mounts": []},
    }
    assert "db" in values["user.jailbee.services_prepared"]
    resume.assert_called_once()


def test_refresh_order_prompt_and_prepared_preservation(refresh_setup):
    from jailbee.autostart_profiles import decode_names, encode_names
    from jailbee.services.elevated import run_refresh

    cfg, incus, values, plan, stage, _, pause, resume = refresh_setup
    values["user.jailbee.services_prepared"] = encode_names(["old"])
    events = []
    paused = pause.return_value
    pause.side_effect = lambda *args, **kwargs: (events.append("pause"), paused)[1]
    stage.side_effect = lambda *args, **kwargs: events.append("stage")
    resume.side_effect = lambda *args, **kwargs: events.append("resume")
    report = run_refresh(
        cfg,
        incus,
        "box",
        plan,
        who="requested by orchestrator parent",
        resume_prompt_prefix="preamble\n\n",
        before_resume=lambda: events.append("watch"),
        mirror_endpoint=None,
    )
    assert report.ok
    assert events == ["pause", "stage", "stage", "watch", "resume"]
    assert decode_names(values["user.jailbee.services_prepared"]) == ["old", "db", "web"]
    assert resume.call_args.args[4] == (
        "preamble\n\nYour container's services were refreshed "
        "(requested by orchestrator parent): db: prepared, restarted; web: prepared, restarted. "
        "Continue your task; re-read files you had open if dependencies changed."
    )


def test_refresh_prepare_failure_resumes_and_skips(refresh_setup):
    from jailbee.autostart import AutostartStepError
    from jailbee.services.elevated import run_refresh

    cfg, incus, _, plan, stage, run, _, resume = refresh_setup
    stage.side_effect = AutostartStepError(container="box", step_name="setup", reason="bad")
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert (
        not report.ok
        and "db: prepare failed:" in report.summary()
        and "web: skipped" in report.summary()
    )
    run.assert_not_called()
    resume.assert_called_once()


def test_refresh_unconfirmed_restore_withholds_resume(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, _, plan, stage, run, _, resume = refresh_setup
    incus.config_show.side_effect = ["devices: {}\n", RuntimeError("transport")]
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert not report.ok and "resume withheld: container may still be elevated" in report.summary()
    assert "jb net strict box" in report.summary()
    assert stage.call_count == 1
    run.assert_not_called()
    resume.assert_not_called()


def test_refresh_interrupted_snapshot_withholds_resume(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, _, plan, stage, run, _, resume = refresh_setup
    incus.config_show.side_effect = ["devices: {}\n", KeyboardInterrupt()]
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert not report.ok
    assert "resume withheld: container may still be elevated" in report.summary()
    assert "KeyboardInterrupt" in report.summary() and "jb net strict box" in report.summary()
    assert stage.call_count == 1
    run.assert_not_called()
    resume.assert_not_called()


def test_refresh_existing_envelope_without_network(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, values, plan, _, run, _, _ = refresh_setup
    values["user.jailbee.services_approval"] = json.dumps(
        {"version": 1, "services": {"web": {"mounts": []}}}
    )
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert report.ok
    assert json.loads(values["user.jailbee.services_approval"])["services"]["web"] == {
        "network": None,
        "mounts": [],
    }
    run.assert_called_once()


def test_refresh_invalid_envelope_refuses_before_pause(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, values, plan, stage, _, pause, _ = refresh_setup
    values["user.jailbee.services_approval"] = "invalid"
    with pytest.raises(RefreshError, match="envelope"):
        run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    pause.assert_not_called()
    stage.assert_not_called()


def test_refresh_restart_failure_and_resume_failure(refresh_setup):
    from jailbee.incus import ExecResult
    from jailbee.services.elevated import run_refresh

    cfg, incus, _, plan, _, run, _, resume = refresh_setup
    run.return_value = ExecResult(7, b"", b"bad restart\nmore")
    resume.return_value = "boom"
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert not report.ok
    assert "restart failed: bad restart (exit 7)" in report.summary()
    assert report.agent == "resume failed: boom"


def test_refresh_before_resume_error_withholds_resume(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, _, plan, _, _, _, resume = refresh_setup

    def fail():
        raise RuntimeError("watch failed")

    report = run_refresh(
        cfg, incus, "box", plan, who="host", before_resume=fail, mirror_endpoint=None
    )
    assert (
        not report.ok
        and "resume withheld: could not register agent supervision (watch failed)" in report.agent
    )
    assert "db: prepared, restarted" in report.summary()
    resume.assert_not_called()


def test_refresh_label_failure_resumes(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, _, plan, _, run, _, resume = refresh_setup
    incus.config_set.side_effect = RuntimeError("label error")
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert (
        not report.ok and "label error" in report.summary() and "web: skipped" in report.summary()
    )
    resume.assert_called_once()
    run.assert_not_called()


def test_refresh_cascade_and_no_pause(refresh_setup):
    from jailbee.services.elevated import Plan, PlannedService, run_refresh

    cfg, incus, _, _, stage, _, pause, _ = refresh_setup
    plan = Plan(
        ("db", "web"),
        ("db",),
        (PlannedService("db", False, (), None, ()), PlannedService("web", False, (), None, ())),
    )
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert report.ok and "web: restarted (cascade)" in report.summary()
    stage.assert_not_called()
    pause.assert_not_called()


def test_plain_refresh_output_tail(refresh_setup):
    from jailbee.incus import ExecResult
    from jailbee.services.elevated import run_plain_refresh

    cfg, incus, _, _, _, run, pause, _ = refresh_setup
    assert run_plain_refresh(cfg, incus, "box", ["db"]).ok
    run.return_value = ExecResult(3, b"", b"refused")
    report = run_plain_refresh(cfg, incus, "box", ["db"])
    assert not report.ok and "failed: refused" in report.summary()
    pause.assert_not_called()


def test_refresh_new_mount_not_removed_withholds_resume(refresh_setup):
    from jailbee.services.elevated import Plan, PlannedService, run_refresh

    cfg, incus, _, plan, _, run, _, resume = refresh_setup
    first = plan.services[0]
    plan = Plan(("db",), ("db",), (PlannedService("db", True, first.prepare, None, ("aws",)),))
    incus.config_show.side_effect = ["devices: {}\n", "devices:\n  optional-aws: {type: disk}\n"]
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert not report.ok and "jb unmount aws box" in report.summary()
    resume.assert_not_called()
    run.assert_not_called()


def test_refresh_approval_preserves_existing_service_rights(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, values, plan, _, _, _, _ = refresh_setup
    values["user.jailbee.services_approval"] = json.dumps(
        {"version": 1, "services": {"db": {"network": None, "mounts": ["old"]}}}
    )
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert report.ok
    assert json.loads(values["user.jailbee.services_approval"])["services"]["db"] == {
        "network": "loose",
        "mounts": ["old"],
    }


def test_refresh_interrupt_checks_restoration_before_resume(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, _, plan, stage, run, _, resume = refresh_setup
    stage.side_effect = KeyboardInterrupt()
    incus.config_show.side_effect = ["devices: {}\n", RuntimeError("transport")]
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert not report.ok and "resume withheld" in report.summary()
    run.assert_not_called()
    resume.assert_not_called()


def test_refresh_unexpected_stage_error_resumes(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, _, plan, stage, run, _, resume = refresh_setup
    stage.side_effect = RuntimeError("infrastructure failed")
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert not report.ok and "infrastructure failed" in report.summary()
    run.assert_not_called()
    resume.assert_called_once()


def test_refresh_success_resume_failure_preserves_outcomes(refresh_setup):
    from jailbee.services.elevated import run_refresh

    cfg, incus, _, plan, _, _, _, resume = refresh_setup
    resume.return_value = "boom"
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert not report.ok and report.agent == "resume failed: boom"
    assert "db: prepared, restarted" in report.summary()


def test_refresh_work_network_disagreement_withholds_resume(refresh_setup, mocker):
    from jailbee.services.elevated import run_refresh

    cfg, incus, _, plan, _, run, _, resume = refresh_setup
    incus.list_containers.return_value = [{"name": "box", "profiles": ["jb-net-work-strict"]}]
    mocker.patch(
        "jailbee.work_mode.work_mode_state", side_effect=[("strict", True), ("strict", False)]
    )
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert not report.ok and "marker and ACL disagree" in report.summary()
    resume.assert_not_called()
    run.assert_not_called()


def test_refresh_preexisting_mount_is_preserved(refresh_setup):
    from jailbee.services.elevated import Plan, PlannedService, run_refresh

    cfg, incus, _, plan, _, _, _, resume = refresh_setup
    first = plan.services[0]
    plan = Plan(("db",), ("db",), (PlannedService("db", True, first.prepare, None, ("aws",)),))
    incus.config_show.return_value = "devices:\n  optional-aws: {type: disk}\n"
    report = run_refresh(cfg, incus, "box", plan, who="host", mirror_endpoint=None)
    assert report.ok
    resume.assert_called_once()


def test_pause_no_agent(tmp_path, mocker):
    from datetime import datetime

    from jailbee.services import elevated

    mocker.patch("jailbee.orchestration.agent_watch.probe", return_value=None)
    kill = mocker.patch("jailbee.tmux.kill_window")
    with pytest.raises(
        RefreshError,
        match="could not determine whether an agent is running in box; refresh refused",
    ):
        elevated.pause_agent(make_config(tmp_path), mocker.Mock(), "box", now=datetime.now(UTC))
    kill.assert_not_called()


def test_pause_dead_agent_is_noop(tmp_path, mocker):
    from datetime import datetime

    from jailbee.orchestration.agent_watch import AgentStatus
    from jailbee.services import elevated

    mocker.patch("jailbee.orchestration.agent_watch.probe", return_value=AgentStatus(False))
    kill = mocker.patch("jailbee.tmux.kill_window")
    assert (
        elevated.pause_agent(make_config(tmp_path), mocker.Mock(), "box", now=datetime.now(UTC))
        is None
    )
    kill.assert_not_called()


def test_generated_resume_step_contains_session(tmp_path):
    from jailbee.autostart import agent_autostart_steps

    cfg = make_config(tmp_path, agents={"claude": {"enabled": True}})
    steps = agent_autostart_steps(cfg, initial_prompt=True, resume=SID)
    step = next(s for s in steps if s.name == "claude")
    assert f"--resume {SID}" in step.run


@pytest.fixture
def paused_setup(tmp_path, mocker):
    from datetime import datetime

    from jailbee.orchestration import agent_watch
    from jailbee.services import elevated

    cfg, incus = make_config(tmp_path, agents={"claude": {"enabled": True}}), mocker.Mock()
    incus.config_get_checked.return_value = None
    status = agent_watch.AgentStatus(True, "claude", pid=42, proc_start=77)
    mocker.patch.object(agent_watch, "probe", return_value=status)
    mocker.patch.object(agent_watch, "process_identity_alive", return_value=False)
    current = mocker.patch.object(agent_watch, "current_session", return_value=SID)
    idle = mocker.patch.object(agent_watch, "idle_signal", return_value="idle")
    adapter = mocker.patch("jailbee.accounts.adapters.base.get_adapter").return_value
    adapter.resume_args.return_value = "--resume"
    adapter.session_exists.return_value = True
    kill = mocker.patch("jailbee.tmux.kill_window")
    return cfg, incus, current, idle, adapter, kill, datetime.now(UTC), elevated


def test_pause_records_session_for_restart_validation(paused_setup):
    from jailbee.orchestration import actions, labels

    cfg, incus, _, _, _, kill, now, elevated = paused_setup
    stored = {}
    incus.config_set.side_effect = lambda _, key, value: stored.__setitem__(key, value)
    elevated.pause_agent(cfg, incus, "box", now=now)
    child = type("Child", (), {"config": stored, "short": "box", "full": "box"})()
    ctx = type("Context", (), {"cfg": cfg})()
    assert actions._resolve_resume(ctx, child, SID, "claude") == SID
    kill.assert_called_once()


def test_pause_record_failure_never_kills(paused_setup):
    cfg, incus, _, _, _, kill, now, elevated = paused_setup
    incus.config_set.side_effect = OSError("denied")
    with pytest.raises(RefreshError, match="record agent session"):
        elevated.pause_agent(cfg, incus, "box", now=now)
    kill.assert_not_called()


def test_pause_refuses_when_original_process_does_not_exit(paused_setup, mocker):
    from jailbee.orchestration import agent_watch

    cfg, incus, _, _, _, kill, now, elevated = paused_setup
    mocker.patch.object(elevated, "_pause_wait_seconds", 0.0)
    agent_watch.process_identity_alive.return_value = True
    with pytest.raises(RefreshError, match="exit"):
        elevated.pause_agent(cfg, incus, "box", now=now)
    kill.assert_called_once()


@pytest.mark.parametrize("states,raises", [([None], True), ([True, False], False), ([False], False)])
def test_pause_confirms_original_process_exit(paused_setup, mocker, states, raises):
    from jailbee.orchestration import agent_watch

    cfg, incus, _, _, _, _, now, elevated = paused_setup
    agent_watch.process_identity_alive.side_effect = states
    mocker.patch("jailbee.services.elevated.time.sleep")
    if raises:
        with pytest.raises(RefreshError, match="exit"):
            elevated.pause_agent(cfg, incus, "box", now=now)
    else:
        assert elevated.pause_agent(cfg, incus, "box", now=now).session == SID


def test_pause_kills_window_and_remembers_session(paused_setup):
    cfg, incus, _, _, _, kill, now, elevated = paused_setup
    paused = elevated.pause_agent(cfg, incus, "box", now=now)
    assert paused.session == SID and paused.agent == "claude" and paused.runner is None
    kill.assert_called_once_with(incus, "box", "claude", checked=True)


def test_pause_preserves_runner_and_checked_read_failure(paused_setup):
    from jailbee.incus import IncusError
    from jailbee.orchestration import labels
    from jailbee.orchestration.runner_model import Runner

    cfg, incus, _, _, _, kill, now, elevated = paused_setup
    runner = Runner(agent="claude", model="sonnet")
    incus.config_get_checked.side_effect = lambda _, key: (
        runner.to_label() if key == labels.RUNNER else None
    )
    assert elevated.pause_agent(cfg, incus, "box", now=now).runner == runner
    kill.reset_mock()
    incus.config_get_checked.side_effect = IncusError("transport")
    with pytest.raises(RefreshError, match="transport"):
        elevated.pause_agent(cfg, incus, "box", now=now)
    kill.assert_not_called()


def test_pause_refuses_runner_with_unrewritable_command(paused_setup):
    from jailbee.orchestration import labels
    from jailbee.orchestration.runner_model import Runner

    cfg, incus, _, _, _, kill, now, elevated = paused_setup
    agent = cfg.agents["claude"].model_copy(update={"command": "env claude"})
    cfg = cfg.model_copy(update={"agents": {"claude": agent}})
    runner = Runner(agent="claude-jb", profile="work", model="sonnet")
    incus.config_get_checked.side_effect = lambda _, key: (
        runner.to_label() if key == labels.RUNNER else None
    )
    with pytest.raises(RefreshError, match="runner"):
        elevated.pause_agent(cfg, incus, "box", now=now)
    kill.assert_not_called()


@pytest.mark.parametrize("change", ["different", "missing", "disabled", "empty", "prompt"])
def test_pause_refuses_unusable_relaunch(paused_setup, change):
    cfg, incus, _, _, _, kill, now, elevated = paused_setup
    agent = cfg.agents["claude"]
    agents = dict(cfg.agents)
    if change == "different":
        cfg = cfg.model_copy(update={"primary_agent": "other"})
    elif change == "missing":
        agents.clear()
    elif change == "disabled":
        agents["claude"] = agent.model_copy(update={"enabled": False})
    elif change == "empty":
        agents["claude"] = agent.model_copy(update={"command": " "})
    else:
        agents["claude"] = agent.model_copy(update={"prompt_args": None})
    cfg = cfg.model_copy(update={"agents": agents})
    with pytest.raises(RefreshError):
        elevated.pause_agent(cfg, incus, "box", now=now)
    kill.assert_not_called()


def test_pause_falls_back_to_recorded_history(paused_setup):
    from jailbee.orchestration import labels

    cfg, incus, current, _, _, _, now, elevated = paused_setup
    current.return_value = None
    incus.config_get_checked.side_effect = lambda _, key: (
        json.dumps(["00000000-0000-0000-0000-000000000000", SID])
        if key == labels.AGENT_SESSIONS
        else None
    )
    assert elevated.pause_agent(cfg, incus, "box", now=now).session == SID


def test_pause_refuses_busy_agent(paused_setup):
    cfg, incus, _, idle, _, kill, now, elevated = paused_setup
    idle.return_value = None
    with pytest.raises(RefreshError, match="force"):
        elevated.pause_agent(cfg, incus, "box", now=now)
    kill.assert_not_called()


def test_pause_force_skips_idle_gate(paused_setup):
    cfg, incus, _, idle, _, kill, now, elevated = paused_setup
    idle.return_value = None
    assert elevated.pause_agent(cfg, incus, "box", now=now, force=True).session == SID
    kill.assert_called_once()


@pytest.mark.parametrize("force", [False, True])
def test_pause_unknown_session_refuses_before_kill(paused_setup, force):
    cfg, incus, current, _, _, kill, now, elevated = paused_setup
    current.return_value = None
    with pytest.raises(RefreshError, match="session"):
        elevated.pause_agent(cfg, incus, "box", now=now, force=force)
    kill.assert_not_called()


@pytest.mark.parametrize("failure", ["unsupported", "empty", "missing"])
def test_pause_refuses_unresumable_session(paused_setup, failure):
    cfg, incus, _, _, adapter, kill, now, elevated = paused_setup
    if failure in ("unsupported", "empty"):
        adapter.resume_args.return_value = None if failure == "unsupported" else ""
    else:
        adapter.session_exists.return_value = False
    with pytest.raises(RefreshError, match=r"resum|transcript"):
        elevated.pause_agent(cfg, incus, "box", now=now)
    kill.assert_not_called()


def test_resume_passes_session_and_runner(paused_setup, mocker):
    cfg, incus, _, _, _, _, _, elevated = paused_setup
    relaunch = mocker.patch("jailbee.orchestration.agent_session.relaunch")
    paused = elevated.PausedAgent("claude", SID, None)
    assert elevated.resume_agent(cfg, incus, "box", paused, "prompt") is None
    relaunch.assert_called_once_with(
        cfg, incus, "box", "prompt", runner=None, old_agent="claude", resume=SID
    )
    relaunch.side_effect = RuntimeError("failed")
    assert elevated.resume_agent(cfg, incus, "box", paused, "prompt") == "failed"


def test_foreign_agents_excludes_prompted(paused_setup, mocker):
    from types import SimpleNamespace

    from jailbee import agent_status, procstat
    from jailbee.orchestration.agent_watch import AgentStatus

    cfg, incus, _, _, _, _, _, elevated = paused_setup
    status = AgentStatus(True, "claude", pid=42, proc_start=77)
    incus.list_containers.return_value = [{"name": "box", "state": {"pid": 10}}]
    sessions = [
        SimpleNamespace(pid=42, proc_start=77, agent="claude", updated_at=None),
        SimpleNamespace(pid=43, proc_start=78, agent="claude", updated_at=None),
    ]
    mocker.patch("jailbee.lifecycle.agent_homes", return_value=())
    mocker.patch.object(agent_status, "read_sessions", return_value={"box": sessions})
    mocker.patch.object(procstat, "read_container_pids", return_value=[142, 143])
    mocker.patch.object(
        procstat, "read_process", side_effect=lambda p, **kw: SimpleNamespace(starttime=p - 65)
    )
    mocker.patch.object(procstat, "read_nspid", side_effect=lambda p, **kw: p - 100)
    assert elevated.foreign_agents(cfg, incus, "box", status) == ["claude (pid 43)"]


def test_foreign_agents_rejects_wrong_process_identity(paused_setup, mocker):
    from types import SimpleNamespace

    from jailbee import agent_status, procstat

    cfg, incus, _, _, _, _, _, elevated = paused_setup
    incus.list_containers.return_value = [{"name": "box", "state": {"pid": 10}}]
    session = SimpleNamespace(pid=42, proc_start=77, agent="claude", updated_at=None)
    mocker.patch("jailbee.lifecycle.agent_homes", return_value=())
    mocker.patch.object(agent_status, "read_sessions", return_value={"box": [session]})
    mocker.patch.object(procstat, "read_container_pids", return_value=[100, 101])
    mocker.patch.object(
        procstat,
        "read_process",
        side_effect=lambda p, **kw: SimpleNamespace(starttime=78 if p == 100 else 77),
    )
    mocker.patch.object(procstat, "read_nspid", return_value=43)
    assert elevated.foreign_agents(cfg, incus, "box", None) == []


def test_foreign_agents_refuses_session_discovery_failure(paused_setup, mocker, tmp_path):
    from jailbee.accounts.adapters import base

    cfg, incus, _, _, _, _, _, elevated = paused_setup
    incus.list_containers.return_value = [{"name": "box", "state": {"pid": 10}}]
    mocker.patch("jailbee.lifecycle.agent_homes", return_value=[("box", "broken", tmp_path)])
    adapter = mocker.Mock()
    adapter.read_sessions_strict.side_effect = PermissionError("denied")
    mocker.patch.object(base, "get_adapter", return_value=adapter)
    mocker.patch("jailbee.procstat.read_container_pids", return_value=[123])
    mocker.patch("jailbee.procstat.read_process", return_value=None)
    with pytest.raises(RefreshError, match="denied"):
        elevated.foreign_agents(cfg, incus, "box", None)


def test_foreign_agents_refuses_unreadable_session_directory(paused_setup, mocker, tmp_path):
    from jailbee.accounts.adapters.claude import ClaudeAdapter

    cfg, incus, _, _, _, _, _, elevated = paused_setup
    incus.list_containers.return_value = [{"name": "box", "state": {"pid": 10}}]
    (tmp_path / "sessions").mkdir()
    mocker.patch("jailbee.lifecycle.agent_homes", return_value=[("box", "claude", tmp_path)])
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=ClaudeAdapter())
    mocker.patch("jailbee.accounts.adapters.claude.os.scandir", side_effect=PermissionError("denied"))
    mocker.patch("jailbee.procstat.read_container_pids", return_value=[])
    with pytest.raises(RefreshError, match="denied"):
        elevated.foreign_agents(cfg, incus, "box", None)


def test_foreign_agents_refuses_unreadable_session_file(paused_setup, mocker, tmp_path):
    from jailbee.accounts.adapters.claude import ClaudeAdapter

    cfg, incus, _, _, _, _, _, elevated = paused_setup
    incus.list_containers.return_value = [{"name": "box", "state": {"pid": 10}}]
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "123.json").write_text("{}")
    mocker.patch("jailbee.lifecycle.agent_homes", return_value=[("box", "claude", tmp_path)])
    mocker.patch("jailbee.accounts.adapters.base.get_adapter", return_value=ClaudeAdapter())
    mocker.patch("jailbee.accounts.adapters.claude.os.open", side_effect=PermissionError("denied"))
    mocker.patch("jailbee.procstat.read_container_pids", return_value=[])
    with pytest.raises(RefreshError, match="denied"):
        elevated.foreign_agents(cfg, incus, "box", None)


def test_foreign_agents_refuses_unreadable_processes(paused_setup, mocker):
    cfg, incus, _, _, _, _, _, elevated = paused_setup
    incus.list_containers.return_value = [{"name": "box", "state": {"pid": 10}}]
    mocker.patch("jailbee.procstat.read_container_pids", side_effect=PermissionError("denied"))
    with pytest.raises(RefreshError, match="denied"):
        elevated.foreign_agents(cfg, incus, "box", None)


def payload():
    services = {
        "db": Service(run="db", prepare=[{"run": "init", "network": "loose"}]),
        "api": Service(
            run="api",
            prepare=[
                {"run": "setup", "mounts": ["creds"], "env": {"X": "a b"}, "working_dir": "a b"}
            ],
        ),
        "web": Service(run="web"),
    }
    return graph.plan_payload(services, [], "refresh", step_timeout=42)


def parse(p, named=()):
    p = copy.deepcopy(p)
    if named:
        for service in p["services"]:
            service["will_prepare"] = service["name"] in named and bool(service["prepare"])
    return Plan.from_payload(json.dumps(p), named)


def test_from_payload_roundtrip():
    plan = parse(payload())
    assert plan.named == plan.order
    assert Plan.from_payload(json.dumps(plan.to_json()), plan.named) == plan
    assert plan.services[1].prepare[0]["env"] == {"X": "a b"}
    assert plan.services[1].prepare[0]["working_dir"] == "a b"


def test_elevated_only_when_will_prepare():
    p = payload()
    p["services"][0]["will_prepare"] = False
    plan = parse(p, ["api"])
    assert not plan.services[0].elevated
    assert plan.declared() == ChildRights(mounts=("creds",))


def test_declared_union():
    assert parse(payload()).declared() == ChildRights(loose=True, mounts=("creds",))


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        json.dumps({"version": 2}),
        json.dumps({"version": 1, "op": "refresh", "order": ["../x"], "services": []}),
        '"' + "x" * (300 * 1024) + '"',
        b"\xff",
    ],
)
def test_from_payload_rejects_garbage(raw):
    with pytest.raises(RefreshError):
        Plan.from_payload(raw, [])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(version=True),
        lambda p: p.update(op="restart"),
        lambda p: p.update(order=["db", "db", "web"]),
        lambda p: p["services"][0].update(will_prepare="false"),
        lambda p: p["services"][0]["prepare"][0].update(run=42),
        lambda p: p["services"][0]["prepare"][0].update(extra="x"),
        lambda p: p["services"][0]["prepare"][0].update(env={"X": 2}),
        lambda p: p["services"][0]["elevation"].update(network="strict"),
        lambda p: p["services"][0]["elevation"].update(mounts=[42]),
        lambda p: p["services"][0].update(depends_on=["../x"]),
        lambda p: p["services"][0].update(ready_timeout=False),
        lambda p: p["services"].reverse(),
    ],
)
def test_rejects_malformed_payload(mutate):
    p = copy.deepcopy(payload())
    mutate(p)
    with pytest.raises(RefreshError):
        parse(p)


def dependency_payload(names=()):
    services = {
        "db": Service(run="db", prepare=[{"run": "db-init"}]),
        "api": Service(run="api", prepare=[{"run": "api-init"}], depends_on={"db": {}}),
    }
    return graph.plan_payload(services, names, "refresh", step_timeout=42)


def test_rejects_reversed_dependency_order():
    p = dependency_payload()
    p["order"].reverse()
    p["services"].reverse()
    with pytest.raises(RefreshError):
        parse(p)


@pytest.mark.parametrize("dependency", ["db", "api"])
def test_rejects_cycles_and_self_dependencies(dependency):
    p = dependency_payload()
    p["services"][0]["depends_on"] = [dependency]
    with pytest.raises(RefreshError):
        parse(p)


def test_accepts_omitted_upstream_dependency():
    p = dependency_payload(["api"])
    assert parse(p, ["api"]).order == ("api",)


def test_near_limit_compact_payload_roundtrip():
    p = dependency_payload()
    p["services"][0]["prepare"][0]["run"] = "x" * (256 * 1024 - 2000) + "ä\U0001f600"
    raw = json.dumps(p, separators=(",", ":"), ensure_ascii=False)
    assert len(raw.encode()) < 256 * 1024
    plan = Plan.from_payload(raw, [])
    assert Plan.from_payload(json.dumps(plan.to_json()), plan.named) == plan


def test_rejects_compact_input_whose_roundtrip_exceeds_limit():
    p = dependency_payload()
    p["services"][0]["prepare"][0]["run"] = "ä" * 100000
    raw = json.dumps(p, separators=(",", ":"), ensure_ascii=False)
    assert len(raw.encode()) < 256 * 1024
    with pytest.raises(RefreshError):
        Plan.from_payload(raw, [])


def test_rejects_unknown_named():
    with pytest.raises(RefreshError):
        parse(payload(), ["missing"])


def test_orchestrator_policy_loose_not_grantable(tmp_path):
    assert "loose network mode is not grantable" in orchestrator_policy(
        make_config(tmp_path), Grant(), parse(payload())
    )


def test_orchestrator_policy_mount_outside_cap(tmp_path):
    cfg = make_config(tmp_path)
    grant = Grant(grantable=Grantable(mounts=("creds",)))
    assert "mount 'creds' is not an optional mount of this repo" in orchestrator_policy(
        cfg, grant, parse(payload(), ["api"])
    )


def test_orchestrator_policy_repo_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("jailbee.services.elevated.repo_cap", lambda cfg: RepoCap(mounts=()))
    grant = Grant(grantable=Grantable(mounts=("creds",)))
    assert "mount 'creds' is not grantable" in orchestrator_policy(
        make_config(tmp_path), grant, parse(payload(), ["api"])
    )


def test_host_policy_unknown_mount(tmp_path):
    cfg = make_config(tmp_path)
    assert host_policy(cfg, parse(payload())) == [
        "service 'api' prepare needs optional mount 'creds', which this host does not define"
    ]
    assert host_policy(cfg, parse(payload(), ["db"])) == []
