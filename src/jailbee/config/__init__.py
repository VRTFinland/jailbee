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

import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
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

# An agent name becomes a tmux window name and a `jailbee doctor` label —
# kept to the safest common subset of what both accept. It does *not* reach
# any Incus device name: those derive from each `shared[].subpath` via
# `device_name()`. The two only coincide because every shipped preset happens
# to name its subpath after the agent.
_AGENT_NAME_RE = re.compile(r"[a-z0-9-]+")

# Provisioning env vars set automatically by `jailbee base build`. Users may not
# override these via `golden.provision_env` — built-in install.sh relies on
# them, and silently letting the user shadow them produces confusing failures.
_RESERVED_PROVISION_ENV_KEYS = frozenset(
    {
        "CONTAINER_UID",
        "CONTAINER_GID",
        "JAVA_PACKAGE",
        "NODE_MAJOR",
        "EXTRA_APT_PACKAGES",
        "JAILBEE_USER_HOME",
        "JAILBEE_PROVISION_DIR",
    }
)

# Debian package-name grammar (simplified): start with [a-z0-9], then
# [a-z0-9+\-.]. We enforce this on `golden.extra_apt_packages` because the
# values are passed unquoted to `apt-get install` inside install.sh — letting
# whitespace or shell metacharacters through would amount to shell injection.
_APT_PACKAGE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+\-.]*$")

# Java stack vendor and version format: openjdk-N or corretto-N (N is a major version).
_JAVA_STACK_RE = re.compile(r"^(openjdk|corretto)-\d+$")

# Default Node.js major version when node=True in stacks. Mirrors Golden.node default of 24.
_DEFAULT_NODE_MAJOR = 24


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


class ConfirmConfig(BaseModel):
    """Policy for confirming a bridge operation whose target jailbee chose itself.

    ``jailbee git push`` / ``pull`` / ``checkout`` settle on the single existing
    container without showing a picker. Nothing then states which host branch
    travels or which container branch it lands on — both can come from config
    (``push.default_source``, ``push.push_from``) or from the container's
    ``user.jailbee.base_branch`` label rather than from the command line.

    With ``auto_target`` on (the default), those commands print a plan block and
    ask before mutating anything. Overridable per invocation with
    ``--confirm`` / ``--no-confirm``. Off a TTY, ``pull``/``checkout`` still
    print the block and only skip the prompt; ``push`` requires an explicit
    container name off a TTY in the first place, so it never reaches this
    confirmation there.
    """

    model_config = ConfigDict(extra="forbid")
    auto_target: bool = True


class PullConfig(BaseModel):
    """Policy for `jailbee git pull`'s post-merge cleanup prompts.

    Each step independently controls whether the cleanup runs always,
    never, or prompts the user interactively (default).
    """

    model_config = ConfigDict(extra="forbid")
    destroy_container: Literal["prompt", "always", "never"] = "prompt"
    delete_branch: Literal["prompt", "always", "never"] = "prompt"


class PushConfig(BaseModel):
    """Policy for `jailbee git push`'s interactive default-picker.

    Both keys may be ``"ask"`` to open an interactive prompt instead
    of using a baked-in default. Layered: ``~/.config/jailbee/global.yaml``
    is the user-wide default; ``<repo>/.jailbee/config.yaml`` may override
    per-repo via the standard deep-merge pipeline.

    ``default_source`` values:

    * ``"base"`` *(default)* — resolve to each container's recorded base
      branch (``user.jailbee.base_branch`` Incus metadata label), so the host
      pushes exactly what was branched from, per container.
    * ``"default-branch"`` — always use the repo's default branch (e.g.
      ``main``), regardless of which branch the container is on.
    * ``"current"`` — use the host's currently checked-out branch.
    * ``"ask"`` — open an interactive picker every time.

    ``default_source`` picks *which branch*; ``push_from`` picks *which
    copy of it*:

    * ``"origin"`` *(default)* — push ``refs/remotes/origin/<source>``,
      falling back to ``refs/heads/<source>`` when the branch has no
      upstream counterpart. The host's local ``refs/heads/<base>`` only
      advances on ``git pull``, so for any branch the user does not check
      out (typically the base branch) the remote-tracking ref is the
      fresher one. Mirrors ``new.clone_from='origin'``.
    * ``"local"`` — push ``refs/heads/<source>`` first, as ``jailbee`` did
      before, falling back to the remote-tracking ref.

    ``autofetch`` runs ``git fetch origin <source>`` on the host before
    resolving, so the remote-tracking ref is actually current — the
    counterpart of ``new.autofetch``. It only applies in ``"origin"``
    mode and is best-effort: a failure (offline, branch not on origin)
    is reported and the push proceeds with the refs already present.
    ``--pr`` and ``--current`` always resolve locally regardless of
    these keys; see `jailbee git push --help`.
    """

    model_config = ConfigDict(extra="forbid")
    default_action: Literal["merge", "rebase", "plain", "ask"] = "ask"
    default_source: Literal["default-branch", "current", "base", "ask"] = "base"
    push_from: Literal["local", "origin"] = "origin"
    autofetch: bool = True


class NewConfig(BaseModel):
    """Policy for `jailbee new`'s starting-point selection.

    Applies to the default-branch fallback path (no --base, branch does
    not yet exist in the source repo). The `--base` path always uses
    local refs and skips autofetch; `--pr` performs its own up-front
    fetch via `gh` and is unaffected.

    With ``clone_from='origin'``, the new container is checked out at
    ``refs/remotes/origin/<default_branch>`` on the host. If
    ``autofetch=True``, ``jailbee new`` runs ``git fetch origin <branch>``
    on the host before resolving that ref, so the clone reflects the
    upstream tip without the user having to fetch manually first.

    ``submodules`` controls whether git submodules are initialised
    (recursively) inside the new container.
    """

    model_config = ConfigDict(extra="forbid")
    clone_from: Literal["local", "origin"] = "origin"
    autofetch: bool = True
    background: bool = False
    """Run `jailbee new` detached in the background by default. Overridable
    per-invocation with `--background` / `--no-background`."""
    submodules: bool = True
    """Initialize the superproject's git submodules in the container
    (recursively, offline from the host bind mount). Set false to skip."""


class DestroyConfig(BaseModel):
    """Policy for `jailbee destroy`."""

    model_config = ConfigDict(extra="forbid")
    background: bool = False
    """Run `jailbee destroy` detached in the background by default. Overridable
    per-invocation with `--background` / `--no-background`."""


class BootConfig(BaseModel):
    """Policy for `jailbee start` and `jailbee restart`.

    One key for both: what makes either slow is the autostart run that
    follows the boot, and it is the same run.
    """

    model_config = ConfigDict(extra="forbid")
    background: bool = False
    """Run `jailbee start` / `jailbee restart` detached in the background by
    default. Overridable per-invocation with `--background` /
    `--no-background`."""


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory: str = "16GiB"
    cpu: int = 8
    network: Literal["strict", "loose"] = "strict"
    storage_pool: str = "default"

    @field_validator("network", mode="before")
    @classmethod
    def _no_offline(cls, v: object) -> object:
        return _reject_offline(v)


class Stacks(BaseModel):
    """High-level language/tool toggles for the golden image.

    Each enabled stack expands to its provisioning snippet, shared caches,
    and build-env values (see the derivation methods). This is sugar over
    ``golden.enable_snippets`` + ``shared_caches``; those remain the
    low-level escape hatch.
    """

    model_config = ConfigDict(extra="forbid")

    # bool comes first in each union so YAML `true`/`false` bind to bool,
    # not to a coerced int/str.
    java: bool | str = False  # "openjdk-N" | "corretto-N" | True(→openjdk default) | False
    node: bool | int = False  # N | True(→default major) | False
    python: bool = False
    docker: bool = False
    ecr: bool = False

    @field_validator("java")
    @classmethod
    def _validate_java(cls, v: bool | str) -> bool | str:
        if isinstance(v, bool):
            return v
        if not _JAVA_STACK_RE.match(v):
            raise ValueError(
                f"invalid golden.stacks.java: {v!r}. Use 'openjdk-<N>', 'corretto-<N>', or true."
            )
        return v

    @field_validator("node")
    @classmethod
    def _validate_node(cls, v: bool | int) -> bool | int:
        if isinstance(v, bool):
            return v
        if v < 1:
            raise ValueError(
                f"invalid golden.stacks.node: {v!r}. Use a major version >= 1 or true."
            )
        return v

    def _java_vendor_version(self) -> tuple[str, str] | None:
        """(vendor, version) for a pinned ``java`` value, or None when java is
        off or ``True`` (no explicit vendor/version)."""
        if not isinstance(self.java, str):
            return None
        vendor, _, version = self.java.partition("-")
        return vendor, version

    def snippet_names(self) -> list[str]:
        """Bundled available-library base names implied by the enabled stacks."""
        names: list[str] = []
        if self.java:
            vv = self._java_vendor_version()
            names.append("20-corretto" if vv and vv[0] == "corretto" else "20-openjdk")
        if self.node:
            names.append("30-nodejs")
        if self.python:
            names.append("40-python")
        if self.docker:
            names.append("50-docker")
        if self.ecr:
            names.append("80-ecr-helper")
        if self.java and self.docker:
            names.append("90-registry-mirror-ca")
        return names

    def java_package(self) -> str | None:
        """apt package name for the java stack, or None when java is off."""
        if not self.java:
            return None
        vv = self._java_vendor_version()
        if vv is None:  # java is True → distro default JDK
            return "default-jdk"
        vendor, version = vv
        if vendor == "openjdk":
            return f"openjdk-{version}-jdk"
        # corretto — the only other vendor _JAVA_STACK_RE admits
        return f"java-{version}-amazon-corretto-jdk"

    def node_major(self) -> int | None:
        """node major version for the node stack, or None when node is off."""
        if self.node is True:
            return _DEFAULT_NODE_MAJOR
        if self.node is False:
            return None
        return self.node

    def shared_caches(self) -> list[SharedCache]:
        """Language caches implied by the enabled stacks."""
        caches: list[SharedCache] = []
        if self.java:
            caches.append(
                SharedCache(
                    name="gradle",
                    host_subpath="caches/gradle",
                    container_path="~/.gradle",
                )
            )
            caches.append(SharedCache(name="m2", host_subpath="caches/m2", container_path="~/.m2"))
        if self.node:
            caches.append(
                SharedCache(
                    name="npm",
                    host_subpath="caches/npm",
                    container_path="~/.npm",
                )
            )
            caches.append(
                SharedCache(
                    name="pnpm-store",
                    host_subpath="caches/pnpm-store",
                    container_path="~/.local/share/pnpm/store",
                )
            )
        return caches


class Golden(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = ""
    ubuntu_version: str = "26.04"
    java: str = "amazon-corretto-17"
    node: int = 24
    # DEPRECATED — ignored. The container's Python is always the base
    # image's system python3 (its version is a function of ubuntu_version,
    # since the archive ships only one python3.X per release). Kept in the
    # model so a stale `python:` key is a soft, non-blocking deprecation
    # warning (via validate_runtime) rather than a hard extra-field error.
    python: str = ""
    provision_script: Path | None = None
    provision_env: dict[str, str] = {}
    extra_apt_packages: list[str] = Field(default_factory=list)
    disable_snippets: list[str] = Field(default_factory=list)
    enable_snippets: list[str] = Field(default_factory=list)
    stacks: Stacks = Field(default_factory=Stacks)

    @field_validator("extra_apt_packages")
    @classmethod
    def _validate_pkg_names(cls, v: list[str]) -> list[str]:
        for pkg in v:
            if not _APT_PACKAGE_NAME_RE.match(pkg):
                raise ValueError(
                    f"invalid apt package name: {pkg!r}. Must match "
                    r"[a-z0-9][a-z0-9+\-.]*"
                )
        return v


# Supported JetBrains Toolbox launcher names. The Toolbox lays each app out as
# /opt/jetbrains-toolbox/apps/<id>/bin/<launcher>, where the launcher binary
# uses the IDE's short name (e.g. `pycharm` for pycharm-professional, `idea`
# for intellij-idea-ultimate, `studio` for android-studio). gui.open_ide() uses
# this value directly as the `find -name` pattern.
IdeName = Literal[
    "idea",
    "webstorm",
    "pycharm",
    "goland",
    "clion",
    "phpstorm",
    "rider",
    "rubymine",
    "datagrip",
    "rustrover",
    "aqua",
    "dataspell",
    "studio",
]


class GpgConfig(BaseModel):
    """GPG support inside containers.

    When enabled, jailbee RO bind-mounts ~/.gnupg, sets SSH_AUTH_SOCK in
    the base profile to the host gpg-agent's SSH socket, and runs the
    doctor check for that socket.

    Defaults to disabled — host gpg-agent setup is personal, so opt-in
    at the global-config layer rather than ambient-on for every repo.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False


class SshConfig(BaseModel):
    """SSH config inside containers.

    When enabled, jailbee bind-mounts <shared_dir>/ssh as the container
    user's ~/.ssh and enforces 0700 on every `jailbee init`.
    `seed_from_host` (default true) controls whether the first init
    copies host ~/.ssh/{config,known_hosts,config.d/} into the
    shared dir. Private keys, authorized_keys and sockets are
    NEVER seeded — keys come from the host gpg-agent.

    Defaults to disabled — explicit opt-in lives in
    ~/.config/jailbee/global.yaml.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    seed_from_host: bool = True


class JetbrainsConfig(BaseModel):
    """JetBrains IDE integration.

    - `enabled`: master switch. Defaults to false; opt-in via
      ~/.config/jailbee/global.yaml. When true, the strict-mode egress
      allowlist is auto-extended with JetBrains' license/plugin/CDN
      hosts so account activation and plugin updates work out of the
      box. When false, `jailbee ide` errors out, the autostart launch is
      suppressed, and `userprefs_from_host` / `toolbox_host_path`
      auto-mounts and all JetBrains egress entries are skipped
      regardless of their individual values.
    - `ide`: which JetBrains binary `jailbee ide` (no --app) and autostart
      launch. The `IdeName` Literal lists supported launchers.
    - `userprefs_from_host`: opt-in RW bind-mount of
      ~/.java/.userPrefs/jetbrains/ (license tokens) into the
      container. Defaults to false — most users don't need it once
      license-host egress is on. Set to true to reuse host-side
      JetBrains Account login state across containers.
    - `share_idea`: opt-out shared-cache mount that persists project
      JetBrains state (.idea/) across containers of the same source
      repo. Defaults to true. Mounts <shared_dir>/jetbrains-idea over
      ~/<container_prefix>/.idea inside each container. Set to false
      if the source repo tracks .idea/* files in VCS that should not
      be shadowed by the mount. Skipped automatically in --mount mode
      so the host's .idea wins.
    - `ai_enabled`: opt-in switch for JetBrains AI Assistant egress
      hosts. Defaults to false. Has no effect when `enabled` is false.
    - `autostart`: launch the IDE after autostart steps complete.
    - `toolbox_host_path`: host path RO-mounted to /opt/jetbrains-toolbox
      (the container-side path is hardcoded in gui.open_ide). Set to
      None to disable the auto-mount.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    ide: IdeName = "idea"
    userprefs_from_host: bool = False
    share_idea: bool = True
    ai_enabled: bool = False
    autostart: bool = False
    toolbox_host_path: PathExpanded | None = Field(
        default_factory=lambda: Path.home() / ".local" / "share" / "JetBrains" / "Toolbox"
    )


# Default host path for the Chrome install. Matches the Debian/Ubuntu
# google-chrome-stable package layout (binary at
# /opt/google/chrome/google-chrome). gui.open_chrome hardcodes the
# container-side path, so the container-side mount target is fixed even
# when the user changes the source path (e.g. to point at chromium).
_DEFAULT_CHROME_HOST_PATH = Path("/opt/google/chrome")


class ChromeConfig(BaseModel):
    """Chrome integration.

    - `enabled`: master switch. Defaults to false; opt-in via
      ~/.config/jailbee/global.yaml. When false, `jailbee chrome` errors out
      and the autostart launch is suppressed regardless of `autostart`.
    - `url`: URL Chrome opens on launch. None = launch with no URL.
      `jailbee chrome <name> <URL>` overrides this per-call.
    - `dark_mode`: pass --force-dark-mode + --enable-features=
      WebContentsForceDark regardless of host GTK theme.
    - `autostart`: launch Chrome after autostart steps complete.
    - `host_path`: host path RO-mounted to /opt/google/chrome (the
      container-side path is hardcoded in gui.open_chrome). Defaults
      to /opt/google/chrome — set to a different path for non-standard
      installs (e.g. chromium), or None to disable the auto-mount.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    url: str | None = None
    dark_mode: bool = False
    autostart: bool = False
    host_path: PathExpanded | None = Field(default_factory=lambda: _DEFAULT_CHROME_HOST_PATH)


class TerminalKittyConfig(BaseModel):
    """Kitty terminal integration (host-side opt-in, container-side terminfo).

    When a developer runs `jailbee shell` / `jailbee tmux` from a kitty terminal on
    the host, `TERM=xterm-kitty` propagates into the container via `incus
    exec`. The base image's terminfo database doesn't ship the `xterm-kitty`
    entry, so curses-aware tools emit `WARNING: terminal is not fully
    functional` and degrade. This block, when active, RO bind-mounts the
    host's `xterm-kitty` terminfo file into every container so the entry
    resolves naturally.

    - `enabled`: ``"auto"`` (default) activates iff the host terminfo file
      can be located. ``True`` activates and fails validation if no file is
      found. ``False`` disables the integration unconditionally.
    - `host_terminfo_path`: explicit host path. When ``None`` (default),
      autodetect probes ``/usr/share/terminfo/x/xterm-kitty``,
      ``~/.local/kitty.app/lib/kitty/terminfo/x/xterm-kitty``, and
      ``~/.terminfo/x/xterm-kitty`` in that order.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: Literal["auto", True, False] = "auto"
    host_terminfo_path: PathExpanded | None = None


class TerminalConfig(BaseModel):
    """Container of terminal-emulator integrations. Currently just kitty."""

    model_config = ConfigDict(extra="forbid")
    kitty: TerminalKittyConfig = TerminalKittyConfig()


def _kitty_terminfo_candidates() -> list[Path]:
    """Ordered list of host paths jailbee probes for the kitty terminfo entry.

    1. Distro package (``kitty-terminfo`` on Debian/Ubuntu/Fedora).
    2. Kitty's official ``installer.sh`` user-local layout.
    3. User-installed via ``tic``.
    """
    home = Path.home()
    return [
        Path("/usr/share/terminfo/x/xterm-kitty"),
        home / ".local/kitty.app/lib/kitty/terminfo/x/xterm-kitty",
        home / ".terminfo/x/xterm-kitty",
    ]


def resolve_kitty_terminfo_path(*, explicit: Path | None) -> Path | None:
    """Return an existing host terminfo file path, or None.

    Explicit-path mode: returns the path iff it exists. Autodetect mode:
    returns the first existing candidate from ``_kitty_terminfo_candidates``.
    """
    if explicit is not None:
        return explicit if explicit.exists() else None
    for cand in _kitty_terminfo_candidates():
        if cand.exists():
            return cand
    return None


class AutostartStep(BaseModel):
    """A single shell step to run inside a container during autostart.

    `network` swaps the container's network profile for the duration of the
    step (and restores it afterwards). `mounts` lists `optional_mounts` keys
    to attach for the step's duration. `background: True` detaches via
    `setsid` and does not wait for completion.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    run: str
    network: Literal["strict", "loose"] | None = None
    mounts: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    working_dir: str = ""
    background: bool = False
    timeout: int | None = None
    continue_on_error: bool = False

    @field_validator("network", mode="before")
    @classmethod
    def _no_offline(cls, v: object) -> object:
        return _reject_offline(v)


class DockerRegistryMirrorRepoConfig(BaseModel):
    """Per-repo overrides for the host-global rpardini mirror.

    Currently only ``extra_registries``: a list of upstream registry hostnames
    that this repo pulls images from but which aren't covered by rpardini's
    built-in defaults (Docker Hub, registry.k8s.io, gcr.io, quay.io, ghcr.io).
    The strings are hostnames (optionally with ``:port``) — no scheme, no
    path. Empty by default; ``jailbee new`` / ``jailbee apply`` push the merged list
    into the mirror's REGISTRIES env on each run.
    """

    model_config = ConfigDict(extra="forbid")
    extra_registries: list[str] = Field(default_factory=list)

    @field_validator("extra_registries")
    @classmethod
    def _validate_hostnames(cls, v: list[str]) -> list[str]:
        for raw in v:
            if not raw or raw != raw.strip():
                raise ValueError(f"empty / whitespace-padded registry hostname: {raw!r}")
            if any(c.isspace() for c in raw):
                raise ValueError(f"registry hostname must not contain whitespace: {raw!r}")
            if "/" in raw or "://" in raw:
                raise ValueError(
                    f"registry must be a bare hostname[:port], not a URL/path: {raw!r}"
                )
        return v


# `claude.pr_prompt` ships to the container as an environment variable inside
# jailbee's own prompt. The cap is a sanity bound, not a model context limit:
# it turns a pasted-in-by-accident file into a config error instead of a
# `claude` invocation that fails opaquely and silently falls back.
_MAX_PR_PROMPT_LEN = 20_000


class AgentSharedMount(BaseModel):
    """One bind-mount an agent needs to keep its auth/config across containers.

    Share the minimum surface that avoids re-authentication. Caches, chat
    histories and logs are per-branch working state and must stay
    per-container; a generically-named file (e.g. `~/.env`) must never be
    shared, because the mount would collide with unrelated tools and leak
    their secrets between containers.
    """

    model_config = ConfigDict(extra="forbid")
    subpath: str
    path: str
    type: Literal["dir", "file"] = "dir"
    seed: str | None = None

    @model_validator(mode="after")
    def _seed_is_file_only(self) -> AgentSharedMount:
        if self.type == "dir" and self.seed is not None:
            raise ValueError(f"seed is only valid for type: file (subpath {self.subpath!r})")
        return self


class AgentConfig(BaseModel):
    """A terminal coding agent wired into the container lifecycle.

    `install`/`update` are shell command lines run inside the container as the
    dev user through the autostart step pipeline, so each gets a fresh
    `bash -lc` login shell — which is why `~/.local/bin` and
    `~/.npm-global/bin` are on PATH for them.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    autostart: bool = False
    command: str = ""
    install: str | None = None
    install_check: str | None = None
    update: str | None = None
    auto_update: bool = True
    install_network: Literal["strict", "loose"] = "strict"
    shared: list[AgentSharedMount] = Field(default_factory=list)
    egress_allow: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    def effective_install_check(self) -> str:
        """The command that decides install-vs-update.

        Defaults to `command -v <first token of command>`: the binary's own
        name, not the full command line, so flags in `command` don't leak into
        the probe.
        """
        if self.install_check:
            return self.install_check
        binary = self.command.split()[0] if self.command.strip() else ""
        return f"command -v {binary}"


class ClaudeAgentConfig(AgentConfig):
    """`agents.claude` — the generic fields plus Claude-only integrations.

    `enabled`, `autostart`, `command` and `auto_update` are inherited: their
    semantics are identical to any other agent's. `enabled` gates the shared
    `<shared_dir>/claude` cache mount (see `Config.effective_shared_caches`),
    the `CLAUDE_API_HOSTS` strict-mode egress auto-add, the
    `<shared_dir>/claude` subdir creation on `jailbee init`, and the
    claude-subdir presence check in `jailbee doctor`. When enabled, jailbee
    creates an empty `<shared_dir>/claude` directory as a bind-mount source
    and seeds `<shared_dir>/claude/.claude.json` with `{}` — the golden image
    exports `CLAUDE_CONFIG_DIR=$HOME/.claude`, so Claude Code reads its
    global config from inside that directory mount, and Claude Code inside
    the first container runs its onboarding flow from a clean state. No host
    `~/.claude` / `~/.claude.json` is read.

    - `plugins_enabled`: when true (default), `effective_egress_allow`
      also appends `CLAUDE_PLUGIN_HOSTS` (GitHub + npm) so that Claude
      Code's plugin marketplace, skills and SessionStart hooks load in
      strict-mode containers. Set to false to keep the API reachable
      while blocking marketplace traffic. Has no effect when `enabled`
      is false.
    - `install_jailbee_skills`: when true (default, requires `enabled`), `jailbee new`
      and `jailbee apply` copy jailbee's bundled Claude skills (`jailbee-usage`,
      `jailbee-repo-setup`) into the shared `<shared_dir>/claude/skills/` so the
      in-container Claude understands jailbee and can help with `.jailbee/config.yaml`
      edits. Host-side file copy only — no network. Has no effect when `enabled`
      is false. The pre-1.0 key name (`install_gie_skills`) is not accepted at
      all — `_check_retired_keys`/`_RETIRED_KEYS_CLAUDE` raises a `ConfigError`
      naming this key as the replacement, under both the legacy `claude:` and
      the `agents.claude` spelling.
    - `ai_pr_description`: when true (default, requires `enabled`),
      `jailbee pr` asks the in-container Claude CLI to generate the
      PR title and body from the branch's commits and diff, falling back to
      a placeholder if generation fails. Has no effect when `enabled` is
      false.
    - `ai_pr_branch`: when true (default, requires `enabled`), `jailbee pr` asks
      the in-container Claude to propose a convention-following PR head branch
      name when opening a new PR; has no effect when `enabled` is false.
    - `pr_prompt`: project-specific PR-writing instructions, typically set in a
      repo's `.jailbee/config.yaml` as a YAML block scalar. They are embedded in
      jailbee's own prompt as a delimited section that explicitly outranks the
      generic guidance, so a project can dictate the title and body shape
      without having to restate the JSON response contract `_parse_pr_text`
      depends on. Capped at 20 000 characters so a pathological value fails at
      config load rather than inside the container. Has no effect when
      `enabled` or `ai_pr_description` is false.
    - `ai_pr_model`: the model `jailbee pr` passes to `claude --model` when
      generating the PR text. Defaults to `sonnet`: writing a PR description is
      a bounded summarisation job, and pinning it means the generation does not
      compete for the same budget as the coding work that just happened in the
      container. Accepts an alias (`sonnet`, `opus`, `haiku`) or a full model
      ID; `null` omits the flag entirely so the container's own default model
      applies. `haiku` is a valid choice but has a smaller context window than
      the alternatives, so a large cumulative diff may not fit. Has no effect
      when `enabled` or `ai_pr_description` is false.
    - `ai_pr_timeout`: seconds `jailbee pr` gives the in-container Claude to
      produce the PR text before giving up and falling back to a placeholder.
      Defaults to 600. Generation is an agentic run, not one model call — it
      reads the log, the cumulative diff, the PR template and the branch's spec
      across a dozen-plus turns, so cost scales with the repository, not just
      with the diff. Measured in jailbee's own repo on a 21-file diff: 129s.
      Raise it for a large tree, or when `claude.pr_prompt` asks for work that
      takes longer. Has no effect when `enabled` or `ai_pr_description` is
      false.
    """

    plugins_enabled: bool = True
    install_jailbee_skills: bool = True
    ai_pr_description: bool = True
    ai_pr_branch: bool = True
    pr_prompt: str | None = Field(default=None, max_length=_MAX_PR_PROMPT_LEN)
    ai_pr_model: str | None = "sonnet"
    ai_pr_timeout: int = Field(default=600, gt=0)

    @field_validator("ai_pr_model")
    @classmethod
    def _reject_non_model_value(cls, v: str | None) -> str | None:
        """A model name is a single token — reject anything that isn't one.

        The value reaches `claude --model` through an environment variable, so
        embedded flags could never be executed as such. The check exists to
        turn a typo or a misunderstanding into a config error, rather than a
        non-zero `claude` exit that `generate_pr_text` reports only as a failed
        generation. Use `null`, not an empty string, to inherit the container's
        own default model.
        """
        if v is None:
            return None
        if not v.strip() or len(v.split()) != 1:
            raise ValueError(
                f"must be a single model name or alias (e.g. 'sonnet', "
                f"'claude-haiku-4-5'), or null to inherit the container "
                f"default; got {v!r}"
            )
        return v.strip()


class GithubConfig(BaseModel):
    """GitHub CLI (gh) integration inside containers.

    - `enabled`: master switch. Defaults to false; opt-in via
      ~/.config/jailbee/global.yaml. When false, jailbee skips:
        * the api.github.com:443 strict-mode egress auto-add,
        * the /etc/profile.d/jailbee-github.sh autostart write,
        * the github doctor checks.
      gh binary itself is always installed in the golden image
      (parallels claude.enabled vs the ensure-claude.sh runtime step).
    - `api_tokens`: map from `container_prefix` to a fine-grained PAT.
      One entry per GitHub resource owner (org or personal account).
      Value is a SecretStr so accidental repr / config-dump masks it.
      Permitted only at the global config layer (~/.config/jailbee/global.yaml);
      see load_config's placement constraint.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    api_tokens: dict[str, SecretStr] = Field(default_factory=dict)


class Autostart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_create: list[AutostartStep] = Field(default_factory=list)
    on_start: list[AutostartStep] = Field(default_factory=list)
    step_timeout: int = 600
    env: dict[str, str] = Field(default_factory=dict)


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
