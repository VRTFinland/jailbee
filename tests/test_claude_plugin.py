import json
import shlex
from pathlib import Path

import typer
import yaml

from jailbee import claude_plugin
from jailbee.profiles import binds_profile_yaml


def test_unattended_settings_default_and_opt_out(make_cfg, tmp_path, monkeypatch):
    from tests.conftest import with_agent

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    enabled = make_cfg(tmp_path / "repo", agents={"claude": {"enabled": True}})
    claude_plugin.stage(enabled)
    enabled_source = Path(claude_plugin.profile_devices(enabled)["claude-managed-settings"]["source"])
    settings = json.loads((enabled_source / "50-jailbee.json").read_text())
    assert settings["permissions"] == {
        "defaultMode": "bypassPermissions",
        "deny": ["AskUserQuestion", "EnterPlanMode"],
    }
    assert settings["skipDangerousModePermissionPrompt"] is True
    assert settings["env"]["JAILBEE_CLAUDE_UNATTENDED"] == "1"

    disabled = with_agent(enabled, "claude", unattended=False)
    claude_plugin.stage(disabled)
    disabled_source = Path(claude_plugin.profile_devices(disabled)["claude-managed-settings"]["source"])
    old_settings = json.loads((disabled_source / "50-jailbee.json").read_text())
    assert set(old_settings) == {"extraKnownMarketplaces", "enabledPlugins"}
    # Applying another repo's opt-out cannot change this repo's live policy mount.
    assert json.loads((enabled_source / "50-jailbee.json").read_text()) == settings


def test_packaged_plugin_manifests():
    root = Path(claude_plugin.__file__).parent / "claude_marketplace"
    market = json.loads((root / ".claude-plugin/marketplace.json").read_text())
    assert market["name"] == "jailbee"
    assert market["plugins"][0]["name"] == "jailbee"
    plugin = root / market["plugins"][0]["source"]
    assert json.loads((plugin / ".claude-plugin/plugin.json").read_text())["name"] == "jailbee"
    assert json.loads((plugin / "monitors/monitors.json").read_text()) == [
        {
            "name": "inbox",
            "command": "jailbee inbox follow",
            "description": "JailBee inbox",
            "when": "always",
        }
    ]


def test_plugin_commands_resolve_in_container_cli():
    from jailbee.container_cli import container_app

    plugin = Path(claude_plugin.__file__).parent / "claude_marketplace/jailbee"
    hooks = json.loads((plugin / "hooks/hooks.json").read_text())
    assert set(hooks) == {"hooks"}
    assert set(hooks["hooks"]) == {"SessionStart", "SessionEnd", "Stop", "PermissionRequest"}
    commands = []
    for entries in hooks["hooks"].values():
        assert isinstance(entries, list) and entries
        for entry in entries:
            assert set(entry) == {"hooks"}
            assert isinstance(entry["hooks"], list) and entry["hooks"]
            for hook in entry["hooks"]:
                assert set(hook) == {"type", "command"}
                assert hook["type"] == "command"
                commands.append(hook["command"])
    monitors = json.loads((plugin / "monitors/monitors.json").read_text())
    commands.extend(monitor["command"] for monitor in monitors)
    for command in commands:
        executable, *parts = shlex.split(command)
        assert executable == "jailbee"
        node = typer.main.get_command(container_app)
        for part in parts:
            assert isinstance(node, typer.core.TyperGroup), command
            assert part in node.commands, command
            node = node.commands[part]
        assert not isinstance(node, typer.core.TyperGroup), command


def test_instruction_mount_contains_empty_dropin_mountpoint(make_cfg, tmp_path, monkeypatch):
    from jailbee import agent_instructions

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = make_cfg(tmp_path / "repo", agents={"claude": {"enabled": True}})
    agent_instructions.sync_global_instructions(cfg)
    source = agent_instructions.profile_devices(cfg)["agent-instructions-claude"]["source"]
    mountpoint = Path(source) / "managed-settings.d"
    assert mountpoint.is_dir()
    assert list(mountpoint.iterdir()) == []


def test_disabled_claude_has_no_managed_mount(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "repo", agents={"claude": {"enabled": False}})
    assert "claude-managed-settings" not in yaml.safe_load(binds_profile_yaml(cfg))["devices"]


def test_settings_and_profile_are_independent_of_instructions(make_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = make_cfg(tmp_path / "repo", agents={"claude": {"enabled": True, "unattended": False}})
    cfg._agent_instructions = False
    claude_plugin.stage(cfg)
    settings = json.loads((claude_plugin.staging_dir(unattended=False) / "50-jailbee.json").read_text())
    assert settings == {
        "extraKnownMarketplaces": {
            "jailbee": {
                "source": {
                    "source": "directory",
                    "path": "/opt/jailbee/app/current/jailbee/claude_marketplace",
                }
            }
        },
        "enabledPlugins": {"jailbee@jailbee": True},
    }
    devices = yaml.safe_load(binds_profile_yaml(cfg))["devices"]
    assert devices["claude-managed-settings"] == {
        "type": "disk",
        "source": str(claude_plugin.staging_dir(unattended=False)),
        "path": "/etc/claude-code/managed-settings.d",
        "readonly": "true",
    }
