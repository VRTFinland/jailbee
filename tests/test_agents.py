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


def test_spec_is_hashable(tmp_path):
    """`AgentSpec` advertises frozen=True; every field must actually be hashable.

    A dict-typed `env` would type-check as hashable (dataclass(frozen=True)
    auto-generates __hash__) but raise TypeError at runtime — this pins the
    tuple-of-pairs representation against a regression back to dict.
    """
    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert isinstance(hash(spec), int)
