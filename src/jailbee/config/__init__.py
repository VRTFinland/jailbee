"""Per-repo configuration models and loader.

Loads YAML config from <repo>/.jailbee/config.yaml. Validates with Pydantic.
Most blocks are optional with sensible defaults; an empty `{}` config is
valid and produces a fully-defaulted Config.

`Config` carries four computed (non-YAML) attributes set at load time:
  * repo_root             — directory containing `.jailbee/`
  * default_branch        — `refs/remotes/<upstream_remote>/HEAD`
  * upstream_remote       — auto-detected via `git.detect_upstream_remote`
  * claude_credentials_dir — derived from host-level `claude_credentials` block

`container_prefix` is a real YAML key (documented, hand-edited) whose
*fallback* is computed: `repo_root.name` when left empty.
"""

from __future__ import annotations

from jailbee.config.common import (
    _HOST_LEVEL_KEYS,
    CONTAINER_USERNAME,
    SCRATCH_ORIGIN_SUFFIX,
    _split_host_keys,
    deep_merge,
)
from jailbee.config.errors import ConfigError, ConfigNotFoundError
from jailbee.config.loader import (
    SCRATCH_BASE_ALIAS,
    _build_config_from_dict,
    device_name,
    load_config,
    load_config_from_text,
    load_config_unsanitized,
    load_repo_config,
    load_repo_config_unsanitized,
    resolve_agents_raw,
    scratch_repo_layer,
    synthesized_repo_layer,
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
    slugify_prefix,
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

# Private (leading-underscore) names are listed in __all__ alongside the
# public ones, not just imported above, because mypy --strict's
# --no-implicit-reexport treats a module's __all__, once present, as the
# complete explicit-reexport list — the `as Name` self-alias trick this
# package used before __all__ existed no longer has any effect. Each private
# name below is imported directly from `jailbee.config` (not its owning
# submodule) by tests or by `global_config.py`; see EXPECTED_SURFACE in
# tests/test_config_package_surface.py.
__all__ = [
    "CONTAINER_USERNAME",
    "DASHBOARD_DEFAULT_HIDE",
    "GITHUB_API_HOSTS",
    "JETBRAINS_AI_HOSTS",
    "JETBRAINS_LICENSE_HOSTS",
    "LOOSE_TTL_PRESETS",
    "NET_DESCRIPTIONS",
    "OFFLINE_REMOVED_MSG",
    "POOL_PRESETS",
    "SCRATCH_BASE_ALIAS",
    "SCRATCH_ORIGIN_SUFFIX",
    "_HOST_LEVEL_KEYS",
    "AgentConfig",
    "Autostart",
    "AutostartStep",
    "BootConfig",
    "ChromeConfig",
    "ClaudeAgentConfig",
    "ClaudeCredentials",
    "ColumnConfig",
    "Config",
    "ConfigError",
    "ConfigNotFoundError",
    "ConfirmConfig",
    "DockerRegistryMirrorRepoConfig",
    "GithubConfig",
    "Golden",
    "GpgConfig",
    "HostDevice",
    "HostMount",
    "HostPort",
    "IdeName",
    "JetbrainsConfig",
    "LooseAutoRevert",
    "NewConfig",
    "OptionalMount",
    "PoolSpec",
    "PullConfig",
    "PushConfig",
    "SharedCache",
    "SshConfig",
    "Stacks",
    "TerminalConfig",
    "TerminalKittyConfig",
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
    "load_repo_config",
    "load_repo_config_unsanitized",
    "parse_loose_ttl",
    "resolve_agents_raw",
    "resolve_kitty_terminfo_path",
    "sanitize_column_blocks",
    "scratch_repo_layer",
    "slugify_prefix",
    "synthesized_repo_layer",
    "validate_column_blocks",
]
