"""Per-repo configuration models and loader.

Loads YAML config from <repo>/.jailbee/config.yaml. Validates with Pydantic.
Most blocks are optional with sensible defaults; an empty `{}` config is
valid and produces a fully-defaulted Config.

`Config` carries four computed (non-YAML) attributes set at load time:
  * repo_root        — directory containing `.jailbee/`
  * upstream_remote  — auto-detected via `git.detect_upstream_remote`
  * default_branch   — `refs/remotes/<upstream_remote>/HEAD`
  * container_prefix — repo_root.name (used for container naming)
"""

from __future__ import annotations

from jailbee.config.common import CONTAINER_USERNAME, _HOST_LEVEL_KEYS, _split_host_keys, deep_merge
from jailbee.config.errors import ConfigError, ConfigNotFoundError
from jailbee.config.loader import (
    _build_config_from_dict,
    device_name,
    load_config,
    load_config_from_text,
    load_config_unsanitized,
    resolve_agents_raw,
)
from jailbee.config.models_agents import (
    AgentConfig,
    Autostart,
    AutostartStep,
    ClaudeAgentConfig,
    DockerRegistryMirrorRepoConfig,
    GithubConfig,
)
from jailbee.config.models_behaviour import (
    BootConfig,
    ConfirmConfig,
    NewConfig,
    PullConfig,
    PushConfig,
)
from jailbee.config.models_columns import (
    DASHBOARD_DEFAULT_HIDE,
    ColumnConfig,
    _columns_already_sanitized,
    sanitize_column_blocks,
    validate_column_blocks,
)
from jailbee.config.models_golden import Golden, IdeName, Stacks
from jailbee.config.models_host import (
    POOL_PRESETS,
    HostDevice,
    HostMount,
    HostPort,
    OptionalMount,
    PoolSpec,
    SharedCache,
    _default_shared_caches,
)
from jailbee.config.models_net import (
    GITHUB_API_HOSTS,
    JETBRAINS_AI_HOSTS,
    JETBRAINS_LICENSE_HOSTS,
    LOOSE_TTL_PRESETS,
    NET_DESCRIPTIONS,
    OFFLINE_REMOVED_MSG,
    ClaudeCredentials,
    LooseAutoRevert,
    format_loose_after,
    parse_loose_ttl,
)
from jailbee.config.models_tools import (
    ChromeConfig,
    GpgConfig,
    JetbrainsConfig,
    SshConfig,
    TerminalConfig,
    TerminalKittyConfig,
    _kitty_terminfo_candidates,
    resolve_kitty_terminfo_path,
)
from jailbee.config.retired import _check_retired_keys
from jailbee.config.root import Config

__all__ = [
    "AgentConfig",
    "Autostart",
    "AutostartStep",
    "BootConfig",
    "CONTAINER_USERNAME",
    "ChromeConfig",
    "ClaudeAgentConfig",
    "ClaudeCredentials",
    "ColumnConfig",
    "Config",
    "ConfigError",
    "ConfigNotFoundError",
    "ConfirmConfig",
    "DASHBOARD_DEFAULT_HIDE",
    "DockerRegistryMirrorRepoConfig",
    "GITHUB_API_HOSTS",
    "GithubConfig",
    "Golden",
    "GpgConfig",
    "HostDevice",
    "HostMount",
    "HostPort",
    "IdeName",
    "JETBRAINS_AI_HOSTS",
    "JETBRAINS_LICENSE_HOSTS",
    "JetbrainsConfig",
    "LOOSE_TTL_PRESETS",
    "LooseAutoRevert",
    "NET_DESCRIPTIONS",
    "NewConfig",
    "OFFLINE_REMOVED_MSG",
    "OptionalMount",
    "POOL_PRESETS",
    "PoolSpec",
    "PullConfig",
    "PushConfig",
    "SharedCache",
    "SshConfig",
    "Stacks",
    "TerminalConfig",
    "TerminalKittyConfig",
    # Private names below are still listed here (not just imported) because
    # mypy --strict's --no-implicit-reexport treats a module's __all__, once
    # present, as the complete explicit-reexport list — the `as Name`
    # self-alias trick this package used before __all__ existed no longer
    # has any effect. Each of these is imported directly from
    # `jailbee.config` (not its owning submodule) by tests or by
    # `global_config.py`; see EXPECTED_SURFACE in
    # tests/test_config_package_surface.py.
    "_HOST_LEVEL_KEYS",
    "_build_config_from_dict",
    "_check_retired_keys",
    "_columns_already_sanitized",
    "_default_shared_caches",
    "_kitty_terminfo_candidates",
    "_split_host_keys",
    "deep_merge",
    "device_name",
    "format_loose_after",
    "load_config",
    "load_config_from_text",
    "load_config_unsanitized",
    "parse_loose_ttl",
    "resolve_agents_raw",
    "resolve_kitty_terminfo_path",
    "sanitize_column_blocks",
    "validate_column_blocks",
]
