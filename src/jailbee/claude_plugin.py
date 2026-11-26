"""Stage Claude's managed plugin settings outside user configuration."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from jailbee.paths import xdg_data_home

if TYPE_CHECKING:
    from jailbee.config import Config


class DirectorySource(TypedDict):
    source: Literal["directory"]
    path: str


class Marketplace(TypedDict):
    source: DirectorySource


class Permissions(TypedDict):
    defaultMode: Literal["bypassPermissions"]
    deny: list[str]


class ManagedSettings(TypedDict):
    extraKnownMarketplaces: dict[str, Marketplace]
    enabledPlugins: dict[str, bool]
    permissions: NotRequired[Permissions]
    skipDangerousModePermissionPrompt: NotRequired[bool]
    env: NotRequired[dict[str, str]]


def staging_dir(*, unattended: bool = True) -> Path:
    # A repo's opt-out must not rewrite another repo's mounted default policy.
    mode = "unattended" if unattended else "interactive"
    return xdg_data_home() / "jailbee" / "claude-managed-settings" / mode


def stage(cfg: Config) -> None:
    if not cfg.claude.enabled:
        return
    settings: ManagedSettings = {
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
    if cfg.claude.unattended:
        settings["permissions"] = {
            "defaultMode": "bypassPermissions",
            "deny": ["AskUserQuestion", "EnterPlanMode"],
        }
        settings["skipDangerousModePermissionPrompt"] = True
        settings["env"] = {"JAILBEE_CLAUDE_UNATTENDED": "1"}
    content = json.dumps(settings, indent=2) + "\n"
    if json.loads(content) != settings:
        raise ValueError("managed settings JSON did not round-trip")
    directory = staging_dir(unattended=cfg.claude.unattended)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "50-jailbee.json"
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def profile_devices(cfg: Config) -> dict[str, dict[str, str]]:
    if not cfg.claude.enabled:
        return {}
    from jailbee.agent_instructions import _container_path_parts
    from jailbee.config import ConfigError

    devices = {
        "claude-managed-settings": {
            "type": "disk",
            "source": str(staging_dir(unattended=cfg.claude.unattended)),
            "path": "/etc/claude-code/managed-settings.d",
            "readonly": "true",
        }
    }
    from jailbee.agent_instructions import profile_devices as instruction_devices

    target = _container_path_parts(devices["claude-managed-settings"]["path"])
    mounts = [(f"host mount {m.host}", m.container) for m in cfg.effective_host_mounts()]
    mounts += [(f"shared-{m.name}", m.container_path) for m in cfg.effective_shared_caches()]
    for name, path in mounts:
        other = _container_path_parts(path)
        if target[: len(other)] == other or other[: len(target)] == target:
            raise ConfigError(f"claude-managed-settings overlaps {name} at {path!r}; remap it")
    for name, device in instruction_devices(cfg).items():
        other = _container_path_parts(device["path"])
        if other == target[:-1]:
            continue
        if target[: len(other)] == other or other[: len(target)] == target:
            raise ConfigError(f"{name} overlaps Claude managed settings")
    return devices
