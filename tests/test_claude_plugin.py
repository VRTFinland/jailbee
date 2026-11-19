import json
from pathlib import Path

import yaml

from jailbee import claude_plugin
from jailbee.profiles import binds_profile_yaml


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


def test_instruction_mount_contains_empty_dropin_mountpoint(make_cfg, tmp_path, monkeypatch):
    from jailbee import agent_instructions

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = make_cfg(tmp_path / "repo", agents={"claude": {"enabled": True}})
    agent_instructions.sync_global_instructions(cfg)
    mountpoint = agent_instructions.staging_dir("claude", "CLAUDE.md") / "managed-settings.d"
    assert mountpoint.is_dir()
    assert list(mountpoint.iterdir()) == []


def test_disabled_claude_has_no_managed_mount(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "repo", agents={"claude": {"enabled": False}})
    assert "claude-managed-settings" not in yaml.safe_load(binds_profile_yaml(cfg))["devices"]


def test_settings_and_profile_are_independent_of_instructions(make_cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = make_cfg(tmp_path / "repo", agents={"claude": {"enabled": True}})
    cfg._agent_instructions = False
    claude_plugin.stage(cfg)
    settings = json.loads((claude_plugin.staging_dir() / "50-jailbee.json").read_text())
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
        "source": str(claude_plugin.staging_dir()),
        "path": "/etc/claude-code/managed-settings.d",
        "readonly": "true",
    }
