"""The pure autostart planner: normalization, agent placement, detach split."""

from __future__ import annotations

import pytest

from jailbee.autostart_plan import AutostartPlan, AutostartTrigger, normalize_stages, plan_autostart
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
                {"stage": "server", "steps": [{"name": "s", "run": "true"}]},
            ]
        }
    )
    plan = plan_autostart(block, AutostartTrigger.ON_CREATE)
    assert [s.stage for s in plan.blocking] == ["schema"]
    assert [s.stage for s in plan.detached] == ["deps", "server"]


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


# `agents_last=True` is a pending initial prompt: the prompted agent starts
# working on its task, so it must not run before any stage of the trigger.

_AGENT = AutostartStep(name="claude", run="exec claude", background=True)


def _stage(name: str, *, detach: bool = False) -> dict[str, object]:
    return {"stage": name, "detach": detach, "steps": [{"name": name, "run": "true"}]}


def _order(plan: AutostartPlan) -> tuple[list[str], list[str]]:
    return [s.stage for s in plan.blocking], [s.stage for s in plan.detached]


def test_agents_last_without_detach_is_the_final_blocking_stage():
    block = Autostart.model_validate({"on_start": [_stage("db"), _stage("seed")]})
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[_AGENT], agents_last=True)
    assert _order(plan) == (["db", "seed", "agents"], [])


def test_agents_last_runs_after_detached_stages():
    block = Autostart.model_validate({"on_start": [_stage("db"), _stage("seed", detach=True)]})
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[_AGENT], agents_last=True)
    assert _order(plan) == (["db"], ["seed", "agents"])


def test_agents_last_moves_an_explicit_agents_stage_to_the_end():
    block = Autostart.model_validate(
        {"on_start": [{"stage": "agents", "network": "loose"}, _stage("db")]}
    )
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[_AGENT], agents_last=True)
    assert _order(plan) == (["db", "agents"], [])
    assert plan.blocking[1].network == "loose"
    assert [st.name for st in plan.blocking[1].all_chains()[0].steps] == ["claude"]


def test_agents_last_keeps_an_explicit_agents_stages_detach_boundary():
    """`detach: true` on the agents stage marks where detachment begins; with
    the stage moved to the end, the stages that followed it still detach."""
    block = Autostart.model_validate(
        {"on_start": [_stage("db"), {"stage": "agents", "detach": True}, _stage("seed")]}
    )
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[_AGENT], agents_last=True)
    assert _order(plan) == (["db"], ["seed", "agents"])
    assert plan.detached[1].detach is False


def test_agents_last_with_already_detached_is_last_of_the_detached_run():
    block = Autostart.model_validate({"on_start": [_stage("db")]})
    plan = plan_autostart(
        block,
        AutostartTrigger.ON_START,
        agent_steps=[_AGENT],
        agents_last=True,
        already_detached=True,
    )
    assert _order(plan) == ([], ["db", "agents"])


def test_agents_last_with_no_stages_is_blocking():
    plan = plan_autostart(
        Autostart(), AutostartTrigger.ON_START, agent_steps=[_AGENT], agents_last=True
    )
    assert _order(plan) == (["agents"], [])


def test_agents_last_with_no_wait_detaches_everything_before_the_agent():
    block = Autostart.model_validate({"on_start": [_stage("db"), _stage("seed")]})
    plan = plan_autostart(
        block,
        AutostartTrigger.ON_START,
        agent_steps=[_AGENT],
        agents_last=True,
        override="no_wait",
    )
    assert _order(plan) == (["db"], ["seed", "agents"])


def test_services_stage_before_agents():
    block = Autostart.model_validate({"on_start": [{"stage": "a"}, {"stage": "b"}]})
    plan = plan_autostart(
        block,
        AutostartTrigger.ON_START,
        agent_steps=[AutostartStep(name="agent", run="x")],
        services_stage=True,
    )
    assert [s.stage for s in plan.blocking] == ["a", "b", "services", "agents"]
    assert plan.detached == []


def test_services_stage_without_agents_is_last():
    block = Autostart.model_validate({"on_start": [{"stage": "a"}]})
    plan = plan_autostart(block, AutostartTrigger.ON_START, services_stage=True)
    assert [s.stage for s in plan.blocking] == ["a", "services"]
    assert plan.detached == []


def test_services_stage_before_explicit_agents_stage():
    block = Autostart.model_validate(
        {"on_start": [{"stage": "a"}, {"stage": "agents"}, {"stage": "b"}]}
    )
    plan = plan_autostart(
        block,
        AutostartTrigger.ON_START,
        agent_steps=[AutostartStep(name="agent", run="x")],
        services_stage=True,
    )
    assert [s.stage for s in plan.blocking] == ["a", "services", "agents", "b"]


def test_services_stage_follows_agents_into_detached_side():
    block = Autostart.model_validate({"on_start": [{"stage": "a"}, {"stage": "b", "detach": True}]})
    agent = AutostartStep(name="agent", run="x")
    plan = plan_autostart(
        block, AutostartTrigger.ON_START, agent_steps=[agent], services_stage=True
    )
    assert [s.stage for s in plan.blocking] == ["a", "services", "agents"]
    assert [s.stage for s in plan.detached] == ["b"]
    plan = plan_autostart(
        block, AutostartTrigger.ON_START, agent_steps=[agent], services_stage=True, agents_last=True
    )
    assert [s.stage for s in plan.blocking] == ["a"]
    assert [s.stage for s in plan.detached] == ["b", "services", "agents"]


def test_services_stage_ignored_on_create():
    block = Autostart.model_validate({"on_create": [{"stage": "a"}]})
    assert plan_autostart(block, AutostartTrigger.ON_CREATE, services_stage=True) == plan_autostart(
        block, AutostartTrigger.ON_CREATE
    )


def test_services_stage_false_is_unchanged():
    block = Autostart.model_validate({"on_start": [{"stage": "a"}, {"stage": "b", "detach": True}]})
    agent = AutostartStep(name="agent", run="x")
    plan = plan_autostart(
        block, AutostartTrigger.ON_START, agent_steps=[agent], services_stage=False
    )
    assert _order(plan) == (["a", "agents"], ["b"])


@pytest.mark.parametrize(
    "override,detached,expected",
    [
        ("wait", False, (["a", "b", "services", "agents"], [])),
        ("no_wait", False, (["a", "services", "agents"], ["b"])),
        (None, True, ([], ["services", "agents", "a", "b"])),
    ],
)
def test_services_boundary_overrides(override, detached, expected):
    block = Autostart.model_validate({"on_start": [{"stage": "a"}, {"stage": "b", "detach": True}]})
    plan = plan_autostart(
        block,
        AutostartTrigger.ON_START,
        agent_steps=[AutostartStep(name="agent", run="x")],
        services_stage=True,
        override=override,
        already_detached=detached,
    )
    assert _order(plan) == expected


@pytest.mark.parametrize("trigger", ["on_create", "on_start"])
@pytest.mark.parametrize(
    "body",
    [
        {"steps": [{"name": "up", "run": "x"}]},
        {"chains": [{"name": "up", "steps": [{"name": "up", "run": "x"}]}]},
    ],
)
def test_user_stage_named_services_is_accepted(trigger, body):
    block = Autostart.model_validate({trigger: [{"stage": "services", **body}]})
    plan = plan_autostart(block, AutostartTrigger(trigger))
    assert plan.blocking[0].all_chains()[0].steps[0].run == "x"


def test_user_services_stage_and_generated_stage_coexist():
    from jailbee.autostart_plan import GeneratedServicesStage

    block = Autostart.model_validate(
        {
            "on_start": [
                {"stage": "services", "steps": [{"name": "up", "run": "x"}]},
                {"stage": "services"},
            ]
        }
    )
    plan = plan_autostart(block, AutostartTrigger.ON_START, services_stage=True)
    assert [isinstance(s, GeneratedServicesStage) for s in plan.blocking] == [False, True]
    implicit = Autostart.model_validate({"on_start": block.on_start[:1]})
    plan = plan_autostart(implicit, AutostartTrigger.ON_START, services_stage=True)
    assert [isinstance(s, GeneratedServicesStage) for s in plan.blocking] == [False, True]


def test_profiles_do_not_preserve_ordinary_services_stage():
    from jailbee.autostart_plan import select_profile

    block = Autostart.model_validate(
        {
            "on_start": [
                {"stage": "services", "steps": [{"name": "up", "run": "x"}]},
                {"stage": "services"},
            ],
            "profiles": {"p": {"stages": ["agents"]}},
            "default_profile": "p",
        }
    )
    assert len(select_profile(block, "p").on_start) == 1


def test_explicit_services_stage_runs_after_detached_setup():
    from jailbee.autostart_plan import GeneratedServicesStage

    block = Autostart.model_validate(
        {"on_start": [{"stage": "setup", "detach": True}, {"stage": "services"}]}
    )
    plan = plan_autostart(
        block, AutostartTrigger.ON_START, agent_steps=[_AGENT], services_stage=True
    )
    assert _order(plan) == (["agents"], ["setup", "services"])
    assert isinstance(plan.detached[-1], GeneratedServicesStage)


@pytest.mark.parametrize(
    "override, detached, prompted, expected",
    [
        ("wait", False, False, (["setup", "services", "agents"], [])),
        ("no_wait", False, False, (["setup", "agents"], ["services"])),
        (None, True, False, ([], ["agents", "setup", "services"])),
        (None, False, True, ([], ["setup", "services", "agents"])),
    ],
)
def test_explicit_services_slot_respects_boundary_overrides(override, detached, prompted, expected):
    block = Autostart.model_validate(
        {"on_start": [{"stage": "setup", "detach": True}, {"stage": "services"}]}
    )
    plan = plan_autostart(
        block,
        AutostartTrigger.ON_START,
        agent_steps=[_AGENT],
        services_stage=True,
        override=override,
        already_detached=detached,
        agents_last=prompted,
    )
    assert _order(plan) == expected


def test_services_subsets_and_remainder_keep_their_positions():
    from jailbee.autostart_plan import GeneratedServicesStage

    block = Autostart.model_validate(
        {
            "on_start": [
                {"stage": "setup"},
                {"stage": "services", "services": ["db", "web"]},
                {"stage": "build", "detach": True},
                {"stage": "services"},
            ]
        }
    )
    plan = plan_autostart(block, AutostartTrigger.ON_START, services_stage=True)
    assert _order(plan) == (["setup", "services"], ["build", "services"])
    slots = [s for s in [*plan.blocking, *plan.detached] if isinstance(s, GeneratedServicesStage)]
    assert [s.services for s in slots] == [["db", "web"], None]
    assert slots[-1].excluded_services == ["db", "web"]


def test_subset_only_uses_legacy_position_for_unplaced_services():
    from jailbee.autostart_plan import GeneratedServicesStage

    block = Autostart.model_validate(
        {
            "on_start": [
                {"stage": "setup", "detach": True},
                {"stage": "services", "services": ["web"]},
            ]
        }
    )
    plan = plan_autostart(
        block, AutostartTrigger.ON_START, agent_steps=[_AGENT], services_stage=True
    )
    assert _order(plan) == (["services", "agents"], ["setup", "services"])
    assert isinstance(plan.blocking[0], GeneratedServicesStage)
    assert plan.blocking[0].excluded_services == ["web"]


def test_disabled_services_slot_disappears_but_preserves_detach():
    block = Autostart.model_validate(
        {"on_start": [{"stage": "services", "detach": True}, {"stage": "setup"}]}
    )
    plan = plan_autostart(block, AutostartTrigger.ON_START, agent_steps=[_AGENT])
    assert _order(plan) == (["agents"], ["setup"])


def test_flat_services_step_is_not_a_reserved_slot():
    block = Autostart.model_validate({"on_start": [{"name": "services", "run": "true"}]})
    plan = plan_autostart(block, AutostartTrigger.ON_START, services_stage=True)
    assert _order(plan) == (["services", "services"], [])


def test_profiles_preserve_service_placement_like_agents():
    from jailbee.autostart_plan import select_profile

    block = Autostart.model_validate(
        {
            "on_start": [{"stage": "setup"}, {"stage": "services"}, {"stage": "agents"}],
            "profiles": {"p": {"stages": ["agents"]}},
            "default_profile": "p",
        }
    )
    selected = select_profile(block, "p")
    assert [s.stage for s in selected.on_start] == ["services", "agents"]


def _rerun_fixture():
    return Autostart.model_validate({
        "on_create": [
            {"stage": "build", "network": "loose", "mounts": ["aws"], "detach": True,
             "chains": [
                 {"name": "first", "steps": [{"name": "a", "run": "a"}, {"name": "b", "run": "b"}]},
                 {"name": "second", "steps": [{"name": "c", "run": "c"}]},
             ]},
            {"stage": "finish", "steps": [{"name": "z", "run": "z"}]},
        ],
        "profiles": {"p": "all"},
        "default_profile": "p",
    })


def test_rerun_block_whole_stage_keeps_network_and_mounts():
    from jailbee.autostart_plan import RerunTarget, rerun_block
    result = rerun_block(_rerun_fixture(), [RerunTarget(AutostartTrigger.ON_CREATE, "build", None)])
    stage = result.on_create[0]
    assert (stage.stage, stage.network, stage.mounts, stage.detach) == ("build", "loose", ["aws"], False)
    assert result.profiles == {}
    assert result.default_profile is None
    assert [s.name for s in stage.all_chains()[0].steps] == ["a", "b"]


def test_rerun_block_single_step_keeps_only_its_chain():
    from jailbee.autostart_plan import RerunTarget, rerun_block
    result = rerun_block(_rerun_fixture(), [RerunTarget(AutostartTrigger.ON_CREATE, "build", "b")])
    chains = result.on_create[0].all_chains()
    assert [(c.name, [s.name for s in c.steps]) for c in chains] == [("first", ["b"])]


def test_rerun_block_two_steps_two_chains_stay_parallel():
    from jailbee.autostart_plan import RerunTarget, rerun_block
    result = rerun_block(_rerun_fixture(), [RerunTarget(AutostartTrigger.ON_CREATE, "build", s) for s in ("c", "a")])
    assert [(c.name, [s.name for s in c.steps]) for c in result.on_create[0].all_chains()] == [("first", ["a"]), ("second", ["c"])]


def test_rerun_block_preserves_config_order_not_argument_order():
    from jailbee.autostart_plan import RerunTarget, rerun_block
    result = rerun_block(_rerun_fixture(), [RerunTarget(AutostartTrigger.ON_CREATE, s, None) for s in ("finish", "build")])
    assert [s.stage for s in result.on_create] == ["build", "finish"]


def test_resolve_ambiguous_stage_needs_trigger():
    from jailbee.autostart_plan import RerunSelectionError, resolve_rerun
    block = Autostart.model_validate({"on_create": [_stage("build")], "on_start": [_stage("build")]})
    with pytest.raises(RerunSelectionError) as exc:
        resolve_rerun(block, stages=["build"], steps=[], trigger=None)
    assert exc.value.exit_code == 2
    assert "on_create" in str(exc.value) and "on_start" in str(exc.value)


def test_resolve_agents_and_services_refused():
    from jailbee.autostart_plan import RerunSelectionError, resolve_rerun
    block = Autostart.model_validate({"on_start": [{"stage": "agents"}, {"stage": "services"}]})
    for name in ("agents", "services", "services#1"):
        with pytest.raises(RerunSelectionError) as exc:
            resolve_rerun(block, stages=[name], steps=[], trigger=None)
        assert exc.value.exit_code == 1
        assert "not re-runnable" in str(exc.value)


def test_resolve_unknown_step_lists_candidates():
    from jailbee.autostart_plan import RerunSelectionError, resolve_rerun
    with pytest.raises(RerunSelectionError) as exc:
        resolve_rerun(_rerun_fixture(), stages=[], steps=["build/nope"], trigger=None)
    assert exc.value.exit_code == 2
    assert all(name in str(exc.value) for name in ("a", "b", "c"))


def test_legacy_flat_steps_are_addressable():
    from jailbee.autostart_plan import resolve_rerun, rerun_block
    block = _flat({"name": "install", "run": "x"}, {"name": "check", "run": "y"})
    targets = resolve_rerun(block, stages=[], steps=["install/check"], trigger=None)
    result = rerun_block(block, targets)
    assert result.on_create[0].stage == "install"
    assert [s.name for s in result.on_create[0].all_chains()[0].steps] == ["check"]


def test_direct_reserved_targets_never_enter_output():
    from jailbee.autostart_plan import RerunTarget, rerun_block
    block = Autostart.model_validate({"on_start": [{"stage": "agents"}, {"stage": "services"}]})
    targets = [RerunTarget(AutostartTrigger.ON_START, name, None) for name in ("agents", "services")]
    assert rerun_block(block, targets).on_start == []


@pytest.mark.parametrize("value", ["build", "/a", "build/", "build/a/b"])
def test_resolve_malformed_step(value):
    from jailbee.autostart_plan import RerunSelectionError, resolve_rerun
    with pytest.raises(RerunSelectionError) as exc:
        resolve_rerun(_rerun_fixture(), stages=[], steps=[value], trigger=None)
    assert exc.value.exit_code == 2


def test_ordinary_services_stage_remains_rerunnable_alongside_slot():
    from jailbee.autostart_plan import resolve_rerun, rerun_block
    block = Autostart.model_validate({"on_start": [{"stage": "services", "steps": [{"name": "x", "run": "x"}]}, {"stage": "services"}]})
    targets = resolve_rerun(block, stages=["services"], steps=[], trigger=AutostartTrigger.ON_START)
    result = rerun_block(block, targets)
    assert len(result.on_start) == 1
    assert result.on_start[0].all_chains()[0].steps[0].name == "x"
