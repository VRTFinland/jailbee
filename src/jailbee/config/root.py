"""The `Config` model: per-repo configuration, validated and merged.

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
    field_validator,
)

from jailbee.config.common import CONTAINER_USERNAME, PathExpanded
from jailbee.config.models_agents import (
    _AGENT_NAME_RE,
    AgentConfig,
    Autostart,
    ClaudeAgentConfig,
    DockerRegistryMirrorRepoConfig,
    GithubConfig,
)
from jailbee.config.models_behaviour import (
    BootConfig,
    ConfirmConfig,
    Defaults,
    DestroyConfig,
    NewConfig,
    PullConfig,
    PushConfig,
)
from jailbee.config.models_columns import ColumnConfig, validate_column_blocks
from jailbee.config.models_golden import Golden
from jailbee.config.models_host import (
    POOL_PRESETS,
    ContainerConfig,
    ContainerUser,
    HostDevice,
    HostMount,
    HostPort,
    OptionalMount,
    SharedCache,
    _default_shared_caches,
    _jetbrains_shared_caches,
)
from jailbee.config.models_net import (
    GITHUB_API_HOSTS,
    JETBRAINS_AI_HOSTS,
    JETBRAINS_LICENSE_HOSTS,
    LooseAutoRevert,
)
from jailbee.config.models_tools import (
    ChromeConfig,
    GpgConfig,
    JetbrainsConfig,
    SshConfig,
    TerminalConfig,
    _kitty_terminfo_candidates,
    resolve_kitty_terminfo_path,
)
from jailbee.constants import SHARED_SUBDIRS
from jailbee.git import DEFAULT_REMOTE

if TYPE_CHECKING:
    from jailbee.global_config import GlobalConfig


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
