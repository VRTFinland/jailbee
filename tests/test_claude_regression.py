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
        ("claude-install", "claude-install", "~/.local/share/claude"),
    ]


def test_claude_seed_and_dirs_are_unchanged(tmp_path):
    """`.claude.json` is no longer a file-level bind: the golden image exports
    `CLAUDE_CONFIG_DIR=$HOME/.claude`, so Claude Code's global config lives
    inside the `claude` directory mount and is seeded by
    `init_command._seed_claude_json`, not by a `seed_files` entry."""
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.dir_subpaths == ("claude", "claude-install")
    assert spec.seed_files == ()


def test_claude_egress_is_unchanged(tmp_path):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.egress == CLAUDE_API_HOSTS + CLAUDE_PLUGIN_HOSTS
