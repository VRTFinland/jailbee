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

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
)

from jailbee.config.common import (
    _HOST_LEVEL_KEYS as _HOST_LEVEL_KEYS,
)
from jailbee.config.common import (
    CONTAINER_USERNAME as CONTAINER_USERNAME,
)
from jailbee.config.common import (
    PathExpanded as PathExpanded,
)
from jailbee.config.common import (
    _copy as _copy,
)
from jailbee.config.common import (
    _expand as _expand,
)
from jailbee.config.common import (
    _parse_yaml_text as _parse_yaml_text,
)
from jailbee.config.common import (
    _read_yaml_or_empty as _read_yaml_or_empty,
)
from jailbee.config.common import (
    _split_host_keys as _split_host_keys,
)
from jailbee.config.common import (
    deep_merge as deep_merge,
)
from jailbee.config.errors import ConfigError as ConfigError
from jailbee.config.errors import ConfigNotFoundError as ConfigNotFoundError
from jailbee.config.models_agents import (
    _AGENT_NAME_RE as _AGENT_NAME_RE,
)
from jailbee.config.models_agents import (
    _MAX_PR_PROMPT_LEN as _MAX_PR_PROMPT_LEN,
)
from jailbee.config.models_agents import (
    AgentConfig as AgentConfig,
)
from jailbee.config.models_agents import (
    AgentSharedMount as AgentSharedMount,
)
from jailbee.config.models_agents import (
    Autostart as Autostart,
)
from jailbee.config.models_agents import (
    AutostartStep as AutostartStep,
)
from jailbee.config.models_agents import (
    ClaudeAgentConfig as ClaudeAgentConfig,
)
from jailbee.config.models_agents import (
    DockerRegistryMirrorRepoConfig as DockerRegistryMirrorRepoConfig,
)
from jailbee.config.models_agents import (
    GithubConfig as GithubConfig,
)
from jailbee.config.models_behaviour import (
    BootConfig as BootConfig,
)
from jailbee.config.models_behaviour import (
    ConfirmConfig as ConfirmConfig,
)
from jailbee.config.models_behaviour import (
    Defaults as Defaults,
)
from jailbee.config.models_behaviour import (
    DestroyConfig as DestroyConfig,
)
from jailbee.config.models_behaviour import (
    NewConfig as NewConfig,
)
from jailbee.config.models_behaviour import (
    PullConfig as PullConfig,
)
from jailbee.config.models_behaviour import (
    PushConfig as PushConfig,
)
from jailbee.config.models_columns import (
    _COLUMN_DEFAULT as _COLUMN_DEFAULT,
)
from jailbee.config.models_columns import (
    DASHBOARD_DEFAULT_HIDE as DASHBOARD_DEFAULT_HIDE,
)
from jailbee.config.models_columns import (
    ColumnConfig as ColumnConfig,
)
from jailbee.config.models_columns import (
    _columns_already_sanitized as _columns_already_sanitized,
)
from jailbee.config.models_columns import (
    _known_ls_field_names as _known_ls_field_names,
)
from jailbee.config.models_columns import (
    sanitize_column_blocks as sanitize_column_blocks,
)
from jailbee.config.models_columns import (
    validate_column_blocks as validate_column_blocks,
)
from jailbee.config.models_golden import (
    _APT_PACKAGE_NAME_RE as _APT_PACKAGE_NAME_RE,
)
from jailbee.config.models_golden import (
    _DEFAULT_NODE_MAJOR as _DEFAULT_NODE_MAJOR,
)
from jailbee.config.models_golden import (
    _JAVA_STACK_RE as _JAVA_STACK_RE,
)
from jailbee.config.models_golden import (
    _RESERVED_PROVISION_ENV_KEYS as _RESERVED_PROVISION_ENV_KEYS,
)
from jailbee.config.models_golden import (
    Golden as Golden,
)
from jailbee.config.models_golden import (
    IdeName as IdeName,
)
from jailbee.config.models_golden import (
    Stacks as Stacks,
)
from jailbee.config.models_host import (
    _CACHE_NAME_RE as _CACHE_NAME_RE,
)
from jailbee.config.models_host import (
    _PREFIX_RE as _PREFIX_RE,
)
from jailbee.config.models_host import (
    POOL_PRESETS as POOL_PRESETS,
)
from jailbee.config.models_host import (
    ContainerConfig as ContainerConfig,
)
from jailbee.config.models_host import (
    ContainerUser as ContainerUser,
)
from jailbee.config.models_host import (
    HostDevice as HostDevice,
)
from jailbee.config.models_host import (
    HostMount as HostMount,
)
from jailbee.config.models_host import (
    HostPort as HostPort,
)
from jailbee.config.models_host import (
    OptionalMount as OptionalMount,
)
from jailbee.config.models_host import (
    PoolPreset as PoolPreset,
)
from jailbee.config.models_host import (
    PoolSpec as PoolSpec,
)
from jailbee.config.models_host import (
    SharedCache as SharedCache,
)
from jailbee.config.models_host import (
    _default_shared_caches as _default_shared_caches,
)
from jailbee.config.models_host import (
    _jetbrains_shared_caches as _jetbrains_shared_caches,
)
from jailbee.config.models_net import (
    _CREDENTIAL_GROUP_RE as _CREDENTIAL_GROUP_RE,
)
from jailbee.config.models_net import (
    GITHUB_API_HOSTS as GITHUB_API_HOSTS,
)
from jailbee.config.models_net import (
    JETBRAINS_AI_HOSTS as JETBRAINS_AI_HOSTS,
)
from jailbee.config.models_net import (
    JETBRAINS_LICENSE_HOSTS as JETBRAINS_LICENSE_HOSTS,
)
from jailbee.config.models_net import (
    LOOSE_TTL_PRESETS as LOOSE_TTL_PRESETS,
)
from jailbee.config.models_net import (
    NET_DESCRIPTIONS as NET_DESCRIPTIONS,
)
from jailbee.config.models_net import (
    OFFLINE_REMOVED_MSG as OFFLINE_REMOVED_MSG,
)
from jailbee.config.models_net import (
    ClaudeCredentials as ClaudeCredentials,
)
from jailbee.config.models_net import (
    LooseAutoRevert as LooseAutoRevert,
)
from jailbee.config.models_net import (
    _reject_offline as _reject_offline,
)
from jailbee.config.models_net import (
    format_loose_after as format_loose_after,
)
from jailbee.config.models_net import (
    parse_loose_ttl as parse_loose_ttl,
)
from jailbee.config.models_tools import (
    _DEFAULT_CHROME_HOST_PATH as _DEFAULT_CHROME_HOST_PATH,
)
from jailbee.config.models_tools import (
    ChromeConfig as ChromeConfig,
)
from jailbee.config.models_tools import (
    GpgConfig as GpgConfig,
)
from jailbee.config.models_tools import (
    JetbrainsConfig as JetbrainsConfig,
)
from jailbee.config.models_tools import (
    SshConfig as SshConfig,
)
from jailbee.config.models_tools import (
    TerminalConfig as TerminalConfig,
)
from jailbee.config.models_tools import (
    TerminalKittyConfig as TerminalKittyConfig,
)
from jailbee.config.models_tools import (
    _kitty_terminfo_candidates as _kitty_terminfo_candidates,
)
from jailbee.config.models_tools import (
    resolve_kitty_terminfo_path as resolve_kitty_terminfo_path,
)
from jailbee.config.retired import (
    _check_agents_spelling as _check_agents_spelling,
)
from jailbee.config.retired import (
    _check_pull_migration as _check_pull_migration,
)
from jailbee.config.retired import (
    _check_retired_keys as _check_retired_keys,
)
from jailbee.config.retired import (
    _label_spellings as _label_spellings,
)
from jailbee.constants import SHARED_SUBDIRS
from jailbee.git import DEFAULT_REMOTE, detect_default_branch, detect_upstream_remote
from jailbee.paths import xdg_data_home

if TYPE_CHECKING:
    from jailbee.global_config import GlobalConfig


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


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    container_user: ContainerUser = ContainerUser()
    container: ContainerConfig = ContainerConfig()
    shared_dir: PathExpanded | None = None
    # Computed on the load path from the host-level `claude_credentials`
    # block, like repo_root / default_branch / container_prefix. Never a YAML
    # key on either layer: `load_config_from_text` refuses both this name and
    # `claude_credentials` in a repo config. None = this repo keeps its own
    # credential inside its config home.
    claude_credentials_dir: Path | None = None
    host_mounts: list[HostMount] = []
    host_devices: list[HostDevice] = []
    host_ports: list[HostPort] = []
    optional_mounts: dict[str, OptionalMount] = {}
    share_local: bool = Field(
        default=True,
        description=(
            "When true, and a directory `<repo_root>/.local` exists, "
            "RW-bind-mount it into each new container at "
            "`~/<container_prefix>/.local` as a host<->container file-transfer "
            "channel. Presence-triggered: an absent dir is a silent skip; "
            "the dir is never auto-created. Skipped in --mount mode (the "
            "full-repo RW bind already exposes it). Set false to disable."
        ),
    )
    shared_caches: list[SharedCache] = Field(
        default_factory=_default_shared_caches,
    )
    pooled_caches: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Per-cache override of pooling. Key is a `shared_caches` name; "
            "true pools it using POOL_PRESETS[name], false keeps the shared "
            "mount. Absent keys follow the preset's own default_on. A dict "
            "rather than a list so global.yaml and the repo config deep-merge "
            "per key instead of appending."
        ),
    )
    egress_allow: list[str] = []
    defaults: Defaults = Defaults()
    golden: Golden = Golden()
    gpg: GpgConfig = GpgConfig()
    ssh: SshConfig = SshConfig()
    jetbrains: JetbrainsConfig = JetbrainsConfig()
    chrome: ChromeConfig = ChromeConfig()
    agents: dict[str, AgentConfig] = Field(default_factory=dict)
    github: GithubConfig = GithubConfig()
    terminal: TerminalConfig = TerminalConfig()
    autostart: Autostart = Autostart()
    docker_registry_mirror: DockerRegistryMirrorRepoConfig = DockerRegistryMirrorRepoConfig()
    loose_auto_revert: LooseAutoRevert = LooseAutoRevert()
    # Both default to empty so that, unset, `effective_*_columns` returns the
    # global value untouched. The repo's block overrides the global one
    # field-by-field, like loose_auto_revert.
    ls: ColumnConfig = ColumnConfig()
    dashboard: ColumnConfig = ColumnConfig()
    confirm: ConfirmConfig = ConfirmConfig()
    pull: PullConfig = PullConfig()
    push: PushConfig = PushConfig()
    new: NewConfig = NewConfig()
    destroy: DestroyConfig = DestroyConfig()
    boot: BootConfig = BootConfig()
    container_prefix: str = ""
    after_new: Literal["shell", "tmux", "none"] = Field(
        default="none",
        description=(
            "After a successful `jailbee new`, automatically attach to the new "
            "container. 'tmux' attaches to the autostart tmux session "
            "(creating it on demand), 'shell' opens an interactive bash "
            "login shell, 'none' (default) returns to the host prompt. "
            "Override per-invocation with `jailbee new --attach <mode>` or "
            "`jailbee new --no-attach`."
        ),
    )

    # Computed at load time, not from YAML. Defaults satisfy the type checker;
    # load_config() always overwrites them.
    repo_root: Path = Path()
    default_branch: str = "main"
    # The host remote jailbee treats as the upstream. Detected rather than
    # configured, for the same reason `default_branch` is: git already knows,
    # and a submodule may answer differently from its superproject. See
    # `git.detect_upstream_remote`.
    upstream_remote: str = DEFAULT_REMOTE

    # Set once by `load_config()`: the `sanitize_column_blocks` fixes it made
    # to `ls`/`dashboard`, if any (empty otherwise — including for a `Config`
    # built any other way, e.g. `Config.model_validate()` in tests). A
    # private attribute rather than a model field on purpose: it must not
    # appear in `jailbee config show`'s `model_dump()`, and it carries no YAML
    # meaning of its own. See `column_warnings()` and `load_config`.
    _column_warnings: list[str] = PrivateAttr(default_factory=list)

    def column_warnings(self) -> list[str]:
        """Column-block fixes `load_config()` made, for the caller to surface.

        `config.py` never prints; `cli._load_or_exit()` is the one place
        these get shown, via `tui.warn`, mirroring how `cli._load_global()`
        surfaces the equivalent list for `global.yaml`.
        """
        return list(self._column_warnings)

    @field_validator("egress_allow")
    @classmethod
    def _validate_egress_allow(cls, v: list[str]) -> list[str]:
        # Validate each entry parses cleanly. Local import avoids
        # config <-> egress circular dependency at module load.
        from jailbee.egress import parse_egress_entry

        for raw in v:
            parse_egress_entry(raw)  # raises ValueError on bad input
        return v

    @field_validator("host_ports")
    @classmethod
    def _validate_host_port_names(cls, v: list[HostPort]) -> list[HostPort]:
        seen: set[str] = set()
        for entry in v:
            if entry.name in seen:
                raise ValueError(f"duplicate host_ports name: {entry.name!r}")
            seen.add(entry.name)
        return v

    @field_validator("agents", mode="before")
    @classmethod
    def _validate_agents(cls, v: object) -> dict[str, AgentConfig]:
        """Dispatch `agents.claude` through `ClaudeAgentConfig`, the rest
        through the generic `AgentConfig`, since a single `dict[str, Model]`
        field can't express a per-key model choice on its own.

        The already-constructed-model branch re-validates a plain
        `AgentConfig` sitting under the `claude` key rather than passing it
        through: `Config.claude` only recognises a `ClaudeAgentConfig` and
        falls back to a disabled default otherwise, so letting a plain
        `AgentConfig` stand would split the config in two — `agents["claude"]`
        enabled (mounts, egress and install all active) while `cfg.claude`
        reports disabled (`pr_ai`, `claude_skills`, `apply` and `doctor` all
        see Claude off). Not reachable from YAML, since the dict branch below
        already dispatches on the key, but it is the shape a caller
        constructing `Config` in Python will write.
        """
        if not isinstance(v, dict):
            raise ValueError("agents must be a mapping of agent name to settings")
        result: dict[str, AgentConfig] = {}
        for name, entry in v.items():
            if isinstance(entry, AgentConfig):
                if name == "claude" and not isinstance(entry, ClaudeAgentConfig):
                    entry = ClaudeAgentConfig.model_validate(entry.model_dump())
                result[name] = entry
                continue
            if not isinstance(entry, dict):
                raise ValueError(f"agents.{name} must be a mapping")
            model_cls: type[AgentConfig] = ClaudeAgentConfig if name == "claude" else AgentConfig
            result[name] = model_cls.model_validate(entry)
        return result

    @property
    def claude(self) -> ClaudeAgentConfig:
        """The `agents.claude` entry, or a disabled default when absent.

        Kept so `pr_ai`, `claude_skills`, `doctor`, `apply` and `cli` can go on
        reading `cfg.claude.*`. Precedent: `repo_root`, `default_branch` and
        `container_prefix` are also derived rather than YAML keys.

        Read-only on purpose. `model_copy(update={"claude": ...})` cannot work
        here — a property shadows the instance dict that update writes — so
        tests must go through `tests.conftest.with_agent`.

        The fallback branch allocates a fresh `ClaudeAgentConfig` on every
        call: `cfg.claude is cfg.claude` is `False` when `"claude"` is absent
        from `agents`, and `cfg.claude.autostart = True` silently mutates a
        throwaway instead of `cfg`. A second silent-no-op shape alongside the
        `model_copy` one above — harmless today because nothing does this,
        but don't rely on the returned object's identity or on mutating it.
        """
        entry = self.agents.get("claude")
        if isinstance(entry, ClaudeAgentConfig):
            return entry
        return ClaudeAgentConfig(command="claude")

    def effective_egress_allow(self) -> list[str]:
        """User's `egress_allow` plus any feature-driven auto-additions.

        Appends:
        - `JETBRAINS_LICENSE_HOSTS` when `jetbrains.enabled`
          (account/license activation, plugin marketplace, installer
          CDNs, framework dependency config). Independent of
          `userprefs_from_host`: the IDE needs these endpoints to
          activate its license regardless of where the user-prefs
          directory lives.
        - `JETBRAINS_AI_HOSTS` when `jetbrains.enabled` and
          `jetbrains.ai_enabled` (AI Assistant backend).
        - each enabled agent's `egress` (see `agents.enabled_agent_specs`) —
          for `claude` this is `CLAUDE_API_HOSTS` plus, when
          `claude.plugins_enabled`, `CLAUDE_PLUGIN_HOSTS`.
        - `GITHUB_API_HOSTS` when `github.enabled` (GitHub CLI API access).

        Deduplicates while preserving user-entry order.
        """
        from jailbee.agents import enabled_agent_specs

        result = list(self.egress_allow)
        existing = set(result)

        def _append(hosts: tuple[str, ...]) -> None:
            for host in hosts:
                if host not in existing:
                    result.append(host)
                    existing.add(host)

        if self.jetbrains.enabled:
            _append(JETBRAINS_LICENSE_HOSTS)
            if self.jetbrains.ai_enabled:
                _append(JETBRAINS_AI_HOSTS)
        for spec in enabled_agent_specs(self):
            _append(spec.egress)
        if self.github.enabled:
            _append(GITHUB_API_HOSTS)
        return result

    def effective_shared_caches(self) -> list[SharedCache]:
        """User's `shared_caches` plus auto-adds for enabled integrations.

        Mirrors `effective_host_mounts` precedence: a user-supplied entry
        whose `name` matches an auto-add suppresses the auto-add. The
        `golden.stacks` caches (gradle+m2 for java, npm+pnpm-store for
        node — see `Stacks.shared_caches`) are folded in first, ahead of
        the integration auto-adds. Then each enabled agent's mounts are
        folded in (see `agents.enabled_agent_specs`) — for `claude` this is
        `claude` + `claude-install` — followed by `chrome-profile` when
        `chrome.enabled`, then `jetbrains-config` + `jetbrains-data` when
        `jetbrains.enabled`. Finally each entry's `pool` is resolved per
        `pooled_caches` / `POOL_PRESETS` (see `_resolve_pool`) before the
        list is returned.
        """
        from jailbee.agents import enabled_agent_specs

        result: list[SharedCache] = list(self.shared_caches)
        existing = {c.name for c in result}

        def _extend(extras: list[SharedCache]) -> None:
            for cache in extras:
                if cache.name not in existing:
                    result.append(cache)
                    existing.add(cache.name)

        _extend(self.golden.stacks.shared_caches())
        for spec in enabled_agent_specs(self):
            _extend(list(spec.shared))
        if self.chrome.enabled:
            _extend(
                [
                    SharedCache(
                        name="chrome-profile",
                        host_subpath="chrome-pool",
                        container_path="~/.config/google-chrome",
                    )
                ]
            )
        if self.jetbrains.enabled:
            _extend(
                _jetbrains_shared_caches(
                    self.container_prefix,
                    share_idea=self.jetbrains.share_idea,
                )
            )
        return [self._resolve_pool(c) for c in result]

    def _resolve_pool(self, cache: SharedCache) -> SharedCache:
        """Attach the preset `PoolSpec` when this cache should be pooled."""
        if cache.pool is not None:
            return cache  # explicit block always wins
        flag = self.pooled_caches.get(cache.name)
        preset = POOL_PRESETS.get(cache.name)
        if preset is None:
            return cache  # `true` without a preset is rejected at load time
        # `false` on a `pool_only` preset is a load-time ConfigError; honouring
        # it here anyway would mount the pool root into every container, so a
        # Config built without validation (tests, `model_copy`) still pools.
        if flag is False and not preset.pool_only:
            return cache
        if flag is True or preset.default_on:
            return cache.model_copy(update={"pool": preset.spec})
        return cache

    def effective_loose_auto_revert(
        self,
        gcfg: GlobalConfig,
    ) -> LooseAutoRevert | None:
        """Resolve the per-repo policy by merging fields explicitly set in
        this repo's YAML on top of the global default.

        Returns ``None`` when the effective ``enabled`` is False; callers
        treat ``None`` as "no auto-revert in this repo".
        """
        base = gcfg.loose_auto_revert
        repo = self.loose_auto_revert
        overrides = {f: getattr(repo, f) for f in repo.model_fields_set}
        merged = base.model_copy(update=overrides)
        return merged if merged.enabled else None

    def _effective_columns(self, base: ColumnConfig, repo: ColumnConfig) -> ColumnConfig:
        """Merge fields explicitly set in this repo's YAML over the global block."""
        overrides = {f: getattr(repo, f) for f in repo.model_fields_set}
        return base.model_copy(update=overrides)

    def effective_ls_columns(self, gcfg: GlobalConfig) -> ColumnConfig:
        """Column preference for ``jailbee ls``: repo block over global block."""
        return self._effective_columns(gcfg.ls, self.ls)

    def effective_host_mounts(self) -> list[HostMount]:
        """User's host_mounts plus auto-additions driven by gpg / ssh /
        jetbrains blocks. Manual entries win: if a user-supplied entry
        has the same container path as an auto-add, the auto-add is
        skipped. Ordering: manual entries first (preserving user order),
        then auto-adds in a fixed order.
        """
        result: list[HostMount] = list(self.host_mounts)
        existing_containers = {str(m.container) for m in result}

        auto: list[HostMount] = []
        if self.gpg.enabled:
            auto.append(
                HostMount(
                    host=Path.home() / ".gnupg",
                    container="/home/dev/.gnupg",
                    readonly=True,
                )
            )
        if self.jetbrains.enabled and self.jetbrains.userprefs_from_host:
            up = Path.home() / ".java" / ".userPrefs" / "jetbrains"
            auto.append(HostMount(host=up, container=str(up), readonly=False))
        if self.jetbrains.enabled and self.jetbrains.toolbox_host_path is not None:
            auto.append(
                HostMount(
                    host=self.jetbrains.toolbox_host_path,
                    container="/opt/jetbrains-toolbox",
                    readonly=True,
                )
            )
        if self.chrome.enabled and self.chrome.host_path is not None:
            auto.append(
                HostMount(
                    host=self.chrome.host_path,
                    container="/opt/google/chrome",
                    readonly=True,
                )
            )
        kitty = self.terminal.kitty
        if kitty.enabled and (
            resolved := resolve_kitty_terminfo_path(explicit=kitty.host_terminfo_path)
        ):
            auto.append(
                HostMount(
                    host=resolved,
                    container="/usr/share/terminfo/x/xterm-kitty",
                    readonly=True,
                )
            )

        for entry in auto:
            if str(entry.container) not in existing_containers:
                result.append(entry)
                existing_containers.add(str(entry.container))
        return result

    def share_local_mount(self) -> HostMount | None:
        """The auto-shared ``<repo>/.local`` RW bind, or None.

        Returns a HostMount when ``share_local`` is enabled and
        ``<repo_root>/.local`` exists as a directory; otherwise None.
        Presence-triggered: an absent dir is a silent skip.

        Deliberately NOT folded into ``effective_host_mounts``: that method
        feeds the binds profile, ``validate_runtime`` and ``jailbee ls``, and has
        no per-container context. This mount needs a filesystem-presence check
        plus a mount-mode skip and a git-exclude side-effect handled at attach
        time (see ``lifecycle._attach_share_local``).
        """
        if not self.share_local:
            return None
        src = self.repo_root / ".local"
        if not src.is_dir():
            return None
        return HostMount(
            host=src,
            container=f"/home/{CONTAINER_USERNAME}/{self.container_prefix}/.local",
            readonly=False,
        )

    def validate_runtime(self) -> list[str]:
        """Validate filesystem paths exist. Returns list of issues (empty if OK)."""
        issues: list[str] = []
        if not self.repo_root.exists():
            issues.append(f"repo_root does not exist: {self.repo_root}")
        for i, mount in enumerate(self.host_mounts):
            if not mount.host.exists():
                issues.append(f"host_mounts[{i}].host does not exist: {mount.host}")
        for i, device in enumerate(self.host_devices):
            if not Path(device.effective_source).exists():
                issues.append(
                    f"host_devices[{i}].source does not exist: "
                    f"{device.effective_source} — device will be skipped"
                )
        for name, opt_mount in self.optional_mounts.items():
            if not opt_mount.host.exists():
                issues.append(f"optional_mounts[{name}].host does not exist: {opt_mount.host}")
        known_mounts = set(self.optional_mounts)
        for trigger_name in ("on_create", "on_start"):
            for step in getattr(self.autostart, trigger_name):
                for m in step.mounts:
                    if m not in known_mounts:
                        issues.append(
                            f"autostart.{trigger_name}[{step.name}].mounts "
                            f"references unknown optional_mount: '{m}'"
                        )
        if self.loose_auto_revert.enabled:
            # The schema types `after` as `str | int` and stops there, so an
            # unparseable value (or one over the 24h cap) loads cleanly and
            # only fails when something parses it — `jailbee net loose`, which
            # refuses to run until it is fixed. This is the one command that
            # can say so first. Global and repo values both land here: the
            # global block is merged into `Config` on the load path.
            try:
                self.loose_auto_revert.duration()
            except ValueError as e:
                issues.append(f"loose_auto_revert.after is unusable: {e}")
        if self.jetbrains.enabled and self.jetbrains.userprefs_from_host:
            host_path = Path.home() / ".java" / ".userPrefs" / "jetbrains"
            if not host_path.is_dir():
                issues.append(
                    f"jetbrains.userprefs_from_host=true but host path "
                    f"does not exist: {host_path}. Run a JetBrains IDE on "
                    f"the host once to create it, or set "
                    f"jetbrains.userprefs_from_host: false in .jailbee/config.yaml."
                )
        if self.jetbrains.enabled and self.jetbrains.toolbox_host_path is not None:
            if not self.jetbrains.toolbox_host_path.is_dir():
                issues.append(
                    f"jetbrains.toolbox_host_path does not exist: "
                    f"{self.jetbrains.toolbox_host_path}. Install JetBrains "
                    f"Toolbox or set jetbrains.toolbox_host_path: null."
                )
        if self.chrome.enabled and self.chrome.host_path is not None:
            if not self.chrome.host_path.is_dir():
                issues.append(
                    f"chrome.host_path does not exist: {self.chrome.host_path}. "
                    f"Install google-chrome-stable or set chrome.host_path: null."
                )
        kitty = self.terminal.kitty
        if kitty.enabled:
            if kitty.host_terminfo_path is not None and not kitty.host_terminfo_path.exists():
                issues.append(
                    f"terminal.kitty.host_terminfo_path does not exist: "
                    f"{kitty.host_terminfo_path}. Point at an existing xterm-kitty "
                    f"terminfo file or set host_terminfo_path: null to autodetect."
                )
            elif (
                kitty.enabled is True
                and kitty.host_terminfo_path is None
                and resolve_kitty_terminfo_path(explicit=None) is None
            ):
                searched = ", ".join(str(p) for p in _kitty_terminfo_candidates())
                issues.append(
                    f"terminal.kitty.enabled=true but no kitty terminfo file found. "
                    f"Searched: {searched}. Install kitty-terminfo (apt/dnf), set "
                    f"terminal.kitty.host_terminfo_path explicitly, or use "
                    f"enabled: auto."
                )
        if self.github.enabled:
            secret = self.github.api_tokens.get(self.container_prefix)
            if secret is not None and not secret.get_secret_value().strip():
                issues.append(f"github.api_tokens['{self.container_prefix}'] is empty")

        seen_subpaths: dict[str, tuple[str, str]] = {}
        for name, agent in self.agents.items():
            if not _AGENT_NAME_RE.fullmatch(name):
                issues.append(
                    f"agent name {name!r} must match [a-z0-9-]+ — it becomes a "
                    f"tmux window name and a doctor label"
                )
            if agent.autostart and not agent.enabled:
                # `claude` keeps the extra parenthetical explaining *why* the
                # gate exists — the only agent with a documented shared-mount
                # + egress side effect on `enabled`. This is the single
                # source of this check: a legacy `claude:` block resolves to
                # `agents["claude"]` via `resolve_agents_raw` before
                # validation, so a second, claude-specific check here would
                # report the same misconfiguration twice.
                detail = (
                    f" (shared ~/.claude mount and Anthropic egress are gated "
                    f"by agents.{name}.enabled)"
                    if name == "claude"
                    else ""
                )
                issues.append(
                    f"agents.{name}.autostart=true requires agents.{name}.enabled=true{detail}"
                )
            if agent.enabled and not agent.command.strip():
                issues.append(f"agents.{name}.enabled=true requires a non-empty `command`")
            if not agent.enabled:
                continue
            for shared_mount in agent.shared:
                if shared_mount.subpath in SHARED_SUBDIRS:
                    issues.append(
                        f"agents.{name}.shared subpath {shared_mount.subpath!r} collides with "
                        f"a built-in shared subdir"
                    )
                prior = seen_subpaths.get(shared_mount.subpath)
                signature = (shared_mount.path, shared_mount.type)
                if prior is not None and prior != signature:
                    issues.append(
                        f"shared subpath {shared_mount.subpath!r} is claimed twice with "
                        f"different targets ({prior} vs {signature}) — two agents may "
                        f"share an identical mount, but not a conflicting one"
                    )
                seen_subpaths[shared_mount.subpath] = signature

        if self.golden.python:
            issues.append(
                "golden.python is deprecated and ignored; the container "
                "Python comes from the base image (golden.ubuntu_version). "
                "Remove the key from the golden: block."
            )

        # Column names are validated outside the model (see
        # `validate_column_blocks`) because the canonical names come from
        # `lifecycle`, which `config.py` cannot import at module level. This
        # covers the *repo* blocks only; the matching blocks in
        # `global.yaml` are checked by `jailbee config validate` too, via
        # `global_config.global_config_issues`. Neither loader recovers from
        # a typo at *this* validation point any more — `load_config` (repo)
        # and `load_global_config` (global) both recover from one via
        # `sanitize_column_blocks` for ordinary loading, since it is a
        # personal display preference and must not break an unrelated
        # command, but `cli.config_validate` calls `load_config_unsanitized`
        # (not `load_config`) precisely so `self.ls`/`self.dashboard` here
        # are still the raw, unrecovered blocks and a typo is still reported
        # as an error.
        issues.extend(validate_column_blocks([("ls", self.ls), ("dashboard", self.dashboard)]))
        if "dashboard" in self.model_fields_set:
            issues.append(
                "dashboard: deprecated and ignored — the dashboards remember their "
                "own columns now (press F2 in `jailbee dashboard`, or View ▸ Columns "
                "in the GUI). A repo-level block is also not seeded at all: only "
                "~/.config/jailbee/global.yaml is imported, into each dashboard's own "
                "settings the first time you open that dashboard after upgrading. "
                "This repo-level block can be deleted."
            )
        return issues


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


def _build_config_from_dict(raw: dict[str, object], config_path: Path) -> Config:
    """Validate a raw merged dict and populate computed Config fields.

    Used by load_config to build the final Config from a (possibly merged)
    raw dict. Computed fields (repo_root, default_branch, container_prefix,
    shared_dir, golden.alias) are set after Pydantic validation. Cross-field
    invariants (prefix regex, reserved env keys, shared_caches uniqueness,
    autostart step-name uniqueness) are checked here as well.
    """
    try:
        raw = resolve_agents_raw(raw)
    except ConfigError as e:
        raise ConfigError(f"Config validation failed in {config_path}:\n{e}") from e
    _check_retired_keys(raw)
    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"Config validation failed in {config_path}:\n{e}") from e

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
            f"[a-z0-9][a-z0-9-]*. Set `container_prefix:` in {config_path} explicitly."
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
    # Local import avoids a circular dependency at module load: global_config
    # already imports from this module's ConfigError, and importing
    # default_global_config_path at module top would form a cycle.
    from jailbee.global_config import default_global_config_path

    global_raw = _read_yaml_or_empty(default_global_config_path())
    _check_retired_keys(global_raw)
    host_raw, global_for_merge = _split_host_keys(global_raw)
    repo_raw = _parse_yaml_text(text, str(path))
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
    cfg = _build_config_from_dict(merged, path)

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
    cfg = load_config_unsanitized(path)

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
