"""Global (host-level) configuration.

Stored at $XDG_CONFIG_HOME/jailbee/global.yaml (default
~/.config/jailbee/global.yaml). Optional file — if absent, defaults are used.
Carries `docker_registry_mirror`, `loose_auto_revert`, `claude_credentials`,
and the `ls` / `dashboard` column preferences.

Per-repo configuration lives in <repo>/.jailbee/config.yaml — see config.py.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from jailbee.config import (
    DASHBOARD_DEFAULT_HIDE,
    ClaudeCredentials,
    ColumnConfig,
    ConfigError,
    LooseAutoRevert,
    _columns_already_sanitized,
)
from jailbee.paths import expand_path, xdg_data_home


def _expand(value: str | Path) -> Path:
    return expand_path(value)


PathExpanded = Annotated[Path, BeforeValidator(_expand)]


def default_global_config_path() -> Path:
    """Return ~/.config/jailbee/global.yaml (or $XDG_CONFIG_HOME/jailbee/global.yaml)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "jailbee" / "global.yaml"


def _default_registry_data_dir() -> Path:
    return xdg_data_home() / "jailbee" / "registry"


class DockerRegistryMirror(BaseModel):
    model_config = ConfigDict(extra="forbid")
    port: int = Field(
        default=3128,
        description="Port the rpardini registry-proxy listens on inside the mirror container.",
    )
    data_dir: PathExpanded = Field(
        default=None,  # type: ignore[assignment]
        description=(
            "Host directory bind-mounted into the mirror container for its cache and "
            "CA storage. Defaults to `<xdg_data_home>/jailbee/registry`, computed "
            "after init since it depends on `Path.home()`."
        ),
    )
    image: str = Field(
        default="rpardini/docker-registry-proxy:0.6.5",
        description=(
            "OCI image tag the mirror container runs. Pinned to a specific tag rather "
            "than `latest`, since an upgrade should be deliberate."
        ),
    )
    # bool-first ordering follows the `Stacks.java: bool | str` idiom for
    # three-valued keys in this codebase; pydantic binds YAML `true`/`false`
    # to bool either way here, since `Literal["auto"]` cannot accept a bool.
    enabled: bool | Literal["auto"] = Field(
        default="auto",
        description=(
            "Whether the mirror is wired into containers' egress and `/etc/hosts`. "
            "`auto` (default) turns it on only for repos that show a signal they need "
            "Docker — see `docker_daemon.mirror_wanted` for the exact signals. `true` "
            "forces it on for every repo on the host; `false` turns off all "
            "mirror-related work regardless of what any repo asks for."
        ),
    )

    def model_post_init(self, __context: object) -> None:
        # Default is computed (uses Path.home()), so we set it post-init.
        if self.data_dir is None:
            object.__setattr__(self, "data_dir", _default_registry_data_dir())


class GlobalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # default_factory ensures DockerRegistryMirror's model_post_init re-runs
    # per GlobalConfig instance, picking up current $XDG_DATA_HOME each time.
    docker_registry_mirror: DockerRegistryMirror = Field(
        default_factory=DockerRegistryMirror,
        description=(
            "Host-global overrides for the rpardini Docker-registry-proxy mirror "
            "shared by every repo on this host — whether it's wired in, which "
            "port/image it runs, and where its cache lives. Host-level only: a repo's "
            "`.jailbee/config.yaml` can only add `extra_registries`, not touch this "
            "block."
        ),
    )
    loose_auto_revert: LooseAutoRevert = Field(
        default_factory=LooseAutoRevert,
        description=(
            "Host-wide default for auto-reverting `jailbee net loose` back to the "
            "previous network mode after a TTL. A repo's own `loose_auto_revert` "
            "block in `.jailbee/config.yaml` overrides this field-by-field — but not "
            "by reading this field: `loose_auto_revert` isn't host-level "
            "(`common.py`'s `_HOST_LEVEL_KEYS`), so a loaded `global.yaml` never "
            "populates this instance; the effective value comes from "
            "`Config.effective_loose_auto_revert()` instead."
        ),
    )
    ls: ColumnConfig = Field(
        default_factory=ColumnConfig,
        description=(
            "Host-wide default set of columns `jailbee ls` shows. A repo's own `ls` "
            "block in `.jailbee/config.yaml` overrides this field-by-field — naming "
            "`fields` there replaces this list outright rather than appending to it."
        ),
    )
    dashboard: ColumnConfig = Field(
        default_factory=lambda: ColumnConfig(hide=list(DASHBOARD_DEFAULT_HIDE)),
        description=(
            "Deprecated and ignored: imported once into each dashboard's own "
            "remembered column settings the first time it's opened after upgrading, "
            "then left alone. Use the dashboard's own settings instead — press F2 in "
            "`jailbee dashboard`, or View ▸ Columns in the GUI."
        ),
    )
    claude_credentials: ClaudeCredentials = Field(
        default_factory=ClaudeCredentials,
        description=(
            "Lets several repos on this host share one Claude Code login instead of "
            "each needing its own `/login`. Host-level only — setting this or the "
            "computed `claude_credentials_dir` in a repo's `.jailbee/config.yaml` is "
            "rejected at load time."
        ),
    )


_LS_DEFAULT = ColumnConfig()
_DASHBOARD_DEFAULT = ColumnConfig(hide=list(DASHBOARD_DEFAULT_HIDE))


def _load_unsanitized(path: Path) -> GlobalConfig:
    """Load global config with schema validation but no column-block recovery.

    `global.yaml` is also the source for Config-layer overlay keys (gpg,
    ssh, chrome, jetbrains, host_mounts, ...). Those are split out by
    `_split_host_keys()` at `load_config()` time; here we discard them
    and validate only the host-level subset.

    Genuine schema problems (bad YAML, a non-mapping top level, or a
    ``docker_registry_mirror``/``ls``/``dashboard`` block shaped wrong —
    e.g. ``fields`` not a list) raise ``ConfigError``: those are host-level
    keys, and unlike a column *name* typo (see ``load_global_config``) there
    is nothing sensible to recover to.

    Shared by ``load_global_config`` (which sanitizes the result before
    returning it) and ``global_config_issues`` (which inspects it as-is, so
    `jailbee config validate` still sees exactly what's wrong).
    """
    if not path.exists():
        return GlobalConfig()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"Top level of {path} must be a mapping; got {type(raw).__name__}.")
    # Local import: config.py imports ConfigError from this module, so a
    # module-level import would form a cycle.
    from jailbee.config import _split_host_keys

    host_raw, _ = _split_host_keys(raw)
    try:
        return GlobalConfig.model_validate(host_raw)
    except ValidationError as e:
        raise ConfigError(f"Global config validation failed in {path}:\n{e}") from e


def load_global_config(path: Path) -> tuple[GlobalConfig, list[str]]:
    """Load global config; return (config, warnings).

    ``warnings`` lists column-name problems in the ``ls`` / ``dashboard``
    blocks that were fixed up rather than rejected — an unknown name
    dropped, a duplicate collapsed, an empty ``fields`` reset to the
    built-in default set (see ``config.sanitize_column_blocks``). Those
    blocks are host-level (``config._HOST_LEVEL_KEYS``) and read on every
    path that renders a table, so a typo there must never be fatal: it is a
    personal display preference, and breaking an unrelated command over a
    cosmetic typo is the wrong trade — the same principle that keeps a
    column preference from narrowing `--format json`. `cli._load_global()`
    is the one place ``warnings`` gets surfaced (via `tui.warn`); the
    dashboards (`dashboard._global_config_or_defaults`) get the sanitized
    config and otherwise ignore the list.

    Genuine host-level schema problems (bad YAML, a malformed
    ``docker_registry_mirror``, ...) are a different matter and still raise
    ``ConfigError`` — see ``_load_unsanitized``. `jailbee config validate`
    reports column-name problems as errors instead of recovering from them
    — see ``global_config_issues``.
    """
    gcfg = _load_unsanitized(path)

    # Early return: both blocks already look exactly like their defaults
    # (the common case — most repos never touch column config), so skip
    # building `lifecycle.ls_field_specs`'s full field list just to confirm
    # nothing needs fixing. This loader runs on the dashboard's refresh
    # cadence (`dashboard.gather_rows` calls it once per tick), so the
    # saved work is not one-time — the global-layer twin of `load_config`'s
    # short-circuit for the repo layer; see `_columns_already_sanitized` for
    # why comparing by value here is safe.
    if _columns_already_sanitized([(gcfg.ls, _LS_DEFAULT), (gcfg.dashboard, _DASHBOARD_DEFAULT)]):
        return gcfg, []

    # Local import: config.py imports names from this module, so a
    # module-level import would form a cycle.
    from jailbee.config import sanitize_column_blocks

    fixed, warnings = sanitize_column_blocks([("ls", gcfg.ls), ("dashboard", gcfg.dashboard)])
    if warnings:
        gcfg = gcfg.model_copy(update=fixed)
    return gcfg, warnings


def global_config_issues(path: Path) -> list[str]:
    """Column-block problems in `global.yaml`, reported rather than fixed up.

    For `jailbee config validate`: unlike ordinary loading (`load_global_config`,
    which recovers from these so no other command breaks over a typo), the
    one command whose job is validating config should still fail on one,
    with the allowed names listed — the same treatment `Config.validate_runtime`
    gives the equivalent repo-level blocks.

    Raises ``ConfigError`` for a genuine host-level schema problem, same as
    `load_global_config` — those stay fatal everywhere, including here.
    """
    from jailbee.config import validate_column_blocks

    gcfg = _load_unsanitized(path)
    issues = validate_column_blocks([("global.ls", gcfg.ls), ("global.dashboard", gcfg.dashboard)])
    if "dashboard" in gcfg.model_fields_set:
        issues.append(
            "global.dashboard: deprecated and ignored — the dashboards remember "
            "their own columns now (press F2 in `jailbee dashboard`, or View ▸ "
            "Columns in the GUI). This block is imported into each dashboard's own "
            "settings the first time you open that dashboard after upgrading; it "
            "can be deleted once you have opened both the TUI and the GUI."
        )
    return issues
