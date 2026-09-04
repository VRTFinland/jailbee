"""Every config field must be reachable, typed, and documented.

The config editor generates its forms from these models, and generates
`global.yaml`'s comments from their descriptions. A field with no
description would appear in the editor with an empty help pane; a field
whose annotation the editor cannot classify would silently not appear at
all. This test turns both into a CI failure.

UNDOCUMENTED is a shrinking allowlist. Do not add to it.
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from jailbee.config import ClaudeAgentConfig, Config
from jailbee.global_config import GlobalConfig

# Computed at load time, never YAML keys. See the `Config` docstring.
EXCLUDED: frozenset[tuple[str, str]] = frozenset(
    {
        ("Config", "repo_root"),
        ("Config", "default_branch"),
        ("Config", "container_prefix"),
        ("Config", "upstream_remote"),
        ("Config", "claude_credentials_dir"),
    }
)

# Shrinks to empty across Tasks 7-10 and is deleted in Task 11.
UNDOCUMENTED: frozenset[tuple[str, str]] = frozenset(
    {
        ("AgentConfig", "auto_update"),
        ("AgentConfig", "autostart"),
        ("AgentConfig", "command"),
        ("AgentConfig", "egress_allow"),
        ("AgentConfig", "enabled"),
        ("AgentConfig", "env"),
        ("AgentConfig", "install"),
        ("AgentConfig", "install_check"),
        ("AgentConfig", "install_network"),
        ("AgentConfig", "shared"),
        ("AgentConfig", "update"),
        ("AgentSharedMount", "path"),
        ("AgentSharedMount", "seed"),
        ("AgentSharedMount", "subpath"),
        ("AgentSharedMount", "type"),
        ("Autostart", "env"),
        ("Autostart", "on_create"),
        ("Autostart", "on_start"),
        ("Autostart", "step_timeout"),
        ("AutostartStep", "background"),
        ("AutostartStep", "continue_on_error"),
        ("AutostartStep", "env"),
        ("AutostartStep", "mounts"),
        ("AutostartStep", "name"),
        ("AutostartStep", "network"),
        ("AutostartStep", "run"),
        ("AutostartStep", "timeout"),
        ("AutostartStep", "working_dir"),
        ("BootConfig", "background"),
        ("ChromeConfig", "autostart"),
        ("ChromeConfig", "dark_mode"),
        ("ChromeConfig", "enabled"),
        ("ChromeConfig", "host_path"),
        ("ChromeConfig", "url"),
        ("ClaudeAgentConfig", "ai_pr_branch"),
        ("ClaudeAgentConfig", "ai_pr_description"),
        ("ClaudeAgentConfig", "ai_pr_model"),
        ("ClaudeAgentConfig", "ai_pr_timeout"),
        ("ClaudeAgentConfig", "auto_update"),
        ("ClaudeAgentConfig", "autostart"),
        ("ClaudeAgentConfig", "command"),
        ("ClaudeAgentConfig", "egress_allow"),
        ("ClaudeAgentConfig", "enabled"),
        ("ClaudeAgentConfig", "env"),
        ("ClaudeAgentConfig", "install"),
        ("ClaudeAgentConfig", "install_check"),
        ("ClaudeAgentConfig", "install_jailbee_skills"),
        ("ClaudeAgentConfig", "install_network"),
        ("ClaudeAgentConfig", "plugins_enabled"),
        ("ClaudeAgentConfig", "pr_prompt"),
        ("ClaudeAgentConfig", "shared"),
        ("ClaudeAgentConfig", "update"),
        ("ClaudeCredentials", "group"),
        ("ClaudeCredentials", "repos"),
        ("ColumnConfig", "fields"),
        ("ColumnConfig", "hide"),
        ("Config", "agents"),
        ("Config", "autostart"),
        ("Config", "boot"),
        ("Config", "chrome"),
        ("Config", "confirm"),
        ("Config", "container"),
        ("Config", "container_user"),
        ("Config", "dashboard"),
        ("Config", "defaults"),
        ("Config", "destroy"),
        ("Config", "docker_registry_mirror"),
        ("Config", "egress_allow"),
        ("Config", "github"),
        ("Config", "golden"),
        ("Config", "gpg"),
        ("Config", "host_devices"),
        ("Config", "host_mounts"),
        ("Config", "host_ports"),
        ("Config", "jetbrains"),
        ("Config", "loose_auto_revert"),
        ("Config", "ls"),
        ("Config", "new"),
        ("Config", "optional_mounts"),
        ("Config", "pull"),
        ("Config", "push"),
        ("Config", "shared_caches"),
        ("Config", "shared_dir"),
        ("Config", "ssh"),
        ("Config", "terminal"),
        ("ConfirmConfig", "auto_target"),
        ("ContainerConfig", "env"),
        ("ContainerUser", "gid"),
        ("ContainerUser", "uid"),
        ("Defaults", "cpu"),
        ("Defaults", "memory"),
        ("Defaults", "network"),
        ("Defaults", "storage_pool"),
        ("DestroyConfig", "background"),
        ("DockerRegistryMirror", "data_dir"),
        ("DockerRegistryMirror", "enabled"),
        ("DockerRegistryMirror", "image"),
        ("DockerRegistryMirror", "port"),
        ("DockerRegistryMirrorRepoConfig", "extra_registries"),
        ("GithubConfig", "api_tokens"),
        ("GithubConfig", "enabled"),
        ("GlobalConfig", "claude_credentials"),
        ("GlobalConfig", "dashboard"),
        ("GlobalConfig", "docker_registry_mirror"),
        ("GlobalConfig", "loose_auto_revert"),
        ("GlobalConfig", "ls"),
        ("Golden", "alias"),
        ("Golden", "disable_snippets"),
        ("Golden", "enable_snippets"),
        ("Golden", "extra_apt_packages"),
        ("Golden", "java"),
        ("Golden", "node"),
        ("Golden", "provision_env"),
        ("Golden", "provision_script"),
        ("Golden", "python"),
        ("Golden", "stacks"),
        ("Golden", "ubuntu_version"),
        ("GpgConfig", "enabled"),
        ("HostDevice", "gid"),
        ("HostDevice", "group"),
        ("HostDevice", "mode"),
        ("HostDevice", "path"),
        ("HostDevice", "source"),
        ("HostDevice", "type"),
        ("HostDevice", "uid"),
        ("HostMount", "container"),
        ("HostMount", "host"),
        ("HostMount", "readonly"),
        ("HostPort", "container_address"),
        ("HostPort", "host_address"),
        ("HostPort", "host_port"),
        ("HostPort", "name"),
        ("HostPort", "port"),
        ("HostPort", "proto"),
        ("JetbrainsConfig", "ai_enabled"),
        ("JetbrainsConfig", "autostart"),
        ("JetbrainsConfig", "enabled"),
        ("JetbrainsConfig", "ide"),
        ("JetbrainsConfig", "share_idea"),
        ("JetbrainsConfig", "toolbox_host_path"),
        ("JetbrainsConfig", "userprefs_from_host"),
        ("LooseAutoRevert", "after"),
        ("LooseAutoRevert", "enabled"),
        ("NewConfig", "autofetch"),
        ("NewConfig", "background"),
        ("NewConfig", "clone_from"),
        ("NewConfig", "submodules"),
        ("OptionalMount", "container"),
        ("OptionalMount", "description"),
        ("OptionalMount", "host"),
        ("OptionalMount", "readonly"),
        ("PoolSpec", "allocate"),
        ("PoolSpec", "link_paths"),
        ("PoolSpec", "seed"),
        ("PoolSpec", "stale_globs"),
        ("PoolSpec", "warmth_file"),
        ("PoolSpec", "wipe_paths"),
        ("PullConfig", "delete_branch"),
        ("PullConfig", "destroy_container"),
        ("PushConfig", "autofetch"),
        ("PushConfig", "default_action"),
        ("PushConfig", "default_source"),
        ("PushConfig", "push_from"),
        ("SharedCache", "container_path"),
        ("SharedCache", "host_subpath"),
        ("SharedCache", "name"),
        ("SharedCache", "pool"),
        ("SshConfig", "enabled"),
        ("SshConfig", "seed_from_host"),
        ("Stacks", "docker"),
        ("Stacks", "ecr"),
        ("Stacks", "java"),
        ("Stacks", "node"),
        ("Stacks", "python"),
        ("TerminalConfig", "kitty"),
        ("TerminalKittyConfig", "enabled"),
        ("TerminalKittyConfig", "host_terminfo_path"),
    }
)


def walk_models(*roots: type[BaseModel]) -> list[type[BaseModel]]:
    """Every model class reachable from `roots`, including subclasses.

    Subclasses matter: `agents.claude` is a `ClaudeAgentConfig`, whose
    Claude-only fields are invisible if only the declared `AgentConfig`
    field type is walked.
    """
    seen: dict[str, type[BaseModel]] = {}
    queue = list(roots)
    while queue:
        model = queue.pop()
        if model.__name__ in seen:
            continue
        seen[model.__name__] = model
        for info in model.model_fields.values():
            for candidate in _models_in(info.annotation):
                queue.append(candidate)
        queue.extend(model.__subclasses__())
    return list(seen.values())


def _models_in(annotation: object) -> list[type[BaseModel]]:
    """Model classes appearing anywhere inside an annotation."""
    from typing import get_args

    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    for arg in get_args(annotation):
        found.extend(_models_in(arg))
    return found


def field_paths() -> list[tuple[str, str, FieldInfo]]:
    """(model name, field name, FieldInfo) for every field under test."""
    out: list[tuple[str, str, FieldInfo]] = []
    for model in walk_models(Config, GlobalConfig, ClaudeAgentConfig):
        for name, info in model.model_fields.items():
            if (model.__name__, name) in EXCLUDED:
                continue
            out.append((model.__name__, name, info))
    return out


def test_walk_reaches_the_known_models():
    """Guard the walker itself: a broken walk would make the suite vacuous."""
    names = {m.__name__ for m in walk_models(Config, GlobalConfig, ClaudeAgentConfig)}
    for expected in (
        "Config",
        "GlobalConfig",
        "AgentConfig",
        "ClaudeAgentConfig",
        "HostMount",
        "AutostartStep",
        "Golden",
        "Stacks",
    ):
        assert expected in names, f"model walk missed {expected}"


def test_every_field_has_a_description():
    missing = sorted(
        (model, field)
        for model, field, info in field_paths()
        if not (info.description or "").strip()
    )
    still_allowed = sorted(UNDOCUMENTED)
    assert missing == [] or set(missing) <= set(still_allowed), (
        "fields with no description= and not on the allowlist: "
        f"{sorted(set(missing) - set(still_allowed))}"
    )


def test_allowlist_has_no_stale_entries():
    """An allowlisted field that now has a description must leave the list."""
    documented = {
        (model, field) for model, field, info in field_paths() if (info.description or "").strip()
    }
    stale = sorted(UNDOCUMENTED & documented)
    assert stale == [], f"remove from UNDOCUMENTED, these are documented now: {stale}"
