"""The `jailbee.config` package must keep re-exporting its whole surface.

The package was split out of a single 2871-line module. Every name below
is imported from `jailbee.config` by at least one module or test, so
dropping one from `__init__.py` breaks an importer that this test names
directly instead of leaving a stack trace in an unrelated test.
"""

from __future__ import annotations

import jailbee.config as config

# Harvested from `from jailbee.config import ...` across src/ and tests/.
EXPECTED_SURFACE = frozenset(
    {
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
        "slugify_prefix",
        "validate_column_blocks",
    }
)


def test_every_expected_name_is_importable():
    missing = sorted(n for n in EXPECTED_SURFACE if not hasattr(config, n))
    assert missing == [], f"jailbee.config no longer exports: {missing}"


def test_all_lists_the_public_surface():
    """Public names (no leading underscore) must be declared in __all__.

    mypy --strict runs with --no-implicit-reexport, so a name absent from
    __all__ is not re-exported for type-checking purposes even though it
    imports fine at runtime. Private names are deliberately excluded:
    they are re-exported for the tests and for global_config, not offered
    as public API.
    """
    public = {n for n in EXPECTED_SURFACE if not n.startswith("_")}
    assert public <= set(config.__all__)
