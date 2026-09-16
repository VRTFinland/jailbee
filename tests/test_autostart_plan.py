"""The pure autostart planner: normalization, agent placement, detach split."""

from __future__ import annotations

from jailbee.autostart_plan import AutostartTrigger, normalize_stages, plan_autostart
from jailbee.config import Autostart, AutostartStep


def _flat(*specs: dict[str, object]) -> Autostart:
    return Autostart.model_validate({"on_create": list(specs)})


def test_flat_list_collapses_consecutive_same_network_into_one_stage():
    block = _flat(
        {"name": "a", "run": "true", "network": "loose"},
        {"name": "b", "run": "true", "network": "loose"},
        {"name": "c", "run": "true"},
    )
    stages = normalize_stages(block.on_create)
    assert [s.stage for s in stages] == ["a", "c"]
    assert stages[0].network == "loose"
    assert [st.name for st in stages[0].all_chains()[0].steps] == ["a", "b"]
    assert stages[1].network is None
    assert [st.name for st in stages[1].all_chains()[0].steps] == ["c"]


def test_flat_list_keeps_per_step_mounts_on_the_step():
    block = _flat({"name": "a", "run": "true", "mounts": ["aws"]})
    stages = normalize_stages(block.on_create)
    assert stages[0].mounts == []
    assert stages[0].all_chains()[0].steps[0].mounts == ["aws"]


def test_flat_list_produces_no_detached_stages():
    block = _flat({"name": "a", "run": "true"})
    plan = plan_autostart(block, AutostartTrigger.ON_CREATE)
    assert [s.stage for s in plan.blocking] == ["a"]
    assert plan.detached == []


def test_detach_splits_at_the_first_marked_stage():
    block = Autostart.model_validate(
        {
            "on_create": [
                {"stage": "schema", "steps": [{"name": "m", "run": "true"}]},
                {"stage": "deps", "detach": True, "steps": [{"name": "d", "run": "true"}]},
                {"stage": "services", "steps": [{"name": "s", "run": "true"}]},
            ]
        }
    )
    plan = plan_autostart(block, AutostartTrigger.ON_CREATE)
    assert [s.stage for s in plan.blocking] == ["schema"]
    assert [s.stage for s in plan.detached] == ["deps", "services"]


def test_agents_stage_is_inserted_at_the_boundary_and_is_blocking():
    block = Autostart.model_validate(
        {
            "on_start": [
                {"stage": "schema", "steps": [{"name": "m", "run": "true"}]},
                {"stage": "deps", "detach": True, "steps": [{"name": "d", "run": "true"}]},
            ]
        }
    )
    agent = AutostartStep(name="claude", run="exec claude", background=True)
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[agent])
    assert [s.stage for s in plan.blocking] == ["schema", "agents"]
    assert [s.stage for s in plan.detached] == ["deps"]


def test_agents_stage_goes_last_when_nothing_detaches():
    """A config with no stages must behave exactly as it does today."""
    block = _flat({"name": "sync", "run": "uv sync"})
    block = Autostart.model_validate({"on_start": block.on_create})
    agent = AutostartStep(name="claude", run="exec claude", background=True)
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[agent])
    assert [s.stage for s in plan.blocking] == ["sync", "agents"]
    assert plan.detached == []


def test_explicit_agents_stage_is_filled_and_not_duplicated():
    block = Autostart.model_validate(
        {
            "on_start": [
                {"stage": "agents"},
                {"stage": "deps", "detach": True, "steps": [{"name": "d", "run": "true"}]},
            ]
        }
    )
    agent = AutostartStep(name="claude", run="exec claude", background=True)
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[agent])
    assert [s.stage for s in plan.blocking] == ["agents"]
    names = [st.name for st in plan.blocking[0].all_chains()[0].steps]
    assert names == ["claude"]


def test_explicit_empty_agents_stage_is_dropped_when_no_agent_autostarts():
    block = Autostart.model_validate({"on_start": [{"stage": "agents"}]})
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[])
    assert plan.blocking == []
    assert plan.detached == []


def test_override_wait_keeps_everything_blocking():
    block = Autostart.model_validate(
        {"on_create": [{"stage": "deps", "detach": True, "steps": [{"name": "d", "run": "true"}]}]}
    )
    plan = plan_autostart(block, AutostartTrigger.ON_CREATE, override="wait")
    assert [s.stage for s in plan.blocking] == ["deps"]
    assert plan.detached == []


def test_override_no_wait_detaches_after_the_first_stage():
    block = Autostart.model_validate(
        {
            "on_create": [
                {"stage": "one", "steps": [{"name": "a", "run": "true"}]},
                {"stage": "two", "steps": [{"name": "b", "run": "true"}]},
            ]
        }
    )
    plan = plan_autostart(block, AutostartTrigger.ON_CREATE, override="no_wait")
    assert [s.stage for s in plan.blocking] == ["one"]
    assert [s.stage for s in plan.detached] == ["two"]


def test_already_detached_puts_everything_including_agents_in_detached():
    block = Autostart.model_validate(
        {"on_start": [{"stage": "deps", "steps": [{"name": "d", "run": "true"}]}]}
    )
    agent = AutostartStep(name="claude", run="exec claude", background=True)
    plan = plan_autostart(
        block, AutostartTrigger.ON_START, agent_steps=[agent], already_detached=True
    )
    assert plan.blocking == []
    assert [s.stage for s in plan.detached] == ["agents", "deps"]


def test_empty_trigger_plans_nothing():
    plan = plan_autostart(Autostart(), AutostartTrigger.ON_CREATE)
    assert plan.blocking == []
    assert plan.detached == []


def test_flat_step_named_agents_is_not_the_reserved_slot():
    """The reserved `agents` slot only exists in the stage form — a flat
    step happening to be named `agents` is just an ordinary step, and the
    generated agent steps still get their own separate stage."""
    block = _flat({"name": "agents", "run": "true"})
    block = Autostart.model_validate({"on_start": block.on_create})
    agent = AutostartStep(name="claude", run="exec claude", background=True)
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[agent])
    assert [s.stage for s in plan.blocking] == ["agents", "agents"]
    assert plan.blocking[0].all_chains()[0].steps[0].run == "true"
    assert [st.name for st in plan.blocking[1].all_chains()[0].steps] == ["claude"]


def test_explicit_agents_stage_with_detach_splits_after_itself():
    block = Autostart.model_validate(
        {
            "on_start": [
                {"stage": "agents", "detach": True},
                {"stage": "deps", "steps": [{"name": "d", "run": "true"}]},
            ]
        }
    )
    agent = AutostartStep(name="claude", run="exec claude", background=True)
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[agent])
    assert [s.stage for s in plan.blocking] == ["agents"]
    assert [s.stage for s in plan.detached] == ["deps"]
    assert plan.blocking[0].detach is True


def test_explicit_agents_stage_keeps_its_network_and_mounts_after_filling():
    block = Autostart.model_validate(
        {"on_start": [{"stage": "agents", "network": "loose", "mounts": ["aws"]}]}
    )
    agent = AutostartStep(name="claude", run="exec claude", background=True)
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[agent])
    assert plan.blocking[0].network == "loose"
    assert plan.blocking[0].mounts == ["aws"]
    assert [st.name for st in plan.blocking[0].all_chains()[0].steps] == ["claude"]


def test_dropped_agents_stage_detach_flag_moves_to_the_next_stage():
    block = Autostart.model_validate(
        {
            "on_start": [
                {"stage": "agents", "detach": True},
                {"stage": "deps", "steps": [{"name": "d", "run": "true"}]},
            ]
        }
    )
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[])
    assert plan.blocking == []
    assert [s.stage for s in plan.detached] == ["deps"]
    assert plan.detached[0].detach is True


def test_explicit_agents_stage_with_already_detached_goes_entirely_to_detached():
    block = Autostart.model_validate(
        {
            "on_start": [
                {"stage": "agents"},
                {"stage": "deps", "steps": [{"name": "d", "run": "true"}]},
            ]
        }
    )
    agent = AutostartStep(name="claude", run="exec claude", background=True)
    plan = plan_autostart(
        block, AutostartTrigger.ON_START, agent_steps=[agent], already_detached=True
    )
    assert plan.blocking == []
    assert [s.stage for s in plan.detached] == ["agents", "deps"]
    assert [st.name for st in plan.detached[0].all_chains()[0].steps] == ["claude"]
