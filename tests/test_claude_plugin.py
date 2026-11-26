import json
import shlex
import subprocess
from pathlib import Path

import pytest
import typer
import yaml

from jailbee import claude_plugin
from jailbee.profiles import binds_profile_yaml


@pytest.mark.parametrize("binary", ["claude", "claude-jb", "/opt/bin/claude"])
def test_launch_settings_reach_binary_once(binary, tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    command = claude_plugin.with_settings(f"{binary} --model opus")
    assert claude_plugin.with_settings(command) == command
    stub = tmp_path / "record"
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    command = command.replace(claude_plugin.SETTINGS_PATH, str(settings))
    command = command.replace(binary, str(stub), 1)
    proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=True)
    assert proc.stdout.splitlines() == ["--settings", str(settings), "--model", "opus"]


def test_launch_settings_missing_file_keeps_argv(tmp_path):
    binary = tmp_path / "claude"
    binary.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
    binary.chmod(0o755)
    command = claude_plugin.with_settings(f"{binary} --model opus")
    command = command.replace(claude_plugin.SETTINGS_PATH, str(tmp_path / "missing.json"))
    proc = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=True)
    assert proc.stdout.splitlines() == ["--model", "opus"]


@pytest.mark.parametrize(
    "command",
    [
        "claude --settings custom.json",
        "claude --settings=custom.json",
        "codex",
        "env X=1 claude",
        "claude --system-prompt 'unterminated",
        'claude --model "unterminated',
    ],
)
def test_launch_settings_preserve_explicit_settings_and_other_binaries(command):
    assert claude_plugin.with_settings(command) == command


@pytest.mark.parametrize("separator", [";", "&&", "||", "|"])
@pytest.mark.parametrize("spacing", ["", " "])
@pytest.mark.parametrize("options", ["", " --model opus"])
def test_later_command_settings_do_not_suppress_claude_settings(
    tmp_path, separator, spacing, options
):
    binary = tmp_path / "claude"
    binary.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$RECORD"\n')
    binary.chmod(0o755)
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    record = tmp_path / "argv"
    original = f'{binary}{options}{spacing}{separator}{spacing}printf "%s" --settings=other'
    command = claude_plugin.with_settings(original)
    command = command.replace(claude_plugin.SETTINGS_PATH, str(settings))
    subprocess.run(
        ["bash", "-c", command],
        env={"RECORD": str(record)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert record.read_text().splitlines() == ["--settings", str(settings), *shlex.split(options)]


@pytest.mark.parametrize("separator", [";", "&&", "||", "|"])
def test_quoted_command_separator_does_not_hide_explicit_settings(separator):
    command = f"claude --system-prompt '{separator}' --settings custom.json"
    assert claude_plugin.with_settings(command) == command


@pytest.mark.parametrize(
    "option",
    [
        "--system-prompt",
        "--append-system-prompt",
        "--json-schema",
        "--mcp-config",
        "--add-dir",
        "--allowedTools",
        "--tools",
        "--plugin-dir",
        "--permission-prompt-tool",
    ],
)
def test_option_value_named_settings_does_not_suppress_jailbee_settings(option):
    command = claude_plugin.with_settings(f"claude {option} --settings=example")
    assert command.count("--settings") == 2
    assert command.endswith(f"{option} --settings=example")


def test_settings_path_is_in_mounted_dropin(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    mount = claude_plugin.profile_devices(cfg)["claude-managed-settings"]["path"]
    assert claude_plugin.SETTINGS_PATH == f"{mount}/50-jailbee.json"


@pytest.mark.parametrize("runner", ["claude", "claude-jb"])
def test_autostart_delivers_settings_after_runner_rewrite(make_cfg, tmp_path, runner):
    from jailbee.autostart import agent_autostart_steps
    from jailbee.orchestration.runner import Runner

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True, "autostart": True}})
    steps = agent_autostart_steps(cfg, runner=Runner(agent=runner))
    [step] = steps
    assert "--settings" in step.run
    assert step.run.startswith(f"exec {runner} ")


def test_disabled_and_non_claude_autostart_do_not_get_settings(make_cfg, tmp_path):
    from jailbee.autostart import agent_autostart_steps

    cfg = make_cfg(
        tmp_path,
        agents={"claude": {"enabled": False}, "codex": {"enabled": True, "autostart": True}},
    )
    [step] = agent_autostart_steps(cfg)
    assert step.run == "exec codex"


def test_unattended_settings_default_and_opt_out(make_cfg, tmp_path, monkeypatch):
    from tests.conftest import with_agent

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    enabled = make_cfg(tmp_path / "repo", agents={"claude": {"enabled": True}})
    claude_plugin.stage(enabled)
    enabled_source = Path(
        claude_plugin.profile_devices(enabled)["claude-managed-settings"]["source"]
    )
    settings = json.loads((enabled_source / "50-jailbee.json").read_text())
    assert settings["permissions"] == {
        "defaultMode": "bypassPermissions",
        "deny": ["AskUserQuestion", "EnterPlanMode"],
    }
    assert settings["skipDangerousModePermissionPrompt"] is True
    assert settings["env"]["JAILBEE_CLAUDE_UNATTENDED"] == "1"

    disabled = with_agent(enabled, "claude", unattended=False)
    claude_plugin.stage(disabled)
    disabled_source = Path(
        claude_plugin.profile_devices(disabled)["claude-managed-settings"]["source"]
    )
    old_settings = json.loads((disabled_source / "50-jailbee.json").read_text())
    assert set(old_settings) == {"extraKnownMarketplaces", "enabledPlugins"}
    # Applying another repo's opt-out cannot change this repo's live policy mount.
    assert json.loads((enabled_source / "50-jailbee.json").read_text()) == settings


def test_unattended_permissions_survive_instruction_opt_out(make_cfg, tmp_path, monkeypatch):
    from jailbee import agent_instructions
    from tests.conftest import with_agent

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = make_cfg(tmp_path / "repo", agents={"claude": {"enabled": True}})
    cfg._agent_instructions = False
    no_memory = with_agent(cfg, "claude", global_instructions=None)
    no_memory._agent_instructions = True
    for opted_out in (no_memory, cfg):
        claude_plugin.stage(opted_out)
        devices = yaml.safe_load(binds_profile_yaml(opted_out))["devices"]
        assert "agent-instructions-claude" not in devices
        source = Path(devices["claude-managed-settings"]["source"])
        settings = json.loads((source / "50-jailbee.json").read_text())
        assert settings["permissions"]["defaultMode"] == "bypassPermissions"
        assert settings["env"]["JAILBEE_CLAUDE_UNATTENDED"] == "1"
        agent_instructions.sync_global_instructions(opted_out)
        assert not (tmp_path / "data/jailbee/agent-instructions").exists()


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
    settings = json.loads(
        (claude_plugin.staging_dir(unattended=False) / "50-jailbee.json").read_text()
    )
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
