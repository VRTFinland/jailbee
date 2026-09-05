"""Loaders that turn `.jailbee/config.yaml` (plus `global.yaml`) into a
validated `Config`.

Layering rules: scalars override, lists append (`[]` resets), dicts
deep-merge. See `common.deep_merge()` and docs/config.md for details.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from jailbee.config.common import (
    SCRATCH_ORIGIN_SUFFIX,
    _copy,
    _parse_yaml_text,
    _read_yaml_or_empty,
    _split_host_keys,
    deep_merge,
)
from jailbee.config.errors import ConfigError, ConfigNotFoundError
from jailbee.config.models_columns import (
    _COLUMN_DEFAULT,
    _columns_already_sanitized,
    sanitize_column_blocks,
)
from jailbee.config.models_golden import _RESERVED_PROVISION_ENV_KEYS
from jailbee.config.models_host import (
    _CACHE_NAME_RE,
    _PREFIX_RE,
    POOL_PRESETS,
    slugify_prefix,
)
from jailbee.config.models_net import ClaudeCredentials
from jailbee.config.retired import (
    _check_agents_spelling,
    _check_pull_migration,
    _check_retired_keys,
)
from jailbee.config.root import Config
from jailbee.git import DEFAULT_REMOTE, detect_default_branch, detect_upstream_remote
from jailbee.paths import REPO_CONFIG_DIRS, repo_config_path_warned, xdg_data_home

if TYPE_CHECKING:
    # Runtime import would be a cycle: `global_config` imports from
    # `jailbee.config` at module level. `from __future__ import annotations`
    # keeps the `scratch_repo_layer` annotation a string, so this is enough.
    from jailbee.global_config import ScratchConfig


def _claude_credentials_from_host_raw(
    host_raw: dict[str, object],
    origin: Path,
) -> ClaudeCredentials:
    """Validate the host layer's `claude_credentials` block.

    A second validation site for the same model class — `GlobalConfig` also
    carries it, so `jailbee config validate` and `doctor` see it through
    `load_global_config`. Only the error wrapper differs; the shape cannot
    drift because both validate `ClaudeCredentials`.

    Resolution happens here rather than in `_build_config_from_dict` because
    it needs `container_prefix`, which that function derives, and because
    `_build_config_from_dict` has callers that never see the host layer.
    """
    block = host_raw.get("claude_credentials")
    if block is None:
        return ClaudeCredentials()
    try:
        return ClaudeCredentials.model_validate(block)
    except ValidationError as e:
        raise ConfigError(f"Invalid `claude_credentials` in {origin}:\n{e}") from e


def _validate_pooled_caches(cfg: Config) -> None:
    """Reject `pooled_caches` keys that name nothing, have no preset, or
    try to un-pool a `PoolPreset.pool_only` cache."""
    if not cfg.pooled_caches:
        return
    caches = {c.name: c for c in cfg.effective_shared_caches()}
    for name, wanted in cfg.pooled_caches.items():
        cache = caches.get(name)
        if cache is None:
            raise ConfigError(
                f"pooled_caches: no shared cache named '{name}'. "
                f"Known names: {', '.join(sorted(caches))}"
            )
        if wanted and cache.pool is None:
            raise ConfigError(
                f"pooled_caches: '{name}' has no builtin pool preset. "
                f"Give the shared_caches entry an explicit `pool:` block instead."
            )
        preset = POOL_PRESETS.get(name)
        if not wanted and preset is not None and preset.pool_only:
            remedy = (
                "Set `chrome.enabled: false` to turn Chrome off instead."
                if name == "chrome-profile"
                else "Disable the integration that adds it instead."
            )
            raise ConfigError(
                f"pooled_caches: '{name}' cannot be un-pooled — its host "
                f"directory is the pool root itself, so a plain shared mount "
                f"would point every container at the pool's own `slots/` and "
                f"`by-container/`. {remedy}"
            )


def _derive_repo_root(config_path: Path) -> Path:
    """repo_root = directory containing .jailbee/.

    Standard case: config_path = <repo>/.jailbee/config.yaml → parent.parent = <repo>.
    Override case (jailbee -c /weird/path.yaml): we still take parent.parent. The
    --config flag is a debug tool and accepts odd inputs.
    """
    return config_path.parent.parent


def _default_shared_dir(container_prefix: str) -> Path:
    return xdg_data_home() / "jailbee" / "shared" / container_prefix


def device_name(subpath: str) -> str:
    """Incus disk-device name for a shared subpath.

    `.` → `-` is not cosmetic: it must yield exactly `claude` and
    `claude-install` for Claude's two subpaths, because those are live
    device names in every existing container's binds profile. The same rule
    still matters for any user-declared `type: file` mount whose subpath
    contains a dot. A different rule renames disk devices under running
    containers.
    """
    return subpath.replace(".", "-")


def resolve_agents_raw(raw: dict[str, object]) -> dict[str, object]:
    """Normalise the `agents:`/`claude:` region of a merged raw config.

    Returns a new dict in which:
      * a legacy `claude:` block has been moved to `agents.claude`,
      * every entry has been deep-merged over its preset (preset first, so
        user scalars win and user `egress_allow` appends).

    Runs on the *merged* global+repo raw dict, before validation, so a
    partially-specified entry (`{enabled: true}`) validates against the
    preset's completed shape.
    """
    from jailbee.agent_presets import AGENT_PRESETS, claude_preset

    result = {k: _copy(v) for k, v in raw.items()}
    raw_agents = result.pop("agents", {})
    if not isinstance(raw_agents, dict):
        raise ConfigError("`agents` must be a mapping of agent name to settings.")
    agents: dict[str, object] = {k: _copy(v) for k, v in raw_agents.items()}

    legacy = result.pop("claude", None)
    if legacy is not None:
        if "claude" in agents:
            # No file is named: this runs on the *merged* global+repo dict, so
            # either layer (or both) may carry either spelling, and the
            # `Config validation failed in <repo config>` wrapper in
            # `_build_config_from_dict` would assert the wrong one. The load
            # path checks the layers separately first
            # (`_check_agents_spelling`) and does name every file; this is the
            # backstop for callers that reach `resolve_agents_raw` directly.
            raise ConfigError(
                "config defines both `claude:` and `agents.claude` — keep one, "
                "in whichever of ~/.config/jailbee/global.yaml and the repo's "
                ".jailbee/config.yaml defines it. `agents.claude` is the "
                "preferred spelling; the `claude:` block is a supported legacy "
                "alias."
            )
        if not isinstance(legacy, dict):
            raise ConfigError("`claude` must be a mapping.")
        agents["claude"] = legacy

    presets: dict[str, dict[str, object]] = dict(AGENT_PRESETS)
    presets["claude"] = claude_preset()

    for name, entry in agents.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"`agents.{name}` must be a mapping.")
        base = presets.get(name)
        agents[name] = deep_merge(base, entry) if base is not None else entry

    if agents:
        result["agents"] = agents
    return result


def _build_config_from_dict(
    raw: dict[str, object], config_path: Path, *, origin: str | None = None
) -> Config:
    """Validate a raw merged dict and populate computed Config fields.

    Used by load_config to build the final Config from a (possibly merged)
    raw dict. Computed fields (repo_root, default_branch, upstream_remote,
    claude_credentials_dir, shared_dir, golden.alias) are set after Pydantic
    validation. container_prefix is conditionally derived: when left empty,
    it defaults to repo_root.name. Cross-field invariants (prefix regex,
    reserved env keys, shared_caches uniqueness, autostart step-name
    uniqueness) are checked here as well.

    `origin` labels the source in error messages when it is not the file at
    `config_path` — a config layer synthesized from `global.yaml`'s
    `scratch.config` has no file of its own.
    """
    label = origin or str(config_path)
    try:
        raw = resolve_agents_raw(raw)
    except ConfigError as e:
        raise ConfigError(f"Config validation failed in {label}:\n{e}") from e
    _check_retired_keys(raw)
    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"Config validation failed in {label}:\n{e}") from e

    repo_root = _derive_repo_root(config_path)
    object.__setattr__(cfg, "repo_root", repo_root)
    # Resolve the upstream remote before the default branch: the latter is
    # `refs/remotes/<remote>/HEAD`, so it depends on the former.
    upstream_remote = detect_upstream_remote(repo_root) or DEFAULT_REMOTE
    object.__setattr__(cfg, "upstream_remote", upstream_remote)
    object.__setattr__(cfg, "default_branch", detect_default_branch(repo_root, upstream_remote))
    if not cfg.container_prefix:
        object.__setattr__(cfg, "container_prefix", repo_root.name)
    if not _PREFIX_RE.match(cfg.container_prefix):
        raise ConfigError(
            f"Invalid container_prefix '{cfg.container_prefix}': must match "
            f"[a-z0-9][a-z0-9-]*. Set `container_prefix:` in {label} explicitly."
        )

    if cfg.shared_dir is None:
        object.__setattr__(cfg, "shared_dir", _default_shared_dir(cfg.container_prefix))

    if not cfg.golden.alias:
        object.__setattr__(cfg.golden, "alias", f"{cfg.container_prefix}-base")

    reserved = _RESERVED_PROVISION_ENV_KEYS & set(cfg.golden.provision_env)
    if reserved:
        raise ConfigError(
            f"golden.provision_env may not override built-in keys: "
            f"{sorted(reserved)}. These are set automatically by "
            f"`jailbee base build`."
        )

    seen_cache_names: set[str] = set()
    for cache in cfg.shared_caches:
        if not _CACHE_NAME_RE.match(cache.name):
            raise ConfigError(
                f"Invalid shared_caches name '{cache.name}': must match [a-z0-9][a-z0-9-]*"
            )
        if cache.name in seen_cache_names:
            raise ConfigError(f"duplicate shared_caches name: '{cache.name}'")
        seen_cache_names.add(cache.name)
        if not (cache.container_path.startswith("/") or cache.container_path.startswith("~")):
            raise ConfigError(
                f"shared_caches[{cache.name}].container_path must be "
                f"absolute or start with '~', got: {cache.container_path}"
            )

    for trigger_name in ("on_create", "on_start"):
        steps = getattr(cfg.autostart, trigger_name)
        seen_step_names: set[str] = set()
        for step in steps:
            if step.name in seen_step_names:
                raise ConfigError(f"duplicate autostart.{trigger_name} step name: '{step.name}'")
            seen_step_names.add(step.name)

    return cfg


def load_config_unsanitized(path: Path) -> Config:
    """Load and validate the per-repo YAML config from disk, without
    recovering from an `ls:`/`dashboard:` column-block problem.

    Layering rules: scalars override, lists append (`[]` resets), dicts
    deep-merge. See deep_merge() and docs/config.md for details.

    This is the raw builder: an unknown/duplicate/empty column name in
    `cfg.ls`/`cfg.dashboard` survives untouched, exactly as the YAML wrote
    it. `load_config` (the loader almost everyone should use) wraps this
    with `sanitize_column_blocks`, mirroring the global layer's
    `_load_unsanitized` / `load_global_config` split. `jailbee config validate`
    (`cli.config_validate`) calls this one directly, so `cfg.validate_runtime()`
    still sees the unrecovered blocks and reports a typo as an error there —
    the one command whose job is telling you what's wrong, same as
    `global_config.global_config_issues` does for `global.yaml`.
    """
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    return load_config_from_text(path.read_text(), path)


def load_config_from_text(text: str, path: Path) -> Config:
    """Build a validated Config from repo-config YAML *text*.

    Identical to `load_config_unsanitized` except the repo layer comes from
    `text` instead of from disk: same host-key split, retired-key check, pull
    migration check, `github` placement ban, global deep-merge, and token
    checks. `path` is used only to derive `repo_root` and to label errors — the
    file at `path` is never read and need not exist.

    Exists so a config committed on another git branch can be loaded with
    exactly the host's semantics (`branch_config`). Hand-merging a single
    block would diverge, because `deep_merge` appends lists and `global.yaml`
    may define its own `autostart` steps.
    """
    return _load_config_from_repo_raw(_parse_yaml_text(text, str(path)), path, origin=str(path))


def _load_config_from_repo_raw(repo_raw: dict[str, object], path: Path, *, origin: str) -> Config:
    """Build a validated `Config` from an already-parsed repo layer.

    The shared body of `load_config_from_text` (repo layer from YAML text) and
    the synthesized layer built in memory from `global.yaml`'s
    `scratch.config`. `path` derives `repo_root` and need not exist; `origin`
    is the human-readable source used in error messages, which for a
    synthesized layer is the global config file rather than `path`.

    Reads the global layer from disk. A caller holding a global layer that
    is not on disk — the config editor validating a staged change before
    writing it — calls `load_config_from_layers` directly.
    """
    # Local import avoids a circular dependency at module load: global_config
    # already imports from this module's ConfigError, and importing
    # default_global_config_path at module top would form a cycle.
    from jailbee.global_config import default_global_config_path

    global_raw = _read_yaml_or_empty(default_global_config_path())
    return load_config_from_layers(global_raw, repo_raw, path, origin=origin)


def load_config_from_layers(
    global_raw: dict[str, object],
    repo_raw: dict[str, object],
    path: Path,
    *,
    origin: str,
) -> Config:
    """Build a validated `Config` from two already-parsed raw layers.

    Every rule `_load_config_from_repo_raw` applies applies here — the
    retired-key check, the pull and agents migration checks, the `github`
    and `claude_credentials` placement bans, the deep merge, the 0600
    token-permission check — because this *is* that function's body. The
    only difference is that `global_raw` is supplied rather than read, so
    the config editor can validate a staged global layer before anything
    reaches disk (spec 3.5 step 2).

    `global_raw` is the whole `global.yaml` mapping, host-level keys
    included; the split is done here, exactly as the on-disk path does it.
    """
    from jailbee.global_config import default_global_config_path

    _check_retired_keys(global_raw)
    host_raw, global_for_merge = _split_host_keys(global_raw)
    _check_retired_keys(repo_raw)
    _check_pull_migration(global_for_merge, repo_raw, default_global_config_path(), path)
    _check_agents_spelling(global_for_merge, repo_raw, default_global_config_path(), path)

    # Placement constraint: github tokens must live in ~/.config/jailbee/global.yaml,
    # never in repo .jailbee/config.yaml — the latter is typically committed to git.
    if "github" in repo_raw:
        raise ConfigError(
            "`github` block is not allowed in repo .jailbee/config.yaml — "
            "move it to ~/.config/jailbee/global.yaml. Tokens would leak via "
            "git commit if placed here."
        )

    # Host-only, for the same reason as `github`: a repo config is typically
    # committed, and a credential-group name applies to whoever holds the
    # checkout. `claude_credentials_dir` is banned alongside it because it is a
    # declared Config field, so YAML could set it and be overwritten silently.
    for banned in ("claude_credentials", "claude_credentials_dir"):
        if banned in repo_raw:
            raise ConfigError(
                f"`{banned}` is not allowed in repo .jailbee/config.yaml — "
                f"move it to ~/.config/jailbee/global.yaml. A credential group "
                f"is host-local: committed here it would apply to every "
                f"teammate and name a group that exists on one machine only."
            )

    merged = deep_merge(global_for_merge, repo_raw)
    cfg = _build_config_from_dict(merged, path, origin=origin)

    creds = _claude_credentials_from_host_raw(host_raw, default_global_config_path())
    object.__setattr__(cfg, "claude_credentials_dir", creds.dir_for(cfg.container_prefix))

    _validate_pooled_caches(cfg)

    # Token security: global.yaml must be 0600 when it carries github.api_tokens.
    if cfg.github.api_tokens:
        gy = default_global_config_path()
        if gy.exists():
            mode = gy.stat().st_mode & 0o777
            if mode & 0o077 != 0:
                raise ConfigError(
                    f"{gy} contains github.api_tokens but has insecure perms "
                    f"(0{mode:03o}). Run `chmod 600 {gy}`."
                )

    if cfg.github.enabled and not cfg.github.api_tokens:
        raise ConfigError(
            "github.enabled=true but github.api_tokens is empty. "
            "Add at least one entry: <container_prefix>: <github_pat_...>"
        )

    return cfg


def _sanitize_columns(cfg: Config) -> Config:
    """Apply `sanitize_column_blocks` to `cfg.ls`/`cfg.dashboard` in place.

    Shared by `load_config` and the synthesized path, so a column typo
    degrades identically wherever the config came from. Fixes are recorded on
    `_column_warnings` for `cli._load_or_exit` to surface; this module never
    prints.
    """
    # Early return: both blocks already look exactly like their defaults
    # (the common case — most repos never touch column config), so skip
    # building `lifecycle.ls_field_specs`'s full field list just to confirm
    # nothing needs fixing. `dashboard.gather_rows` calls this loader once
    # per registered repo on every refresh tick, so the saved work is not
    # one-time — the repo-layer twin of `global_config.load_global_config`'s
    # short-circuit for the global layer.
    if _columns_already_sanitized([(cfg.ls, _COLUMN_DEFAULT), (cfg.dashboard, _COLUMN_DEFAULT)]):
        return cfg

    fixed, warnings = sanitize_column_blocks([("ls", cfg.ls), ("dashboard", cfg.dashboard)])
    if warnings:
        object.__setattr__(cfg, "ls", fixed["ls"])
        object.__setattr__(cfg, "dashboard", fixed["dashboard"])
    cfg._column_warnings = warnings
    return cfg


def load_config(path: Path) -> Config:
    """Load the per-repo config, recovering from `ls:`/`dashboard:` typos.

    Wraps `load_config_unsanitized` with `sanitize_column_blocks` — the same
    recovery `global_config.load_global_config` gives `global.yaml`: an
    unknown column name is dropped, a duplicate collapsed to its first
    occurrence, and an empty (or emptied-by-dropping) `fields` reset to the
    built-in default set, so a typo in a repo's `.jailbee/config.yaml` degrades
    gracefully instead of rendering a zero-column table for everyone working
    in that repo. `hide` is never reset to a default when empty — an
    explicitly empty `hide` is a real, deliberate value (see
    `sanitize_column_blocks`'s docstring), not the same footgun as an empty
    `fields`.

    Any fixes made are recorded on the returned `Config` (see
    `Config.column_warnings()`) rather than printed here — this module never
    prints. `cli._load_or_exit()` is the one place that surfaces them, via
    `tui.warn`, mirroring `cli._load_global()` for the global layer.
    """
    return _sanitize_columns(load_config_unsanitized(path))


SCRATCH_BASE_ALIAS = "jailbee-scratch-base"
"""Golden-image alias every synthesized config shares.

Pinned rather than derived from `container_prefix`, so the image is built once
for the host instead of once per directory. Because it is shared, its content
comes only from `global.yaml` — a scratch directory contributes no config of
its own — which is what makes one image sound rather than merely convenient.
The `jailbee-` prefix keeps it from colliding with a real repo in a directory
named `scratch`.
"""


def scratch_repo_layer(repo_root: Path, scratch: ScratchConfig) -> dict[str, object]:
    """The repo layer a directory with no config file gets.

    Two keys are set here rather than left to `_build_config_from_dict`:
    `container_prefix`, whose derivation from `repo_root.name` would reject a
    name like `Tutkimus_A` and advise setting the key in a file that does not
    exist; and `golden.alias`, whose derived `<prefix>-base` would give every
    scratch directory its own image. The user's `scratch.config` merges on top
    with the usual `deep_merge` rules, so both remain overridable.

    The unguarded builder: it neither consults `scratch.enabled` nor refuses a
    root directory, because it takes the `ScratchConfig` as an argument and has
    no view of either. Callers outside this module want
    `synthesized_repo_layer`, which applies both guards — printing a layer that
    no command in that directory would ever load is worse than printing
    nothing.
    """
    prefix = slugify_prefix(repo_root.name)
    if not prefix:
        raise ConfigError(
            f"Cannot derive a container prefix from the directory name "
            f"{repo_root.name!r} ({repo_root}). Run `jailbee config init` here "
            f"and set `container_prefix:` explicitly."
        )
    base: dict[str, object] = {
        "container_prefix": prefix,
        "golden": {"alias": SCRATCH_BASE_ALIAS},
    }
    return deep_merge(base, scratch.config)


def _refuse_scratch_root(repo_root: Path) -> None:
    """Refuse `$HOME`, the filesystem root, and anything in between.

    All are a mistaken `cd`, never a research directory, and the cost of
    being wrong is a container bind-mounting the user's whole home. Refusing
    `$HOME` alone was not enough: `/home` (or `/Users`, or any other ancestor
    of it) is strictly worse — it holds *every* user's home — yet slipped
    through an equality test. Hence the ancestor predicate, which subsumes the
    filesystem root as well; the root keeps its own message only because the
    remedy differs. Every directory that is not an ancestor of `$HOME` is
    allowed.
    """
    resolved = repo_root.resolve()
    if resolved == Path(resolved.anchor):
        raise ConfigError(
            f"Refusing to create a jailbee environment for {resolved} — that is "
            f"the filesystem root. `cd` into a project directory first."
        )
    try:
        home: Path | None = Path.home().resolve()
    except RuntimeError:  # home directory not resolvable; nothing to compare against
        home = None
    if home is not None and home.is_relative_to(resolved):
        what = (
            "your home directory"
            if resolved == home
            else f"an ancestor of your home directory ({home})"
        )
        raise ConfigError(
            f"Refusing to create a jailbee environment for {resolved} — that is "
            f"{what}. `cd` into a project directory, or run "
            f"`jailbee config init` there if you really mean it."
        )


def synthesized_repo_layer(repo_root: Path) -> dict[str, object]:
    """The repo layer `repo_root` gets when it has no config file — guarded.

    The single gate in front of `scratch_repo_layer`: it reads `global.yaml`
    itself, so it can refuse when `scratch.enabled` is false
    (`ConfigNotFoundError`) or when the directory is `$HOME` or an ancestor of
    it (`ConfigError`). Both the load path (`_synthesize_repo_config`) and the
    diagnostic path (`jailbee config show --layer repo`) go through here, which
    is what keeps them from disagreeing: the layer that is printed is exactly
    the layer that would be loaded, or neither happens.

    Local import: `global_config` imports from this module, so importing it at
    module level would form a cycle (see `_load_config_from_repo_raw`).
    """
    from jailbee.global_config import default_global_config_path, load_global_config

    gpath = default_global_config_path()
    gcfg, _ = load_global_config(gpath)
    if not gcfg.scratch.enabled:
        raise ConfigNotFoundError(
            f"No .jailbee/config.yaml in {repo_root}, and `scratch.enabled` is "
            f"false in {gpath}.\nRun `jailbee config init` to create one."
        )
    _refuse_scratch_root(repo_root)
    return scratch_repo_layer(repo_root, gcfg.scratch)


def _synthesize_repo_config(repo_root: Path) -> Config:
    """Build `repo_root`'s config from `global.yaml`'s `scratch:` block.

    Local import: `global_config` imports from this module, so importing it at
    module level would form a cycle (see `_load_config_from_repo_raw`).
    """
    from jailbee.global_config import default_global_config_path

    raw = synthesized_repo_layer(repo_root)
    cfg = _load_config_from_repo_raw(
        raw,
        repo_root / REPO_CONFIG_DIRS[0] / "config.yaml",
        origin=f"{default_global_config_path()}{SCRATCH_ORIGIN_SUFFIX}",
    )
    cfg._synthetic = True
    return cfg


def load_repo_config_unsanitized(repo_root: Path) -> Config:
    """`load_config_unsanitized` for a repo root, synthesizing when it has no file.

    For `jailbee config validate`, which must still report an `ls:`/`dashboard:`
    column typo as an error — the same reason the file-backed loader comes in
    two flavours.
    """
    path = repo_config_path_warned(repo_root)
    if path is None:
        return _synthesize_repo_config(repo_root)
    return load_config_unsanitized(path)


def load_repo_config(repo_root: Path) -> Config:
    """`repo_root`'s config, synthesized when it has no `.jailbee/config.yaml`.

    The loader every command should use. A directory with a config file behaves
    exactly as before (`load_config`); one without gets a config built from
    `global.yaml`'s `scratch:` block — see `_synthesize_repo_config` and
    `scratch_repo_layer`. `ConfigNotFoundError` is still raised when
    `scratch.enabled` is false, so disabling the feature restores the previous
    behaviour everywhere at once.
    """
    path = repo_config_path_warned(repo_root)
    if path is None:
        return _sanitize_columns(_synthesize_repo_config(repo_root))
    return load_config(path)
