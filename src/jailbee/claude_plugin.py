"""Stage Claude's managed plugin settings outside user configuration."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from jailbee.claude_options import VALUE_OPTIONS
from jailbee.paths import xdg_data_home

if TYPE_CHECKING:
    from jailbee.config import Config


SETTINGS_PATH = "/etc/claude-code/managed-settings.d/50-jailbee.json"


def _first_command(command: str) -> tuple[str, str]:
    """Return original command text and words with expansions masked for scanning.

    Expansion output is only known in the container. Keep each expansion opaque
    so its operators and option-looking text cannot affect the outer command.
    """
    masked: list[str] = []
    quote = ""
    expansions: list[tuple[str, str]] = []
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and quote != "'":
            if not expansions:
                masked.append(command[index : index + 2])
            index += 2
            continue
        if quote and char == quote:
            quote = ""
        elif char in "'\"" and not quote:
            quote = char
        elif quote != "'" and command.startswith("$(", index):
            if not expansions:
                masked.append("JAILBEE_EXPANSION")
            expansions.append((")", quote))
            quote = ""
            index += 2
            continue
        elif quote != "'" and char == "`":
            if expansions and expansions[-1][0] == "`" and not quote:
                _, quote = expansions.pop()
            else:
                if not expansions:
                    masked.append("JAILBEE_EXPANSION")
                expansions.append(("`", quote))
                quote = ""
            index += 1
            continue
        elif expansions and not quote:
            if char == "(":
                expansions.append((")", quote))
            elif char == expansions[-1][0]:
                _, quote = expansions.pop()
                index += 1
                continue
        elif not quote and (
            char in ";&|\n" or (char == "#" and (index == 0 or command[index - 1].isspace()))
        ):
            break
        if not expansions:
            masked.append(char)
        index += 1
    if expansions or quote:
        raise ValueError("unclosed shell quote or expansion")
    return command[:index], "".join(masked)


def with_settings(command: str) -> str:
    """Add settings once to a Claude shell command, checking the mount at launch.

    The host cannot stat a container's mount. An unquoted substitution emits
    exactly two fixed words when present, and no arguments on older containers.
    Keep the rest of the command intact: headless presets contain expansions.
    """
    flag = f"$(test ! -f {SETTINGS_PATH} || printf '%s\\n' --settings {SETTINGS_PATH})"
    try:
        first, masked = _first_command(command)
        words = shlex.split(masked)
    except ValueError:
        return command
    if not words or Path(words[0]).name not in {"claude", "claude-jb"}:
        return command
    if flag in first:
        return command
    skip_value = False
    for word in words[1:]:
        if skip_value:
            skip_value = False
            continue
        if word == "--":
            break
        if word == "--settings" or word.startswith("--settings="):
            return command
        if word in VALUE_OPTIONS:
            skip_value = True
    stream = StringIO(first)
    lexer = shlex.shlex(stream, posix=True)
    lexer.whitespace_split = True
    lexer.get_token()
    offset = stream.tell()
    binary, rest = command[:offset].rstrip(), command[offset:]
    return f"{binary} {flag} {rest}".rstrip()


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
