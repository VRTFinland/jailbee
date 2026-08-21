"""Golden values proving the agents refactor did not move Claude.

The device names in particular are live Incus disk-device names in every
existing container's binds profile; a rename would detach the real mounts.
"""

from jailbee.agents import enabled_agent_specs
from jailbee.constants import CLAUDE_API_HOSTS, CLAUDE_PLUGIN_HOSTS
from tests.conftest import make_cfg


def test_claude_mounts_are_unchanged(tmp_path):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert [(c.name, c.host_subpath, c.container_path) for c in spec.shared] == [
        ("claude", "claude", "~/.claude"),
        ("claude-json", "claude.json", "~/.claude.json"),
        ("claude-install", "claude-install", "~/.local/share/claude"),
    ]


def test_claude_seed_and_dirs_are_unchanged(tmp_path):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.dir_subpaths == ("claude", "claude-install")
    assert spec.seed_files == (("claude.json", "{}\n"),)


def test_claude_egress_is_unchanged(tmp_path):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.egress == CLAUDE_API_HOSTS + CLAUDE_PLUGIN_HOSTS
