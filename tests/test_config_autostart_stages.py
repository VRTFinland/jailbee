"""Stage/chain autostart models: shape, shorthand and the stage-form bans."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from jailbee.config import Autostart, AutostartChain, AutostartStage, load_config


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


# ---------- Loader-level validation: uniqueness, agent reservation, the
# reserved `agents` stage. These call `load_config` (not `make_cfg`, which
# never reaches `_build_config_from_dict` in `jailbee.config.loader` — see
# `tests/test_config.py`'s own `_write_repo`-based `ConfigError` tests for
# the same pattern).


def _write_repo(tmp_path, *, name="myrepo", config_yaml="{}"):
    """Create <tmp>/<name>/.git and <tmp>/<name>/.jailbee/config.yaml."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".jailbee").mkdir()
    (repo / ".jailbee" / "config.yaml").write_text(config_yaml)
    return repo


def test_duplicate_step_name_across_chains_in_one_trigger_is_rejected(tmp_path, mocker):
    """Step names are tmux window names, and `run_step` kills an existing
    window of that name before creating its own. Serial execution made a
    collision merely untidy; parallel chains make it fatal."""
    from jailbee.config.errors import ConfigError

    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    config_yaml = yaml.dump(
        {
            "autostart": {
                "on_start": [
                    {
                        "stage": "deps",
                        "chains": [
                            {"name": "a", "steps": [{"name": "build", "run": "true"}]},
                            {"name": "b", "steps": [{"name": "build", "run": "true"}]},
                        ],
                    }
                ]
            }
        }
    )
    repo = _write_repo(tmp_path, config_yaml=config_yaml)
    with pytest.raises(ConfigError, match="duplicate autostart.on_start step name: 'build'"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_duplicate_stage_name_is_rejected(tmp_path, mocker):
    from jailbee.config.errors import ConfigError

    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    config_yaml = yaml.dump(
        {
            "autostart": {
                "on_start": [
                    {"stage": "deps", "steps": [{"name": "a", "run": "true"}]},
                    {"stage": "deps", "steps": [{"name": "b", "run": "true"}]},
                ]
            }
        }
    )
    repo = _write_repo(tmp_path, config_yaml=config_yaml)
    with pytest.raises(ConfigError, match="duplicate autostart.on_start stage name: 'deps'"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_duplicate_chain_name_within_a_stage_is_rejected(tmp_path, mocker):
    """Load-bearing, not cosmetic: `_run_chains_in_parallel` keys both its
    chain table and its cursors by chain name, so two chains sharing one
    would collapse into a single chain and the other's steps would never
    run — silently, with the stage reporting success."""
    from jailbee.config.errors import ConfigError

    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    config_yaml = yaml.dump(
        {
            "autostart": {
                "on_start": [
                    {
                        "stage": "deps",
                        "chains": [
                            {"name": "a", "steps": [{"name": "one", "run": "true"}]},
                            {"name": "a", "steps": [{"name": "two", "run": "true"}]},
                        ],
                    }
                ]
            }
        }
    )
    repo = _write_repo(tmp_path, config_yaml=config_yaml)
    with pytest.raises(ConfigError, match=r"duplicate autostart.on_start\[deps\] chain name: 'a'"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_stage_mounts_validated_against_optional_mounts(tmp_path, make_cfg):
    cfg = make_cfg(
        tmp_path,
        autostart={
            "on_start": [
                {"stage": "deps", "mounts": ["nope"], "steps": [{"name": "a", "run": "true"}]}
            ]
        },
    )
    issues = cfg.validate_runtime()
    assert any("unknown optional_mount: 'nope'" in i for i in issues)


def test_step_name_colliding_with_an_autostarting_agent_is_rejected(tmp_path, mocker):
    """The generated agent step takes the same tmux window name, so a
    user step called `claude` and the agent's own launch window would kill
    each other. docs/agents.md already warns about this; now it is an error."""
    from jailbee.config.errors import ConfigError

    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    config_yaml = yaml.dump(
        {
            "agents": {"claude": {"enabled": True, "autostart": True, "command": "claude"}},
            "autostart": {"on_start": [{"name": "claude", "run": "true"}]},
        }
    )
    repo = _write_repo(tmp_path, config_yaml=config_yaml)
    with pytest.raises(ConfigError, match="reserved by the 'claude' agent"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_step_name_may_match_an_agent_that_does_not_autostart(tmp_path, mocker):
    """No generated step, no window, no collision."""
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    config_yaml = yaml.dump(
        {
            "agents": {"claude": {"enabled": True, "autostart": False, "command": "claude"}},
            "autostart": {"on_start": [{"name": "claude", "run": "true"}]},
        }
    )
    repo = _write_repo(tmp_path, config_yaml=config_yaml)
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.autostart.on_start[0].name == "claude"


def test_step_level_network_is_deprecated_but_still_valid(tmp_path, make_cfg):
    """The notice lives in `deprecation_notices`, not `validate_runtime`.

    Both are printed by `jailbee config validate`, but only the latter is
    read by `branch_config.load_branch_autostart` as "this config does not
    fit this host" — and a deprecated spelling fits fine (see
    `tests/test_branch_config.py::test_a_branch_using_the_legacy_per_step_network_is_accepted`).
    """
    cfg = make_cfg(
        tmp_path,
        autostart={"on_start": [{"name": "a", "run": "true", "network": "loose"}]},
    )
    assert any("deprecated" in n and "network" in n for n in cfg.deprecation_notices())
    assert not any("deprecated" in i for i in cfg.validate_runtime())
    # Deprecated, not broken: the value is untouched.
    assert cfg.autostart.on_start[0].network == "loose"


def test_agents_stage_with_own_steps_is_rejected(tmp_path, mocker):
    """The reserved `agents` stage is filled in by the planner from the
    `agents` config; steps the user writes there would be silently
    overwritten (or dropped entirely, on a trigger with no autostarting
    agent) — reject it at load time instead."""
    from jailbee.config.errors import ConfigError

    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    config_yaml = yaml.dump(
        {
            "autostart": {
                "on_start": [
                    {"stage": "agents", "steps": [{"name": "a", "run": "true"}]},
                ]
            }
        }
    )
    repo = _write_repo(tmp_path, config_yaml=config_yaml)
    with pytest.raises(ConfigError, match="stage 'agents'.*generated from the `agents` config"):
        load_config(repo / ".jailbee" / "config.yaml")


def test_empty_agents_stage_loads_fine(tmp_path, mocker):
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    config_yaml = yaml.dump(
        {
            "autostart": {
                "on_start": [
                    {"stage": "agents"},
                ]
            }
        }
    )
    repo = _write_repo(tmp_path, config_yaml=config_yaml)
    cfg = load_config(repo / ".jailbee" / "config.yaml")
    assert cfg.autostart.on_start[0].stage == "agents"
