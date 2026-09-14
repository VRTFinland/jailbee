"""Stage/chain autostart models: shape, shorthand and the stage-form bans."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jailbee.autostart import _flat_steps_only
from jailbee.config import Autostart, AutostartChain, AutostartStage, AutostartStep


def test_stage_with_chains_keeps_order():
    stage = AutostartStage.model_validate(
        {
            "stage": "deps",
            "network": "loose",
            "mounts": ["gradle-cache"],
            "detach": True,
            "chains": [
                {"name": "python", "steps": [{"name": "uv-sync", "run": "uv sync"}]},
                {"name": "node", "steps": [{"name": "npm-ci", "run": "npm ci"}]},
            ],
        }
    )
    assert stage.stage == "deps"
    assert stage.network == "loose"
    assert stage.mounts == ["gradle-cache"]
    assert stage.detach is True
    assert [c.name for c in stage.all_chains()] == ["python", "node"]


def test_steps_shorthand_becomes_one_chain_named_main():
    stage = AutostartStage.model_validate(
        {"stage": "schema", "steps": [{"name": "migrate", "run": "./manage.py migrate"}]}
    )
    chains = stage.all_chains()
    assert len(chains) == 1
    assert chains[0].name == "main"
    assert [s.name for s in chains[0].steps] == ["migrate"]


def test_stage_rejects_both_chains_and_steps():
    with pytest.raises(ValidationError, match="chains.*or.*steps"):
        AutostartStage.model_validate(
            {
                "stage": "x",
                "steps": [{"name": "a", "run": "true"}],
                "chains": [{"name": "c", "steps": [{"name": "b", "run": "true"}]}],
            }
        )


def test_step_inside_a_stage_may_not_carry_network():
    with pytest.raises(ValidationError, match="network"):
        AutostartStage.model_validate(
            {"stage": "x", "steps": [{"name": "a", "run": "true", "network": "loose"}]}
        )


def test_step_inside_a_stage_may_not_carry_mounts():
    with pytest.raises(ValidationError, match="mounts"):
        AutostartStage.model_validate(
            {"stage": "x", "steps": [{"name": "a", "run": "true", "mounts": ["aws"]}]}
        )


def test_flat_step_list_still_accepted_with_network_and_mounts():
    block = Autostart.model_validate(
        {"on_create": [{"name": "sync", "run": "uv sync", "network": "loose", "mounts": ["aws"]}]}
    )
    assert block.on_create[0].network == "loose"
    assert block.on_create[0].mounts == ["aws"]


def test_stage_list_accepted_on_a_trigger():
    block = Autostart.model_validate(
        {"on_start": [{"stage": "deps", "steps": [{"name": "a", "run": "true"}]}]}
    )
    assert isinstance(block.on_start[0], AutostartStage)


def test_mixing_flat_steps_and_stages_in_one_trigger_is_rejected():
    with pytest.raises(ValidationError, match="mix"):
        Autostart.model_validate(
            {
                "on_start": [
                    {"name": "a", "run": "true"},
                    {"stage": "deps", "steps": [{"name": "b", "run": "true"}]},
                ]
            }
        )


def test_empty_chain_name_rejected():
    with pytest.raises(ValidationError):
        AutostartChain.model_validate({"name": "", "steps": []})


# `jailbee.autostart._flat_steps_only` is the interim guard between the now
# `AutostartStep | AutostartStage`-unioned `Autostart.on_create`/`on_start`
# fields and the still-flat-only step executor in `jailbee.autostart`. It
# stays here (not in tests/test_autostart.py, the compatibility canary) until
# the stage executor — a later task in this plan — replaces it.


def test_flat_steps_only_passes_a_flat_step_list_through_unchanged():
    steps = [AutostartStep(name="a", run="true"), AutostartStep(name="b", run="true")]
    assert _flat_steps_only(steps) == steps


def test_flat_steps_only_rejects_stage_form():
    stages = [AutostartStage(stage="deps", steps=[AutostartStep(name="a", run="true")])]
    with pytest.raises(NotImplementedError, match="stage-form"):
        _flat_steps_only(stages)
