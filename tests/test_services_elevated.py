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


def test_pause_no_agent(tmp_path, mocker):
    from datetime import datetime

    from jailbee.services import elevated

    mocker.patch("jailbee.orchestration.agent_watch.probe", return_value=None)
    kill = mocker.patch("jailbee.tmux.kill_window")
    assert (
        elevated.pause_agent(make_config(tmp_path), mocker.Mock(), "box", now=datetime.now(UTC))
        is None
    )
    kill.assert_not_called()


@pytest.fixture
def paused_setup(tmp_path, mocker):
    from datetime import datetime

    from jailbee.orchestration import agent_watch
    from jailbee.services import elevated

    cfg, incus = make_config(tmp_path), mocker.Mock()
    incus.config_get_checked.return_value = None
    status = agent_watch.AgentStatus(True, "claude", pid=42, proc_start=77)
    mocker.patch.object(agent_watch, "probe", return_value=status)
    current = mocker.patch.object(agent_watch, "current_session", return_value=SID)
    idle = mocker.patch.object(agent_watch, "idle_signal", return_value="idle")
    adapter = mocker.patch("jailbee.accounts.adapters.base.get_adapter").return_value
    adapter.resume_args.return_value = "--resume"
    adapter.session_exists.return_value = True
    kill = mocker.patch("jailbee.tmux.kill_window")
    return cfg, incus, current, idle, adapter, kill, datetime.now(UTC), elevated


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


@pytest.mark.parametrize("failure", ["unsupported", "missing"])
def test_pause_refuses_unresumable_session(paused_setup, failure):
    cfg, incus, _, _, adapter, kill, now, elevated = paused_setup
    if failure == "unsupported":
        adapter.resume_args.return_value = None
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
        procstat, "read_process", side_effect=lambda p: SimpleNamespace(starttime=p - 65)
    )
    mocker.patch.object(procstat, "read_nspid", side_effect=lambda p: p - 100)
    assert elevated.foreign_agents(cfg, incus, "box", status) == ["claude (pid 43)"]


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
