"""Host-facing config models: container users/env, host mounts/devices/ports,
and shared-cache/pool definitions.
"""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jailbee.config.common import PathExpanded

_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_CACHE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def slugify_prefix(name: str) -> str:
    """Turn a directory name into a `container_prefix`, or `""` if impossible.

    `_PREFIX_RE` allows only `[a-z0-9][a-z0-9-]*`, and directory names
    routinely break it (`Tutkimus_A`, `my project`). Lowercases, every run of
    characters outside `[a-z0-9]` becomes one `-`, and leading/trailing `-`
    are trimmed. `""` means nothing usable survived (`"..."`), which callers
    report rather than passing on.

    Related to `lifecycle.derive_container_name`, which applies a similar
    transformation to branch names, but uses a different character class
    `[a-z0-9-]` (preserving dashes) and includes per-segment dot stripping.
    """
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class ContainerUser(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uid: int = Field(
        default=-1,
        description=(
            "Container UID. Defaults to the host uid so bind-mounted host files "
            "stay readable inside the container."
        ),
    )
    gid: int = Field(
        default=-1,
        description=(
            "Container GID. Defaults to the host gid so bind-mounted host files "
            "stay readable inside the container."
        ),
    )

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
    """Container-wide settings applied via the Incus base profile."""

    model_config = ConfigDict(extra="forbid")
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Literal string env vars injected into every process Incus starts in the "
            "container — shells, tmux, and autostart steps. Values are passed through "
            "verbatim, not shell-expanded. Keys must match `[A-Za-z_][A-Za-z0-9_]*`, and "
            "an entry here overrides jailbee's own GUI/SSH defaults for the same key."
        ),
    )

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
    host: PathExpanded = Field(
        description=(
            "Host path to bind-mount. `~` and environment variables are expanded at load time."
        ),
    )
    container: str = Field(
        description="In-container mount target.",
    )
    readonly: bool = Field(
        default=False,
        description="Read-write unless set to `true`. Set `true` for anything sensitive.",
    )


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
    path: str = Field(
        description="Device path inside the container. Must be absolute.",
    )
    source: str | None = Field(
        default=None,
        description="Host device path. Must be absolute. Defaults to the in-container `path`.",
    )
    type: Literal["unix-char", "unix-block"] = Field(
        default="unix-char",
        description="Incus device type to render this as.",
    )
    mode: str | None = Field(
        default="0666",
        description=(
            "Node mode on the Incus profile device, as an octal string. Devices with a "
            "udev `static_node` rule (e.g. `/dev/kvm`) reset to their distro default on "
            "every boot regardless of this setting — see `group`."
        ),
    )
    gid: int | None = Field(
        default=None,
        description="Node group owner on the Incus profile device. Unset leaves it unmanaged.",
    )
    uid: int | None = Field(
        default=None,
        description="Node user owner on the Incus profile device. Unset leaves it unmanaged.",
    )
    group: str | None = Field(
        default=None,
        description=(
            "Container group the `dev` user is added to so it can open the device. "
            "Unset auto-derives it from the host source node's owning group (e.g. "
            "`/dev/kvm` → `kvm`), which is the mechanism that actually grants access."
        ),
    )

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
    name: str = Field(
        description=(
            "Handle for this forward, unique within `host_ports`. Must match "
            "`[a-z0-9][a-z0-9-]*`, max 40 chars. Becomes the Incus device name "
            "`port-cfg-<name>` and the `jailbee port rm` key."
        ),
    )
    port: int = Field(
        description="Container-side port (1-65535) — what listens inside the container.",
    )
    host_port: int | None = Field(
        default=None,
        description=(
            "Host-side port the container connects to. Defaults to `port`; set this "
            "when the container-side and host-side ports differ."
        ),
    )
    proto: Literal["tcp", "udp"] = Field(
        default="tcp",
        description="Protocol for the forward.",
    )
    host_address: str = Field(
        default="127.0.0.1",
        description=(
            "Host address the container connects to. Must be an IP literal — a "
            "hostname is rejected, since resolving one at device-add time would "
            "silently pin a single IP into the device."
        ),
    )
    container_address: str = Field(
        default="127.0.0.1",
        description="Container address the proxy listens on. Must also be an IP literal.",
    )

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
    host: PathExpanded = Field(
        description=(
            "Host path to bind-mount. `~` and environment variables are expanded at load time."
        ),
    )
    container: str = Field(
        description="In-container mount target.",
    )
    readonly: bool = Field(
        default=True,
        description="Read-only by default; set `false` to mount read-write.",
    )
    description: str = Field(
        default="",
        description="Shown in the `jailbee new --mount` picker.",
    )


class PoolSpec(BaseModel):
    """Pool a shared cache: one private copy per container.

    A pooled cache is not mounted by the binds profile. Each container
    gets its own slot directory from `<shared_dir>/<host_subpath>/slots/`,
    attached as a per-container disk device named `<cache name>-slot`.
    This is how two containers avoid sharing one tool's lock files.
    """

    model_config = ConfigDict(extra="forbid")
    seed: bool = Field(
        default=True,
        description=(
            "Copy the warmest existing slot into a fresh one so a new container "
            "starts warm. `false` means every slot starts empty."
        ),
    )
    link_paths: list[str] = Field(
        default=[],
        description=(
            "Slot-relative subtrees hardlinked from the seed source instead of "
            "copied. Only for files written once and later deleted whole, never "
            "modified in place — hardlinking a lock file or an in-place-rewritten "
            "file would restore the cross-container sharing pooling exists to remove."
        ),
    )
    wipe_paths: list[str] = Field(
        default=[],
        description=(
            "Slot-relative regenerable subtrees removed when a slot is released, and "
            "excluded from seeding."
        ),
    )
    stale_globs: list[str] = Field(
        default=[],
        description=(
            "Slot-relative glob patterns unlinked on release and excluded from "
            "seeding — lock files an unclean exit left behind."
        ),
    )
    warmth_file: str | None = Field(
        default=None,
        description=(
            "Slot-relative path whose mtime ranks slots by real activity when "
            "choosing a seed. Unset ranks by the slot directory's own mtime."
        ),
    )
    allocate: Literal["on-start", "on-demand"] = Field(
        default="on-start",
        description=(
            "`on-start` attaches the slot on container create and every boot; "
            "`on-demand` waits for an explicit call (used by Chrome, which most "
            "containers never launch)."
        ),
    )


class PoolPreset(BaseModel):
    """A builtin `PoolSpec` plus whether it applies without being asked.

    `pool_only` marks a cache whose un-pooled form has no meaning: its
    `host_subpath` is the pool root itself, not a cache directory, so
    rendering it as a plain shared mount would point every container at
    `slots/`, `by-container/` and `.lock` — and the profile writing its
    own content there is exactly what `ensure_pool_dirs` then refuses.
    `pooled_caches: {<name>: false}` is rejected at load time for such a
    preset (see `_validate_pooled_caches`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    default_on: bool = Field(
        description=("Whether this cache is pooled when its name is absent from `pooled_caches`."),
    )
    spec: PoolSpec = Field(
        description="The pooling behavior applied when this cache is pooled.",
    )
    pool_only: bool = Field(
        default=False,
        description=(
            "Marks a cache whose un-pooled form has no meaning: its `host_subpath` is "
            "the pool root itself, not a cache directory, so `pooled_caches: "
            "{name: false}` is rejected at load time for it."
        ),
    )


class SharedCache(BaseModel):
    """A bind-mount from <shared_dir>/<host_subpath> into the container.

    `container_path` may start with ``~``, expanded to ``/home/<user>``.
    `pool` turns the entry into a per-container pool instead of a shared
    mount.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(
        description="Cache identifier, used as the `pooled_caches` key and slot device name.",
    )
    host_subpath: str = Field(
        description="Path under `<shared_dir>` that backs this cache on the host.",
    )
    container_path: str = Field(
        description="In-container mount target. May start with `~`, expanded to `/home/<user>`.",
    )
    pool: PoolSpec | None = Field(
        default=None,
        description=(
            "Turns this entry into a per-container pool slot instead of a live shared "
            "mount, overriding `pooled_caches` and any builtin preset for this name."
        ),
    )


# Regenerable Chrome cache subtrees, relative to a pool slot: excluded from
# seeding a fresh slot and wiped when a slot is released. Lives here (not in
# pool.py) because it's part of the "chrome-profile" POOL_PRESETS entry
# below, alongside the other builtin presets' path lists.
_CHROME_WIPE_PATHS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/Service Worker/CacheStorage",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "ShaderCache",
    "GrShaderCache",
    "GraphiteDawnCache",
    # Top-level regenerable data sets — large, redownloaded on demand,
    # not user state. Without these the freed slot stays at ~80 MB.
    "Safe Browsing",
    "optimization_guide_model_store",
    "BrowserMetrics",
)

POOL_PRESETS: dict[str, PoolPreset] = {
    "gradle": PoolPreset(
        default_on=True,
        spec=PoolSpec(
            link_paths=["caches/modules-2/files-2.1", "wrapper/dists"],
            wipe_paths=["daemon"],
            stale_globs=["**/*.lock", "**/*.lck"],
        ),
    ),
    "m2": PoolPreset(
        default_on=True,
        spec=PoolSpec(
            link_paths=["repository"],
            # `_remote.repositories` is rewritten in place by
            # maven-resolver's DefaultTrackingFileManager (RandomAccessFile
            # "rw" → setLength(0) → rewrite), so it must never be
            # hardlinked between slots.
            stale_globs=[
                "**/*.lock",
                "**/*.part*",
                "**/*.lastUpdated",
                "**/_remote.repositories",
            ],
        ),
    ),
    "npm": PoolPreset(
        default_on=False,
        spec=PoolSpec(link_paths=["_cacache"], wipe_paths=["_logs"], stale_globs=["**/*.lock"]),
    ),
    "pnpm-store": PoolPreset(
        default_on=False,
        spec=PoolSpec(link_paths=["v3/files"], stale_globs=["**/*.lock"]),
    ),
    "chrome-profile": PoolPreset(
        default_on=True,
        # `host_subpath` is "chrome-pool" — the pool root, not a cache dir.
        pool_only=True,
        spec=PoolSpec(
            link_paths=[],  # SQLite + Preferences are rewritten in place
            wipe_paths=list(_CHROME_WIPE_PATHS),
            stale_globs=["Singleton*", "BrowserMetrics-*.pma"],
            warmth_file="Default/Login Data",
            allocate="on-demand",
        ),
    ),
}


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
