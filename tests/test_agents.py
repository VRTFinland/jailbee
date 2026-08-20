from jailbee.agents import enabled_agent_specs
from tests.conftest import make_cfg, with_agent


def test_disabled_agents_are_excluded(tmp_path):
    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": False}})
    assert enabled_agent_specs(cfg) == []


def test_spec_carries_mounts_egress_and_install(tmp_path):
    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.name == "codex"
    assert [c.name for c in spec.shared] == ["codex"]
    assert [c.container_path for c in spec.shared] == ["~/.codex"]
    assert spec.egress == ("api.openai.com:443",)
    assert spec.install == "npm i -g @openai/codex"
    assert spec.install_check == "command -v codex"
    assert spec.install_network == "strict"


def test_file_mount_becomes_a_seed_file_not_a_dir(tmp_path):
    cfg = make_cfg(tmp_path, agents={"aider": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.dir_subpaths == ()
    assert spec.seed_files == (("aider.conf.yml", ""),)


def test_claude_sorts_last(tmp_path):
    cfg = make_cfg(
        tmp_path,
        agents={
            "claude": {"enabled": True},
            "codex": {"enabled": True},
            "aider": {"enabled": True},
        },
    )
    assert [s.name for s in enabled_agent_specs(cfg)] == ["aider", "codex", "claude"]


def test_plugin_hosts_only_when_claude_plugins_enabled(tmp_path):
    from jailbee.constants import CLAUDE_PLUGIN_HOSTS

    on = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    (spec,) = enabled_agent_specs(on)
    assert CLAUDE_PLUGIN_HOSTS[0] in spec.egress

    off = with_agent(on, "claude", plugins_enabled=False)
    (spec_off,) = enabled_agent_specs(off)
    assert CLAUDE_PLUGIN_HOSTS[0] not in spec_off.egress


def test_grok_install_runs_loose(tmp_path):
    cfg = make_cfg(tmp_path, agents={"grok": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.install_network == "loose"


def test_spec_autostart_reflects_config(tmp_path):
    """Differential in both directions: catches a hardcoded True or False."""
    autostart_on = make_cfg(tmp_path, agents={"codex": {"enabled": True, "autostart": True}})
    (spec_on,) = enabled_agent_specs(autostart_on)
    assert spec_on.autostart is True

    autostart_off = make_cfg(tmp_path, agents={"codex": {"enabled": True, "autostart": False}})
    (spec_off,) = enabled_agent_specs(autostart_off)
    assert spec_off.autostart is False


def test_env_is_an_immutable_tuple_of_pairs(tmp_path):
    """Pins `env`'s shape against a regression back to a mutable dict.

    `AgentSpec` is `frozen=True`; a dict-typed `env` would silently defeat
    that guarantee via `spec.env["k"] = "v"`. A tuple has no `__setitem__`,
    so that specific escape hatch is closed — assert both the exact value
    and the absence of in-place mutation.
    """
    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": True, "env": {"FOO": "bar"}}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.env == (("FOO", "bar"),)
    assert not hasattr(spec.env, "__setitem__")
