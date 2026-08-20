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

import ipaddress
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from jailbee.constants import SHARED_SUBDIRS
from jailbee.git import DEFAULT_REMOTE, detect_default_branch, detect_upstream_remote
from jailbee.paths import expand_path, xdg_data_home

if TYPE_CHECKING:
    from jailbee.global_config import GlobalConfig

_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CACHE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# An agent name becomes a tmux window name and a `jailbee doctor` label —
# kept to the safest common subset of what both accept. It does *not* reach
# any Incus device name: those derive from each `shared[].subpath` via
# `device_name()`. The two only coincide because every shipped preset happens
# to name its subpath after the agent.
_AGENT_NAME_RE = re.compile(r"[a-z0-9-]+")

# Container's unix username is hardcoded — must match the user baked into the
# golden image by provision/install.sh. It used to be configurable; made fixed
# because nothing enforced consistency between this value and the golden-image
# user, and the symptom was a confusing Permission-denied on `jailbee new`.
CONTAINER_USERNAME = "dev"

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

# Keys in ~/.config/jailbee/global.yaml that belong to GlobalConfig (host-level)
# and must NOT be merged into the Config layer. `docker_registry_mirror`
# exists in both schemas with different shapes (host-level: {port, enabled,
# image, data_dir}; Config-level: {extra_registries}) — we resolve the
# ambiguity by treating it as host-level at the global file, and only
# accepting the Config-level shape at the repo file.
#
# `ls` and `dashboard` are here for a different reason: they exist in both
# schemas with the *same* shape and are merged field-by-field by
# `Config._effective_columns` (repo block over global block), exactly like
# `loose_auto_revert`. Letting them through to `deep_merge` as well would
# apply the list rule to `fields`/`hide` — which *appends* a non-empty
# overlay — so a global `fields: [name, state]` plus a repo
# `fields: [name, ip]` produced `[name, state, name, ip]` and rendered NAME
# twice. Splitting them out keeps `effective_ls_columns` /
# `effective_dashboard_columns` the single merge mechanism, and it is what
# makes a global-only `dashboard:` block reach the dashboards at all
# (`dashboard.resolve_dashboard_columns` reads `GlobalConfig.dashboard`
# directly when there is no cwd repo).
#
# Note `loose_auto_revert` is *not* in this set even though it has exactly
# the same "merged field-by-field, not through deep_merge" shape — see
# `Config.effective_loose_auto_revert`. That routing is deliberate and
# belongs to an earlier spec; don't "fix" the apparent 3-vs-4-fields
# asymmetry by adding it here. It works today only because every
# `LooseAutoRevert` field is a scalar (`enabled: bool`, `after: str | int`),
# so `deep_merge`'s append-a-list behaviour never triggers. The day a list
# field is added to `LooseAutoRevert`, it reintroduces the exact append bug
# `ls`/`dashboard` were split out to avoid, and would need the same
# treatment (its own merge method, kept out of `deep_merge`).
#
# `agents` deliberately stays OUT of this set. It is a mapping keyed by agent
# name, so `deep_merge` recurses per agent instead of hitting the list rule —
# a repo layer adjusting one field of a globally-defined agent merges cleanly.
# Its one list-valued field, `egress_allow`, *wants* the append behaviour: a
# repo adding a single host to a global agent is the intended use. Don't
# "fix" the apparent asymmetry with `ls`/`dashboard` by adding it here.
_HOST_LEVEL_KEYS: frozenset[str] = frozenset({"docker_registry_mirror", "ls", "dashboard"})


def _split_host_keys(
    raw: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Return (host_level, config_level) sub-dicts of a global.yaml raw load."""
    host = {k: v for k, v in raw.items() if k in _HOST_LEVEL_KEYS}
    config = {k: v for k, v in raw.items() if k not in _HOST_LEVEL_KEYS}
    return host, config


def _read_yaml_or_empty(path: Path) -> dict[str, object]:
    """Read and parse a YAML file. Missing file -> {}. Invalid YAML -> ConfigError."""
    if not path.exists():
        return {}
    return _parse_yaml_text(path.read_text(), str(path))


def _parse_yaml_text(text: str, origin: str) -> dict[str, object]:
    """Parse YAML text into a mapping, mirroring `_read_yaml_or_empty`.

    `origin` is a human-readable label for error messages (a path, or a
    "<ref>:<path>" locator for a config read out of git).
    """
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {origin}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"Top level of {origin} must be a mapping; got {type(raw).__name__}.")
    return raw


def deep_merge(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    """Deep-merge two raw config dicts.

    Rules:
      * scalars: overlay wins (None clears)
      * lists:   overlay appended; `[]` overlay = reset to empty list
      * dicts:   recursive deep_merge per key
      * type mismatch (different shape): overlay wins

    Inputs are not mutated.
    """
    result: dict[str, object] = {k: _copy(v) for k, v in base.items()}
    for key, overlay_value in overlay.items():
        if key not in result:
            result[key] = _copy(overlay_value)
            continue
        base_value = result[key]
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = deep_merge(base_value, overlay_value)
        elif isinstance(base_value, list) and isinstance(overlay_value, list):
            # Empty overlay list = explicit reset; non-empty = append.
            result[key] = [] if not overlay_value else base_value + list(overlay_value)
        else:
            # Scalar override, type mismatch, or None-clear: overlay wins.
            result[key] = _copy(overlay_value)
    return result


def _copy(value: object) -> object:
    """Shallow recursive copy for dict/list, identity for scalars."""
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or is invalid."""


class ConfigNotFoundError(ConfigError):
    """Raised when .jailbee/config.yaml is missing in the current directory."""


_RETIRED_KEYS_TOP_LEVEL: dict[str, str] = {
    "ide": "jetbrains.ide",
    "chrome_url": "chrome.url",
    "seed_ssh_from_host": "ssh.seed_from_host",
    "jetbrains_userprefs_from_host": "jetbrains.userprefs_from_host",
}

_RETIRED_KEYS_AUTOSTART: dict[str, str] = {
    "open_ide": "jetbrains.ide + jetbrains.autostart",
    "open_chrome": "chrome.autostart",
    "chrome_dark_mode": "chrome.dark_mode",
}

# Renamed with the project itself. Accepted as a validation alias with a
# deprecation warning through 1.0.x; retired in 1.1.0.
_RETIRED_KEYS_CLAUDE: dict[str, str] = {
    "install_gie_skills": "claude.install_jailbee_skills",
}

# Removed-without-replacement keys. Surface the same ConfigError as the
# moved-key maps above, but with a human-readable reason instead of a
# new location.
_CLAUDE_SEED_REMOVED_MSG = (
    "claude.seed_from_host has been removed — jailbee no longer seeds "
    "~/.claude from the host. The container starts with an empty "
    "<shared_dir>/claude and Claude Code runs its onboarding flow on "
    "first launch. Remove this key from your config."
)
_REMOVED_KEYS_TOP_LEVEL: dict[str, str] = {
    "seed_claude_from_host": _CLAUDE_SEED_REMOVED_MSG,
}
_REMOVED_KEYS_CLAUDE: dict[str, str] = {
    "seed_from_host": _CLAUDE_SEED_REMOVED_MSG,
}


def _check_retired_keys(raw: dict[str, object]) -> None:
    """Raise ConfigError if YAML contains keys retired in the host-tooling
    config restructure. Names the new location in the error message.
    """
    for old, new in _RETIRED_KEYS_TOP_LEVEL.items():
        if old in raw:
            raise ConfigError(
                f"Unknown field `{old}` in config: moved to `{new}`. "
                f"See docs/config.md for the new schema."
            )
    for old, reason in _REMOVED_KEYS_TOP_LEVEL.items():
        if old in raw:
            raise ConfigError(reason)
    autostart = raw.get("autostart", {})
    if isinstance(autostart, dict):
        for old, new in _RETIRED_KEYS_AUTOSTART.items():
            if old in autostart:
                raise ConfigError(
                    f"Unknown field `autostart.{old}` in config: moved to "
                    f"`{new}`. See docs/config.md for the new schema."
                )
    # Checked under both spellings: the legacy top-level `claude:` block and
    # its `agents.claude` successor. A user who has already migrated to
    # `agents.claude` still deserves the same retired-key error, not a
    # confusing "unknown field" from Pydantic's `extra="forbid"`.
    claude_blocks: list[tuple[str, object]] = [("claude", raw.get("claude", {}))]
    agents = raw.get("agents", {})
    if isinstance(agents, dict):
        claude_blocks.append(("agents.claude", agents.get("claude", {})))
    for label, claude in claude_blocks:
        if not isinstance(claude, dict):
            continue
        for old, reason in _REMOVED_KEYS_CLAUDE.items():
            if old in claude:
                raise ConfigError(reason)
        for old, new in _RETIRED_KEYS_CLAUDE.items():
            if old in claude:
                raise ConfigError(
                    f"Unknown field `{label}.{old}` in config: renamed to "
                    f"`{new}`. See docs/config.md for the new schema."
                )


def _check_pull_migration(
    global_raw: dict[str, object],
    repo_raw: dict[str, object],
    global_path: Path,
    repo_path: Path,
) -> None:
    """Raise ConfigError if either layer still uses the legacy `merge:` key.

    The block was renamed when `jailbee git merge` became `jailbee git pull`.
    Reports every file that carries the legacy key in a single message.
    """
    legacy_paths: list[Path] = []
    if "merge" in global_raw:
        legacy_paths.append(global_path)
    if "merge" in repo_raw:
        legacy_paths.append(repo_path)
    if not legacy_paths:
        return
    paths_listed = "\n  ".join(str(p) for p in legacy_paths)
    raise ConfigError(
        f"Config key 'merge:' was renamed to 'pull:' "
        f"(the 'jailbee git merge' command was renamed to 'jailbee git pull'). "
        f"Update:\n  {paths_listed}"
    )


def _check_agents_spelling(
    global_raw: dict[str, object],
    repo_raw: dict[str, object],
    global_path: Path,
    repo_path: Path,
) -> None:
    """Raise ConfigError if the legacy `claude:` and `agents.claude` spellings
    are both in play across the two layers.

    `resolve_agents_raw` catches the same conflict on the merged dict, but by
    then the layers are indistinguishable and the surrounding message in
    `_build_config_from_dict` can only name the repo config. The likely shape
    of this conflict for an existing user is a `claude:` block left in
    `global.yaml` (what the old template wrote) meeting an `agents.claude` in
    a repo config — so the file the user must edit is precisely the one that
    message would *not* name. Report every file that carries either spelling,
    the way `_check_pull_migration` does for the renamed `merge:` key.
    """

    def _has_agents_claude(raw: dict[str, object]) -> bool:
        agents = raw.get("agents")
        return isinstance(agents, dict) and "claude" in agents

    legacy = [p for raw, p in ((global_raw, global_path), (repo_raw, repo_path)) if "claude" in raw]
    modern = [
        p
        for raw, p in ((global_raw, global_path), (repo_raw, repo_path))
        if _has_agents_claude(raw)
    ]
    if not legacy or not modern:
        return
    listed = "\n  ".join(f"{p} ({label})" for p, label in _label_spellings(legacy, modern))
    raise ConfigError(
        "Config defines both the legacy `claude:` block and `agents.claude` — "
        "keep one. `agents.claude` is the preferred spelling; the `claude:` "
        f"block is a supported legacy alias. Files involved:\n  {listed}"
    )


def _label_spellings(
    legacy: list[Path], modern: list[Path]
) -> list[tuple[Path, str]]:
    """`(path, "claude:" / "agents.claude" / both)` for each file involved,
    in `legacy`-then-`modern` file order without repeating a path."""
    labels: dict[Path, list[str]] = {}
    for path in legacy:
        labels.setdefault(path, []).append("claude:")
    for path in modern:
        labels.setdefault(path, []).append("agents.claude")
    return [(path, " and ".join(spellings)) for path, spellings in labels.items()]


def _expand(value: str | Path) -> Path:
    return expand_path(value)


PathExpanded = Annotated[Path, BeforeValidator(_expand)]


class ContainerUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uid: int = -1
    gid: int = -1

    def model_post_init(self, __context: object) -> None:
        if self.uid == -1:
            object.__setattr__(self, "uid", os.getuid())
        if self.gid == -1:
            object.__setattr__(self, "gid", os.getgid())


# Env-var name grammar accepted by `container.env`. Same as POSIX env names:
# leading [A-Za-z_], rest [A-Za-z0-9_]. Rejected at config-load time so a
# typo doesn't surface as a confusing `incus profile edit` failure.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ContainerConfig(BaseModel):
    """Container-wide settings applied via the Incus base profile.

    `env` is a map of literal string env vars injected into every process
    Incus starts in the container — including `jailbee shell`, `jailbee tmux`,
    tmux servers, and autostart steps. Values are not expanded; the YAML
    string is passed through verbatim. Keys jailbee itself sets in the base
    profile (DISPLAY, WAYLAND_DISPLAY, XDG_RUNTIME_DIR, SSH_AUTH_SOCK)
    are still overridable here — user override wins.
    """

    model_config = ConfigDict(extra="forbid")
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("env")
    @classmethod
    def _validate_env_names(cls, v: dict[str, str]) -> dict[str, str]:
        for name in v:
            if not _ENV_NAME_RE.match(name):
                raise ValueError(
                    f"invalid env var name: {name!r}. Must match "
                    r"[A-Za-z_][A-Za-z0-9_]*"
                )
        return v


class HostMount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: PathExpanded
    container: str
    readonly: bool = False


_OCTAL_MODE_RE = re.compile(r"^0[0-7]{3,4}$")

# Unix group-name grammar (useradd's NAME_REGEX, simplified). Enforced on
# `host_devices.group` because the value is interpolated into an in-container
# `usermod -aG <group> dev` shell command — letting shell metacharacters
# through would be a command-injection vector.
_GROUP_NAME_RE = re.compile(r"^[a-z_][a-z0-9_-]*\$?$")


class HostDevice(BaseModel):
    """A host character/block device passed into the container.

    Mirrors the render-node ``unix-char`` mechanism in
    ``profiles.base_profile_yaml`` but driven by config. Opt-in, default
    empty. Each entry widens the host-kernel attack surface reachable from
    inside the (unprivileged) container — see docs/config.md.

    ``group`` controls which container group the ``dev`` user is added to so
    it can actually open the device. When unset, jailbee auto-derives it from the
    host source node's owning group (e.g. ``/dev/kvm`` → ``kvm``). This is the
    reliable access mechanism: a device like ``/dev/kvm`` carries a udev
    ``static_node`` rule, so the container's systemd-udevd resets the node to
    its distro default (``root:kvm 0660``) on every boot regardless of the
    profile ``mode`` — group membership grants access where ``mode`` cannot.
    """

    model_config = ConfigDict(extra="forbid")
    path: str
    source: str | None = None
    type: Literal["unix-char", "unix-block"] = "unix-char"
    mode: str | None = "0666"
    gid: int | None = None
    uid: int | None = None
    group: str | None = None

    @property
    def effective_source(self) -> str:
        """Host device path; defaults to the in-container ``path``."""
        return self.source or self.path

    @field_validator("path", "source")
    @classmethod
    def _validate_absolute(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith("/"):
            raise ValueError(f"host_devices path/source must be absolute, got: {v!r}")
        return v

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str | None) -> str | None:
        if v is not None and not _OCTAL_MODE_RE.match(v):
            raise ValueError(f"host_devices mode must be an octal string like '0666', got: {v!r}")
        return v

    @field_validator("group")
    @classmethod
    def _validate_group(cls, v: str | None) -> str | None:
        if v is not None and not _GROUP_NAME_RE.match(v):
            raise ValueError(f"host_devices group must be a valid unix group name, got: {v!r}")
        return v


# Handle grammar for a `host_ports` entry. The value becomes an Incus device
# name (`port-cfg-<name>`) and the `jailbee port rm` key, so it is kept to
# characters that read cleanly in both.
_PORT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PORT_NAME_MAX_LEN = 40


class HostPort(BaseModel):
    """A host service made reachable inside every container of the repo.

    Rendered as an Incus ``proxy`` device with ``bind=instance``: the
    container listens on ``container_address:port`` and Incus's forkproxy
    connects to ``host_address:host_port`` on the host. The classic case is
    an adb server — with the forward in place, plain ``adb devices`` works
    inside the container and no ``ADB_SERVER_SOCKET`` is needed.

    Only this direction is configurable. A host-side listener is a
    machine-wide resource, so declaring one here would make every container
    of the repo fight over the same host port, breaking the property that
    many branch containers coexist. See ``jailbee port to-host``.

    Note this is a hole through ``net strict`` by construction: the host end
    of the connection is opened by a host process outside the container's
    network namespace, so the egress ACL never sees it. See docs/security.md.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    port: int
    host_port: int | None = None
    proto: Literal["tcp", "udp"] = "tcp"
    host_address: str = "127.0.0.1"
    container_address: str = "127.0.0.1"

    @property
    def effective_host_port(self) -> int:
        """Host-side port; defaults to the container-side ``port``."""
        return self.port if self.host_port is None else self.host_port

    @model_validator(mode="before")
    @classmethod
    def _reject_direction_keys(cls, data: object) -> object:
        """Explain the to-host omission instead of saying "Unknown field".

        Runs before `extra="forbid"` so the reason lands in the message.
        """
        if isinstance(data, dict):
            for key in ("direction", "to_host", "bind"):
                if key in data:
                    raise ValueError(
                        f"host_ports entries cannot set `{key}`: only the "
                        "to-container direction (a host service reachable "
                        "inside the container) is configurable. A host port "
                        "is machine-wide, so a repo config declaring one "
                        "would make every container of this repo fight over "
                        "it. Use `jailbee port to-host <port>` on the one "
                        "container that needs it."
                    )
        return data

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _PORT_NAME_RE.match(v) or len(v) > _PORT_NAME_MAX_LEN:
            raise ValueError(
                "host_ports name must match [a-z0-9][a-z0-9-]* and be at "
                f"most {_PORT_NAME_MAX_LEN} chars, got: {v!r}"
            )
        return v

    @field_validator("port", "host_port")
    @classmethod
    def _validate_port(cls, v: int | None) -> int | None:
        # Ours to enforce: Incus accepts an out-of-range port at device-add
        # time and only fails when the device starts.
        if v is not None and not 1 <= v <= 65535:
            raise ValueError(f"host_ports port must be 1..65535, got: {v}")
        return v

    @field_validator("host_address", "container_address")
    @classmethod
    def _validate_address(cls, v: str) -> str:
        # A hostname would have to be resolved at device-add time, silently
        # pinning one IP into the device. Incus wants an address anyway.
        try:
            ipaddress.ip_address(v)
        except ValueError as e:
            raise ValueError(
                f"host_ports address must be an IP literal, not a hostname: {v!r}"
            ) from e
        return v


class OptionalMount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: PathExpanded
    container: str
    readonly: bool = True
    description: str = ""


class SharedCache(BaseModel):
    """A bind-mount from <shared_dir>/<host_subpath> into the container.

    `container_path` may start with ``~``, expanded to ``/home/<user>``.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    host_subpath: str
    container_path: str


def _default_shared_caches() -> list[SharedCache]:
    """Default cache mounts — stack-neutral: just the ssh cache.

    The language caches (pnpm/gradle/npm/m2) are now opt-in: add them
    explicitly via ``shared_caches:`` in YAML, or enable the matching
    `golden.stacks.java` / `.node` toggle, which auto-adds gradle+m2
    (java) or npm+pnpm-store (node) via `Config.effective_shared_caches`.
    Override the whole list by setting ``shared_caches:`` to a different
    list (or ``[]`` to disable entirely). Agent and JetBrains shared caches
    are not included here — they are added by
    `Config.effective_shared_caches`: one set per enabled entry in
    `agents:` (via the generic `agents.enabled_agent_specs` loop, which is
    where Claude's now come from), plus the JetBrains ones when
    `jetbrains.enabled`.
    """
    return [
        SharedCache(name="ssh", host_subpath="ssh", container_path="~/.ssh"),
    ]


def _jetbrains_shared_caches(container_prefix: str, *, share_idea: bool) -> list[SharedCache]:
    """JetBrains shared-cache mounts auto-added when `jetbrains.enabled`.

    Three entries: `<shared_dir>/jetbrains-config` → `~/.config/JetBrains`
    (IDE preferences, plugins, recents); `<shared_dir>/jetbrains-data`
    → `~/.local/share/JetBrains` (caches, indexes); and, when
    `share_idea` is true, `<shared_dir>/jetbrains-idea` →
    `~/<container_prefix>/.idea` (project state — run configs, code
    styles, inspection profiles).

    Kept here rather than in `_default_shared_caches()` so the list-level
    `shared_caches:` YAML key isn't polluted with mounts the user doesn't
    need when `jetbrains.enabled=false`.
    """
    result = [
        SharedCache(
            name="jetbrains-config",
            host_subpath="jetbrains-config",
            container_path="~/.config/JetBrains",
        ),
        SharedCache(
            name="jetbrains-data",
            host_subpath="jetbrains-data",
            container_path="~/.local/share/JetBrains",
        ),
    ]
    if share_idea:
        result.append(
            SharedCache(
                name="jetbrains-idea",
                host_subpath="jetbrains-idea",
                container_path=f"~/{container_prefix}/.idea",
            )
        )
    return result


# Descriptions baked into the generated Incus net-profiles (visible via
# `incus profile show <prefix>-net-{strict,loose}`). The modes themselves
# are fixed — only `egress_allow` (strict-mode allowlist) is
# user-configurable.
NET_DESCRIPTIONS: dict[str, str] = {
    "strict": "Minimal egress for normal dev",
    "loose": "Wider egress for debugging",
}

# The `offline` network mode (no NIC attached) was removed: `strict` is
# already a default-deny egress allowlist, so `offline` only duplicated it
# with extra UI surface. Config files carrying the old value get this
# message rather than a bare enum error.
OFFLINE_REMOVED_MSG = (
    "network mode 'offline' was removed — use 'strict' (default-deny egress allowlist)"
)


def _reject_offline(v: object) -> object:
    """`mode="before"` validator body shared by the two network fields."""
    if v == "offline":
        raise ValueError(OFFLINE_REMOVED_MSG)
    return v


# Hosts JetBrains IDEs need to reach for license activation, plugin
# marketplace, installer/CDN, and framework dependency config. Added
# automatically by `Config.effective_egress_allow()` whenever
# `jetbrains.enabled` is true so users don't have to know JetBrains'
# service topology.
#
# Sourced from JetBrains' published allowlist guidance
# (https://intellij-support.jetbrains.com/hc/en-us/articles/206544429):
# - account / cloudconfig: JBA license activation and validation
# - plugins: plugin marketplace
# - www, download, download-cf, download-cdn: docs + installers + CDNs
# - frameworks: Java framework dependency config + AI prompt rules
# - data.services: legacy services endpoint (retained for older IDE builds)
# - resources: OAuth provider icons rendered in the JBA sign-in dialog;
#   without it the login UI cannot finish loading and license activation
#   silently stalls in "trial available" state.
# - api.jetbrains.cloud: license trace-status endpoint. Note the `.cloud`
#   TLD — a wildcard on `*.jetbrains.com` would NOT match this host.
# - oauth.account: JBA OAuth sign-in endpoint. Different subdomain AND
#   different IP space (AWS ELB in eu-west-1) than account.jetbrains.com,
#   so the account allowlist entry doesn't cover it. Without this the
#   sign-in flow cannot complete the OAuth handshake.
# - downloads.marketplace: plugin payload CDN (CloudFront), separate IP
#   space from plugins.jetbrains.com. Required for installing or updating
#   plugins from the marketplace in strict mode.
JETBRAINS_LICENSE_HOSTS: tuple[str, ...] = (
    "account.jetbrains.com:443",
    "oauth.account.jetbrains.com:443",
    "cloudconfig.jetbrains.com:443",
    "plugins.jetbrains.com:443",
    "downloads.marketplace.jetbrains.com:443",
    "www.jetbrains.com:443",
    "resources.jetbrains.com:443",
    "download.jetbrains.com:443",
    "download-cf.jetbrains.com:443",
    "download-cdn.jetbrains.com:443",
    "frameworks.jetbrains.com:443",
    "data.services.jetbrains.com:443",
    "api.jetbrains.cloud:443",
)

# Hosts JetBrains AI Assistant uses. Added by `effective_egress_allow()`
# only when `jetbrains.enabled` AND `jetbrains.ai_enabled` are true,
# because AI Assistant is opt-in and most users won't need these.
JETBRAINS_AI_HOSTS: tuple[str, ...] = (
    "api.app.prod.grazie.aws.intellij.net:443",
    "api.jetbrains.ai:443",
)

# Hosts gh CLI reaches: api.github.com for REST/GraphQL. Added
# automatically by `Config.effective_egress_allow()` when `github.enabled`
# so users don't have to know the GitHub API topology. Intentionally
# minimal — github.com:443 / codeload.github.com:443 /
# uploads.github.com:443 / objects.githubusercontent.com:443 stay off
# until a use case forces them in.
GITHUB_API_HOSTS: tuple[str, ...] = ("api.github.com:443",)


_DURATION_RE = re.compile(r"^(\d+)\s*(s|m|h)$")


def _parse_duration(value: str) -> timedelta:
    """Parse ``30s`` / ``5m`` / ``2h`` into a ``timedelta``.

    Unitless integers go through ``LooseAutoRevert.after`` directly (typed
    as ``int``) and are interpreted as minutes — this helper handles only
    suffixed strings.
    """
    m = _DURATION_RE.match(value.strip())
    if not m:
        raise ValueError(f"invalid duration {value!r}; expected `<int>s|m|h` (e.g. `5m`)")
    n = int(m.group(1))
    unit = m.group(2)
    if unit == "s":
        return timedelta(seconds=n)
    if unit == "m":
        return timedelta(minutes=n)
    return timedelta(hours=n)


class LooseAutoRevert(BaseModel):
    """Policy for auto-reverting `jailbee net loose` after a TTL.

    Lives in both ``~/.config/jailbee/global.yaml`` and per-repo
    ``.jailbee/config.yaml``. Per-repo overrides global field by field — see
    ``Config.effective_loose_auto_revert``.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    after: str | int = "5m"

    def duration(self) -> timedelta:
        """Parse ``after`` into a ``timedelta``. Raises ``ValueError`` on
        bad input (negative, zero, unparseable, or >24h).
        """
        raw = self.after
        if isinstance(raw, int):
            if raw <= 0:
                raise ValueError(f"loose_auto_revert.after must be > 0, got {raw}")
            td = timedelta(minutes=raw)
        else:
            td = _parse_duration(raw)
        if td <= timedelta(0):
            raise ValueError(f"loose_auto_revert.after must be > 0, got {raw!r}")
        if td > timedelta(hours=24):
            raise ValueError(f"loose_auto_revert.after must be <= 24h, got {raw!r}")
        return td


# Durations offered when jailbee asks how long to stay in loose — the CLI
# prompt's preset list and the Qt dashboard's dialog items. Not a policy:
# the effective default still comes from `LooseAutoRevert.after`, and any
# value `LooseAutoRevert.duration()` accepts can be typed instead.
LOOSE_TTL_PRESETS: tuple[str, ...] = ("5m", "15m", "30m", "1h", "2h", "4h", "8h")


def parse_loose_ttl(raw: str) -> timedelta | None:
    """Parse a user-supplied loose TTL. ``never`` → None (no auto-revert).

    The single definition of the duration syntax accepted by `jailbee net loose
    --for`, the CLI's interactive prompt and the Qt dashboard's dialog — all
    three share it so a value one accepts can never be rejected by another.
    Delegates to `LooseAutoRevert.duration()` so the units and the 24h cap stay
    in one place; raises `ValueError` with its message.
    """
    value = raw.strip()
    if value.lower() == "never":
        return None
    return LooseAutoRevert(after=value).duration()


def format_loose_after(after: str | int) -> str:
    """Render a `LooseAutoRevert.after` value as prompt-ready text.

    The field is `str | int`; a bare int means minutes.
    """
    return f"{after}m" if isinstance(after, int) else after


# Columns the dashboard drops from the `ls` field set by default: REPO is
# redundant under per-repo grouping, the wide GIT STATUS combo and the
# JSON-only full_name add noise, and TTL is folded into the NETWORK cell.
# Lives here rather than in `dashboard.py` because it is a config default
# and `config.py` cannot import `dashboard` (that module imports this one).
DASHBOARD_DEFAULT_HIDE: tuple[str, ...] = (
    "repo",
    "full_name",
    "git_status",
    "created",
    "ttl",
)


class ColumnConfig(BaseModel):
    """Which columns a table shows.

    ``fields`` is an explicit ordered list and wins outright when set: naming
    a column is a request for that exact column, so it also renders even if
    it would otherwise be hidden by a dynamic ``show_if`` (e.g. ``pr`` with
    nothing open) — see ``table_format.apply_column_config``, the one place
    that rule is implemented, shared by ``jailbee ls`` and the dashboard alike.
    ``hide`` is subtractive and applies only to the built-in default set,
    where ``show_if`` still applies unchanged. A ``--fields`` flag on the
    command line beats both — this is the remembered preference, not a lock.

    Applies to table output only. ``--format json`` keeps its own
    ``default_json`` field set regardless of ``fields``/``hide`` here: this
    is a personal display preference, and a script depending on the default
    JSON shape must not have it silently narrowed by someone's ``global.yaml``.
    An explicit ``--fields`` flag still wins in every format.

    Column choice is a personal preference, so the normal home is
    ``global.yaml``; the same block in a repo's ``.jailbee/config.yaml``
    overrides it for everyone working in that repo, which is deliberate
    and rare.
    """

    model_config = ConfigDict(extra="forbid")
    fields: list[str] | None = None
    hide: list[str] = []


# Repo-layer default for both `Config.ls` and `Config.dashboard`. Unlike the
# global layer — where `GlobalConfig.dashboard`'s default already carries
# `DASHBOARD_DEFAULT_HIDE` (see `global_config._DASHBOARD_DEFAULT`) — a
# repo's own block defaults to a plain, unset `ColumnConfig`; the
# dashboard-hide default is applied later, when `effective_dashboard_columns`
# merges the repo block over the global one. So both repo fields share one
# default here. Used by `load_config`'s sanitize short-circuit.
_COLUMN_DEFAULT = ColumnConfig()


def _known_ls_field_names() -> set[str]:
    """Real `jailbee ls` / dashboard column names, including the LOCAL ones.

    Shared by ``validate_column_blocks`` and ``sanitize_column_blocks``, its
    recovery-flavoured counterpart. The canonical names come from
    ``lifecycle.ls_field_specs``, which ``config.py`` cannot import at
    module level (``lifecycle`` imports ``config`` — a cycle), hence the
    function-local import.
    """
    from jailbee.lifecycle import ls_field_specs

    return {f.name for f in ls_field_specs(now=datetime.now(UTC), all_repos=True)}


def validate_column_blocks(blocks: Sequence[tuple[str, ColumnConfig]]) -> list[str]:
    """Return human-readable problems in `ls:` / `dashboard:` column blocks.

    Used wherever a column typo should be *reported as an error*:
    ``Config.validate_runtime`` for a repo's ``.jailbee/config.yaml``, and
    ``jailbee config validate``'s own check of ``~/.config/jailbee/global.yaml`` (see
    ``global_config.global_config_issues``). Both are advisory-reporting
    paths — neither is on the hot path that actually renders a table, which
    is why raising is fine here but not in ``global_config.load_global_config``
    (see ``sanitize_column_blocks``, its recovery-flavoured counterpart used
    there: a personal display preference must never break unrelated work).

    Rejects three things: an unknown column name, ``fields: []`` (a table
    with no columns at all — ``fields: null`` is how you ask for the
    built-in default set), and a repeated name in ``fields`` (which would
    render that column twice).
    """
    known = _known_ls_field_names()
    allowed = ", ".join(sorted(known))
    issues: list[str] = []
    for block_name, block in blocks:
        if block.fields is not None and not block.fields:
            issues.append(
                f"{block_name}: fields is empty, which would render a table with "
                f"no columns; use `fields: null` for the built-in default set or "
                f"name at least one column"
            )
        seen: set[str] = set()
        for name in block.fields or []:
            if name in seen:
                issues.append(
                    f"{block_name}: duplicate field {name!r} in fields; "
                    f"each column may be named once"
                )
            seen.add(name)
        for name in list(block.fields or []) + list(block.hide):
            if name not in known:
                issues.append(f"{block_name}: unknown field {name!r}; allowed: {allowed}")
    return issues


def sanitize_column_blocks(
    blocks: Sequence[tuple[str, ColumnConfig]],
) -> tuple[dict[str, ColumnConfig], list[str]]:
    """Recover from problems in `ls:` / `dashboard:` blocks instead of rejecting them.

    Companion to ``validate_column_blocks``: same three problems, same
    "which names are real" data, opposite remedy. Used both by
    ``global_config.load_global_config`` (for ``global.yaml``) and by
    ``load_config`` (for a repo's ``.jailbee/config.yaml``) so that a typo'd
    column name — a purely cosmetic, personal display preference — never
    breaks an unrelated command in either file (``jailbee config validate`` is
    where a typo in either is still reported as an error, via
    ``validate_column_blocks``).

    * An unknown name is dropped (from ``fields`` or ``hide``).
    * A duplicate name in ``fields`` is dropped, keeping the first
      occurrence.
    * ``fields: []`` — explicit, or reduced to it by dropping every name as
      unknown/duplicate — falls back to ``fields: None`` (the built-in
      default set). There is no such thing as a table with zero columns, so
      unlike an unknown name (drop it, the rest of the list still means
      something) there is nothing sensible to recover *to* except the
      default; an explicit empty list is presumed to be a mistake rather
      than a real request for no columns.

    Returns the corrected blocks by name, plus one human-readable warning
    per fix made (empty when the input was already valid) for the caller to
    surface however it surfaces warnings — this function, like the rest of
    `config.py`, never prints.

    Each corrected block is produced with ``block.model_copy(update=...)``,
    touching only the sub-field(s) that actually needed a fix, rather than
    reconstructing a fresh ``ColumnConfig``. This matters for the repo layer:
    ``Config._effective_columns`` merges over the global block field-by-field
    keyed on ``ColumnConfig.model_fields_set`` (see its docstring), so a
    reconstruction that always passes both ``fields`` and ``hide`` would mark
    a field the repo never mentioned as "explicitly set" and make it
    unconditionally override the global value — corrupting the merge for
    every repo, not just the ones with a typo. A no-op ``model_copy()`` (or
    one that only updates the field(s) actually being fixed) leaves
    ``model_fields_set`` exactly as the caller set it.
    """
    known = _known_ls_field_names()
    allowed = ", ".join(sorted(known))
    warnings: list[str] = []
    fixed: dict[str, ColumnConfig] = {}

    for block_name, block in blocks:
        updates: dict[str, object] = {}

        hide: list[str] = []
        for name in block.hide:
            if name in known:
                hide.append(name)
            else:
                warnings.append(
                    f"{block_name}.hide: unknown field {name!r} ignored; allowed: {allowed}"
                )
        if hide != block.hide:
            updates["hide"] = hide

        if block.fields is None:
            pass  # nothing to fix: unset or explicit `null` both mean "no override"

        elif not block.fields:
            warnings.append(
                f"{block_name}.fields: empty, which would render a table with "
                f"no columns; using the built-in default set"
            )
            updates["fields"] = None

        else:
            seen: set[str] = set()
            cleaned: list[str] = []
            for name in block.fields:
                if name not in known:
                    warnings.append(
                        f"{block_name}.fields: unknown field {name!r} ignored; allowed: {allowed}"
                    )
                    continue
                if name in seen:
                    warnings.append(f"{block_name}.fields: duplicate field {name!r} ignored")
                    continue
                seen.add(name)
                cleaned.append(name)

            if not cleaned:
                warnings.append(
                    f"{block_name}.fields: no valid column names remained; "
                    f"using the built-in default set"
                )
                updates["fields"] = None
            elif cleaned != block.fields:
                updates["fields"] = cleaned

        fixed[block_name] = block.model_copy(update=updates) if updates else block

    return fixed, warnings


def _columns_already_sanitized(pairs: Sequence[tuple[ColumnConfig, ColumnConfig]]) -> bool:
    """True when every ``(block, that block's default)`` pair is equal by value.

    Lets a loader skip ``sanitize_column_blocks`` (and the `lifecycle` import
    and ``ls_field_specs()`` rebuild it needs) when there is provably nothing
    to sanitize — shared by ``load_global_config`` (global.yaml layer) and
    ``load_config`` (repo layer), since both run on the dashboard's
    refresh-cadence hot path and both have "no block set at all" as the
    overwhelmingly common case.

    Comparing by *value*, not ``model_fields_set``, is deliberate and safe:
    a default ``ColumnConfig`` is ``fields=None`` plus an already-valid
    ``hide`` list, so a block equal to it cannot contain an unknown name, a
    non-null empty ``fields``, or a duplicate — the three things
    ``sanitize_column_blocks`` recovers from. That holds even when the block
    was *explicitly* set to a value that happens to equal the default (e.g.
    an explicit ``hide: []`` matching a default of ``hide: []``):
    ``sanitize_column_blocks`` only ever inspects a block's ``fields``/``hide``
    values, never its ``model_fields_set``, so a value-equal block sanitizes
    to itself unconditionally regardless of how it got set. This is exactly
    the property the repo-vs-global merge (``Config._effective_columns``)
    depends on being preserved — see the real-load-path tests named
    `..._beats_a_nonempty_global` in ``test_config.py``.
    """
    return all(block == default for block, default in pairs)


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
    `<shared_dir>/claude` + `<shared_dir>/claude.json` cache mounts (see
    `Config.effective_shared_caches`), the `CLAUDE_API_HOSTS` strict-mode
    egress auto-add, the `<shared_dir>/claude` subdir creation on
    `jailbee init`, and the claude-subdir presence check in `jailbee doctor`.
    When enabled, jailbee creates an empty `<shared_dir>/claude` directory and
    an empty `<shared_dir>/claude.json` file as bind-mount sources — Claude
    Code inside the first container runs its onboarding flow from a clean
    state. No host `~/.claude` / `~/.claude.json` is read.

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
        `claude` + `claude-json` + `claude-install` — followed by
        `jetbrains-config` + `jetbrains-data` when `jetbrains.enabled`.
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
        if self.jetbrains.enabled:
            _extend(
                _jetbrains_shared_caches(
                    self.container_prefix,
                    share_idea=self.jetbrains.share_idea,
                )
            )
        return result

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

    def effective_dashboard_columns(self, gcfg: GlobalConfig) -> ColumnConfig:
        """Column preference for the dashboards: repo block over global block."""
        return self._effective_columns(gcfg.dashboard, self.dashboard)

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

    `.` → `-` is not cosmetic: it must yield exactly `claude`, `claude-json`
    and `claude-install` for Claude's three subpaths, because those are live
    device names in every existing container's binds profile. A different rule
    renames disk devices under running containers.
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
    _, global_for_merge = _split_host_keys(global_raw)
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

    merged = deep_merge(global_for_merge, repo_raw)
    cfg = _build_config_from_dict(merged, path)

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
