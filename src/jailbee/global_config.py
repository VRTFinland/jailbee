"""Global (host-level) configuration.

Stored at $XDG_CONFIG_HOME/jailbee/global.yaml (default
~/.config/jailbee/global.yaml). Optional file — if absent, defaults are used.
Carries `docker_registry_mirror`, `loose_auto_revert`, `credentials`,
and the `ls` / `dashboard` column preferences.

Per-repo configuration lives in <repo>/.jailbee/config.yaml — see config.py.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from jailbee.config import (
    DASHBOARD_DEFAULT_HIDE,
    ColumnConfig,
    ConfigError,
    Credentials,
    LooseAutoRevert,
    _columns_already_sanitized,
    _split_host_keys,
    normalize_credentials_key,
)
from jailbee.config.models_remote import RemoteConfig
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


class ScratchConfig(BaseModel):
    """Defaults for a directory that has no `.jailbee/config.yaml`.

    `config` is a repo-config document — the same schema as
    `.jailbee/config.yaml` — merged as the repo layer by
    `config.load_repo_config` when there is no file to read. Deliberately
    untyped here: declaring the `Config` schema a second time would create a
    second place to keep in sync with every future config key, and the real
    validator runs on it anyway at synthesis time.

    `enabled: false` restores the pre-feature behaviour — a missing config
    file is `ConfigNotFoundError` everywhere.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=True,
        description=(
            "Whether `jailbee` synthesizes a repo config for a directory with no "
            "`.jailbee/config.yaml`. `false` restores the pre-feature behaviour: a "
            "missing config file is `ConfigNotFoundError` everywhere."
        ),
    )
    config: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "Raw repo-config overrides applied on top of the built-in defaults for a "
            "directory with no `.jailbee/config.yaml`. Same shape as "
            "`.jailbee/config.yaml` itself — validated against the `Config` schema at "
            "synthesis time, not here."
        ),
    )


class ConfigEditPolicy(BaseModel):
    """How `jailbee config edit` writes the file it saves."""

    model_config = ConfigDict(extra="forbid")
    write_policy: Literal["auto", "patch", "regenerate"] = Field(
        default="auto",
        description=(
            "How `jailbee config edit` writes a file it saves. `patch` touches only the "
            "keys you changed and leaves comments, key order and formatting alone. "
            "`regenerate` rewrites the whole file with jailbee's own generated comments, "
            "which drops any note you wrote in it. `auto` picks per layer — `regenerate` "
            "for this file, which jailbee owns and nobody reviews, and `patch` for a "
            "repo's `.jailbee/config.yaml`, which is committed and read as a PR diff. "
            "`jailbee config edit --write patch|regenerate` overrides this for one run."
        ),
    )


class DashboardAutoHide(BaseModel):
    """TUI-only priority overrides; omitted columns use the built-in order."""

    model_config = ConfigDict(extra="forbid")
    hide_first: list[str] = Field(
        default_factory=list,
        description="Columns to remove first, in order, when the terminal is too narrow.",
    )


class DashboardConfig(ColumnConfig):
    """New layout preferences alongside the legacy one-time column seed."""

    hide: list[str] = Field(
        default_factory=lambda: list(DASHBOARD_DEFAULT_HIDE),
        description="Legacy columns excluded from the one-time dashboard preference import.",
    )

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_columns(cls, value: object) -> object:
        if isinstance(value, ColumnConfig) and not isinstance(value, cls):
            return value.model_dump(exclude_unset=True)
        return value

    auto_hide: DashboardAutoHide = Field(
        default_factory=DashboardAutoHide,
        description="Temporary terminal-width column hiding (TUI only).",
    )


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
    dashboard: DashboardConfig = Field(
        default_factory=DashboardConfig,
        description=(
            "`auto_hide` controls temporary TUI column hiding. Legacy `fields`/`hide` "
            "are imported once into each dashboard's remembered column settings; "
            "use F2 in the TUI or View ▸ Columns in the GUI to change those."
        ),
    )
    credentials: Credentials = Field(
        default_factory=Credentials,
        description=(
            "Lets several repos on this host share one Claude Code login instead of "
            "each needing its own `/login`. Host-level only — setting this or the "
            "computed `credential_group` in a repo's `.jailbee/config.yaml` is "
            "rejected at load time."
        ),
    )
    scratch: ScratchConfig = Field(
        default_factory=ScratchConfig,
        description=(
            "Defaults for a directory that has no `.jailbee/config.yaml`. Host-level "
            "only: a repo's own config file cannot set this — it would be an unknown "
            "top-level key there."
        ),
    )
    update_check: bool = Field(
        default=True,
        description=(
            "Whether jailbee tells you when a newer release is on PyPI. The check "
            "itself never runs on a command's path: a command reads the last answer "
            "from its state database and, when that is over a day old, starts a "
            "detached probe that fetches "
            "`https://pypi.org/pypi/jailbee/json` for the next run. Nothing "
            "identifying you or your repos is sent. `false` turns it off, as does "
            "`JAILBEE_NO_UPDATE_CHECK=1` for a single command. Host-level only "
            "(`common.py`'s `_HOST_LEVEL_KEYS`): whether your machine talks to PyPI "
            "is not a repo's decision."
        ),
    )
    install_host_skills: bool = Field(
        default=False,
        description=(
            "When true, `jailbee setup` installs jailbee's bundled skills for every "
            "skill-capable agent it finds on this host (claude, codex, gemini, "
            "opencode), each in its own skills directory, and `jailbee doctor` "
            "verifies them. Off by default: the containers get their skills without "
            "any host action, and which agents run on the host itself is the user's "
            "call. Host-level only (`common.py`'s `_HOST_LEVEL_KEYS`)."
        ),
    )
    config_edit: ConfigEditPolicy = Field(
        default_factory=ConfigEditPolicy,
        description=(
            "Settings for `jailbee config edit` itself. Host-level only "
            "(`common.py`'s `_HOST_LEVEL_KEYS`): how your own files get written is a "
            "personal editing habit, so a repo's `.jailbee/config.yaml` cannot set it."
        ),
    )
    remote: RemoteConfig = Field(
        default_factory=RemoteConfig,
        description=(
            "Remote access settings shared by every repo on this host. Host-level only: "
            "a repo's `.jailbee/config.yaml` cannot enable or broaden remote access."
        ),
    )


_LS_DEFAULT = ColumnConfig()
_DASHBOARD_DEFAULT = DashboardConfig()


def _auto_hide_names(names: list[str]) -> tuple[list[str], list[str]]:
    """Canonicalize layout priorities, reporting cosmetic name mistakes."""
    if not names:
        return [], []
    from jailbee.config.models_columns import _known_ls_field_names, canonical_ls_field

    known = _known_ls_field_names()
    cleaned: list[str] = []
    issues: list[str] = []
    for raw in names:
        name = canonical_ls_field(raw)
        if name not in known:
            issues.append(f"global.dashboard.auto_hide.hide_first: unknown field {raw!r}")
        elif name in cleaned:
            issues.append(f"global.dashboard.auto_hide.hide_first: duplicate field {raw!r}")
        else:
            cleaned.append(name)
    return cleaned, issues


def validate_global_raw(
    raw: dict[str, object],
    path: Path,
    *,
    emit_hint: bool = True,
) -> GlobalConfig:
    """Validate the host-level half of an already-parsed `global.yaml`.

    `global.yaml` is also the source for Config-layer overlay keys (gpg,
    ssh, chrome, jetbrains, host_mounts, ...). Those are split out by
    `_split_host_keys()` at `load_config()` time; here we discard them
    and validate only the host-level subset.

    Genuine schema problems (a ``docker_registry_mirror``/``ls``/
    ``dashboard`` block shaped wrong — e.g. ``fields`` not a list) raise
    ``ConfigError``: those are host-level keys, and unlike a column *name*
    typo (see ``load_global_config``) there is nothing sensible to recover
    to.

    `emit_hint` gates the legacy `claude_credentials:` deprecation notice,
    mirroring `config.loader.load_config_from_layers`: the full-screen config
    editor calls this synchronously from its save handler and passes `False`,
    so a save never prints to the terminal the editor owns.

    Split from ``_load_unsanitized`` so a caller holding a mapping that is
    not on disk — the config editor validating a staged global layer
    before writing it — can reach the same rules. `path` is used only to
    label errors. The mirror of `config.loader.load_config_from_layers`
    on the `Config` side, and the reason that seam is symmetric: without
    it, ten of the twelve host-level paths the editor offers would be
    written unvalidated.
    """
    raw, folded = normalize_credentials_key(raw, str(path))
    if emit_hint and folded:
        from jailbee.config.loader import _warn_legacy_credentials_block

        _warn_legacy_credentials_block(str(path))
    host_raw, _ = _split_host_keys(raw)
    try:
        config = GlobalConfig.model_validate(host_raw)
        allow = config.remote.ssh.commands.allow
        if allow:
            from jailbee.remote_ssh.router import known_command_paths

            unknown = sorted(set(allow) - known_command_paths())
            if unknown:
                joined = ", ".join(unknown)
                raise ValueError(f"unknown remote Jailbee command path(s): {joined}")
        return config
    except (ValidationError, ValueError) as e:
        raise ConfigError(f"Global config validation failed in {path}:\n{e}") from e


def _load_unsanitized(path: Path) -> GlobalConfig:
    """Load global config with schema validation but no column-block recovery.

    A missing file is an ordinary state and yields a defaulted
    ``GlobalConfig``. Bad YAML and a non-mapping top level raise
    ``ConfigError``; everything past the parse is
    ``validate_global_raw``'s.

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
    return validate_global_raw(raw, path)


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

    priorities, priority_warnings = _auto_hide_names(gcfg.dashboard.auto_hide.hide_first)
    if priorities != gcfg.dashboard.auto_hide.hide_first:
        gcfg = gcfg.model_copy(
            update={
                "dashboard": gcfg.dashboard.model_copy(
                    update={
                        "auto_hide": gcfg.dashboard.auto_hide.model_copy(
                            update={"hide_first": priorities}
                        )
                    }
                )
            }
        )

    # Early return: both blocks already look exactly like their defaults
    # (the common case — most repos never touch column config), so skip
    # building `lifecycle.ls_field_specs`'s full field list just to confirm
    # nothing needs fixing. This loader runs on the dashboard's refresh
    # cadence (`dashboard.gather_rows` calls it once per tick), so the
    # saved work is not one-time — the global-layer twin of `load_config`'s
    # short-circuit for the repo layer; see `_columns_already_sanitized` for
    # why comparing by value here is safe.
    dashboard_columns = gcfg.dashboard.model_copy(update={"auto_hide": DashboardAutoHide()})
    if _columns_already_sanitized(
        [(gcfg.ls, _LS_DEFAULT), (dashboard_columns, _DASHBOARD_DEFAULT)]
    ):
        return gcfg, priority_warnings

    # Local import: config.py imports names from this module, so a
    # module-level import would form a cycle.
    from jailbee.config import sanitize_column_blocks

    # Applied unconditionally, not `if warnings`: an alias rewrite
    # (`claude_group` -> `group`) is a fix that produces no warning by design,
    # so gating on the warning list would drop it and leave the deprecated
    # spelling in the loaded block — where nothing downstream understands it.
    # `sanitize_column_blocks` returns each block unchanged (the same object)
    # when it needed no fix, so this costs nothing in the common case.
    fixed, warnings = sanitize_column_blocks([("ls", gcfg.ls), ("dashboard", gcfg.dashboard)])
    gcfg = gcfg.model_copy(update=fixed)
    return gcfg, priority_warnings + warnings


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
    _, priority_issues = _auto_hide_names(gcfg.dashboard.auto_hide.hide_first)
    issues.extend(priority_issues)
    if (
        "dashboard" in gcfg.model_fields_set
        and {"fields", "hide"} & gcfg.dashboard.model_fields_set
    ):
        issues.append(
            "global.dashboard.fields/hide: deprecated — the dashboards remember "
            "their own columns now (press F2 in `jailbee dashboard`, or View ▸ "
            "Columns in the GUI). These keys are imported once per frontend and "
            "can then be removed; dashboard.auto_hide remains active."
        )
    return issues
