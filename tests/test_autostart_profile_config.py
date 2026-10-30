"""Profiles reject ambiguous configs and filter before detach planning."""

import pytest
from pydantic import ValidationError

from jailbee.autostart_plan import AutostartTrigger, plan_autostart
from jailbee.config import Autostart


def block(**updates):
    data = {
        "on_create": [{"stage": "deps", "steps": []}],
        "on_start": [
            {"stage": "backend", "detach": True, "steps": []},
            {"stage": "frontend-dev", "steps": []},
        ],
        "profiles": {"full": "all", "frontend": {"stages": ["frontend-dev", "deps"]}},
        "default_profile": "full",
    }
    return Autostart.model_validate(data | updates)


def test_profiles_require_default():
    with pytest.raises(ValidationError, match="default_profile"):
        block(default_profile=None)


def test_default_profile_must_exist():
    with pytest.raises(ValidationError, match="missing"):
        block(default_profile="missing")


def test_profile_unknown_stage_rejected():
    with pytest.raises(ValidationError, match="missing"):
        block(profiles={"full": {"stages": ["missing"]}})


def test_profile_named_all_rejected():
    with pytest.raises(ValidationError, match="all"):
        block(profiles={"all": "all"}, default_profile="all")


def test_profile_empty_stages_rejected():
    with pytest.raises(ValidationError, match="stages"):
        block(profiles={"full": {"stages": []}})


def test_profiles_reject_flat_trigger():
    with pytest.raises(ValidationError, match="on_create"):
        block(on_create=[{"name": "deps", "run": "true"}])


def test_default_profile_without_profiles_rejected():
    with pytest.raises(ValidationError, match="default_profile"):
        block(profiles={})


def test_profile_may_list_agents_without_explicit_stage():
    assert block(profiles={"full": {"stages": ["agents"]}}).default_profile == "full"


def test_select_profile_none_and_all_unchanged():
    from jailbee.autostart_plan import select_profile

    cfg = block()
    assert select_profile(cfg, None) is cfg
    assert select_profile(cfg, "full") is cfg


def test_select_profile_filters_both_triggers_in_config_order():
    from jailbee.autostart_plan import profile_names, select_profile, selected_stage_names

    cfg = block()
    filtered = select_profile(cfg, "frontend")
    assert [s.stage for s in filtered.on_create] == ["deps"]
    assert [s.stage for s in filtered.on_start] == ["frontend-dev"]
    assert selected_stage_names(cfg, "frontend", AutostartTrigger.ON_START) == ("frontend-dev",)
    assert profile_names(cfg) == ("full", "frontend")


def test_select_profile_keeps_agents_stage():
    from jailbee.autostart_plan import select_profile

    cfg = block(on_start=[{"stage": "agents"}, {"stage": "frontend-dev", "steps": []}])
    assert [s.stage for s in select_profile(cfg, "frontend").on_start] == ["agents", "frontend-dev"]


def test_detach_boundary_computed_on_filtered_block():
    from jailbee.autostart_plan import select_profile

    plan = plan_autostart(select_profile(block(), "frontend"), AutostartTrigger.ON_START)
    assert plan.detached == []
    assert [s.stage for s in plan.blocking] == ["frontend-dev"]


def test_diff_autostart_reports_profile_changes():
    from jailbee.branch_config import diff_autostart

    changed = block(default_profile="frontend", profiles={"frontend": {"stages": ["deps"]}})
    diff = diff_autostart(block(), changed)
    assert {c.name for c in diff.block_changes} == {"profiles", "default_profile"}
    assert not diff.widening_steps
