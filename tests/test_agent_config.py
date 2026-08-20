import pytest

from jailbee.config import (
    AgentConfig,
    ClaudeAgentConfig,
    ConfigError,
    device_name,
    resolve_agents_raw,
)
from tests.conftest import make_cfg


def test_device_name_maps_dots_to_dashes():
    assert device_name("claude") == "claude"
    assert device_name("claude.json") == "claude-json"
    assert device_name("claude-install") == "claude-install"


def test_preset_supplies_command_and_install():
    out = resolve_agents_raw({"agents": {"codex": {"enabled": True}}})
    codex = out["agents"]["codex"]
    assert codex["command"] == "codex"
    assert codex["install"] == "npm i -g @openai/codex"


def test_user_scalar_overrides_preset():
    out = resolve_agents_raw({"agents": {"codex": {"enabled": True, "install": "my-installer"}}})
    assert out["agents"]["codex"]["install"] == "my-installer"


def test_user_egress_appends_to_preset():
    out = resolve_agents_raw(
        {"agents": {"codex": {"enabled": True, "egress_allow": ["extra.host:443"]}}}
    )
    hosts = out["agents"]["codex"]["egress_allow"]
    assert "api.openai.com:443" in hosts
    assert hosts[-1] == "extra.host:443"


def test_empty_egress_list_resets_preset():
    out = resolve_agents_raw({"agents": {"codex": {"egress_allow": []}}})
    assert out["agents"]["codex"]["egress_allow"] == []


def test_unknown_agent_name_gets_no_preset_base():
    out = resolve_agents_raw({"agents": {"mine": {"command": "mine"}}})
    assert out["agents"]["mine"] == {"command": "mine"}


def test_legacy_claude_block_is_translated():
    out = resolve_agents_raw({"claude": {"enabled": True, "autostart": True}})
    assert "claude" not in out
    claude = out["agents"]["claude"]
    assert claude["enabled"] is True
    assert claude["autostart"] is True
    assert claude["command"] == "claude"


def test_both_claude_spellings_is_an_error():
    with pytest.raises(ConfigError, match=r"both `claude:` and `agents.claude`"):
        resolve_agents_raw({"claude": {"enabled": True}, "agents": {"claude": {"enabled": True}}})


def test_claude_entry_accepts_claude_only_fields():
    cfg = ClaudeAgentConfig.model_validate({"enabled": True, "ai_pr_timeout": 900})
    assert cfg.ai_pr_timeout == 900


def test_generic_agent_rejects_claude_only_fields():
    with pytest.raises(ValueError):
        AgentConfig.model_validate({"enabled": True, "ai_pr_timeout": 900})


def test_install_check_defaults_from_command():
    cfg = AgentConfig.model_validate({"enabled": True, "command": "codex --yolo"})
    assert cfg.effective_install_check() == "command -v codex"


# ---------- Step 7: conflict/uniqueness tests (validate_runtime) ----------


def test_identical_shared_mount_across_agents_is_allowed(tmp_path):
    cfg = make_cfg(
        tmp_path,
        agents={
            "a": {
                "enabled": True,
                "command": "a",
                "shared": [{"subpath": "npm-global", "path": "~/.npm-global"}],
            },
            "b": {
                "enabled": True,
                "command": "b",
                "shared": [{"subpath": "npm-global", "path": "~/.npm-global"}],
            },
        },
    )
    assert cfg.validate_runtime() == []


def test_conflicting_shared_mount_across_agents_is_reported(tmp_path):
    cfg = make_cfg(
        tmp_path,
        agents={
            "a": {
                "enabled": True,
                "command": "a",
                "shared": [{"subpath": "shared", "path": "~/.a"}],
            },
            "b": {
                "enabled": True,
                "command": "b",
                "shared": [{"subpath": "shared", "path": "~/.b"}],
            },
        },
    )
    assert any("claimed twice" in i for i in cfg.validate_runtime())


def test_autostart_requires_enabled(tmp_path):
    cfg = make_cfg(tmp_path, agents={"a": {"autostart": True, "command": "a"}})
    issues = cfg.validate_runtime()
    assert any("requires agents.a.enabled" in i for i in issues)


def test_bad_agent_name_is_an_error(tmp_path):
    cfg = make_cfg(tmp_path, agents={"Not_Valid": {"enabled": True, "command": "x"}})
    issues = cfg.validate_runtime()
    assert any("must match [a-z0-9-]+" in i for i in issues)


# ---------- Task 2: Config.claude derived from agents.claude ----------


def test_claude_property_reflects_agents_entry(tmp_path):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True, "ai_pr_timeout": 900}})
    assert cfg.claude.enabled is True
    assert cfg.claude.ai_pr_timeout == 900


def test_claude_property_defaults_disabled_when_absent(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.claude.enabled is False
    assert cfg.claude.command == "claude"


def test_with_agent_helper_actually_changes_the_config(tmp_path):
    """Guards the silent-failure mode: a property shadows model_copy's dict."""
    from tests.conftest import with_agent

    cfg = with_agent(make_cfg(tmp_path), "claude", enabled=True)
    assert cfg.claude.enabled is True
