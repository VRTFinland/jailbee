from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jailbee.config.models_host import _PREFIX_RE
from jailbee.paths import xdg_data_home

CommandMode = Literal["disabled", "allowlist", "full"]
_COMMAND_PATH_RE = re.compile(r"^[a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*$")


def _jailbee_dirs() -> tuple[Path, ...]:
    """JailBee's own config, data and state dirs, resolved.

    Spelled out here rather than imported: `global_config` imports this
    module, and `db.state_dir` drags in the database layer.
    """
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    xdg_state = os.environ.get("XDG_STATE_HOME")
    config = (Path(xdg_config) if xdg_config else Path.home() / ".config") / "jailbee"
    state = (Path(xdg_state) if xdg_state else Path.home() / ".local" / "state") / "jailbee"
    data = xdg_data_home() / "jailbee"
    return tuple(Path(os.path.realpath(p)) for p in (config, data, state))


class RemoteMountRoot(BaseModel):
    """One host directory a restricted remote session may bind into a container."""

    model_config = ConfigDict(extra="forbid")
    path: Path = Field(
        description=(
            "Host directory, absolute and fully resolved (no symlinks). An "
            "`optional_mounts` entry is attachable over remote SSH only when its "
            "host path resolves to exactly this path; subdirectories are not."
        ),
    )
    readonly: bool = Field(
        default=True,
        description="Attach read-only over remote SSH whatever the mount itself says.",
    )
    repos: list[str] = Field(
        default_factory=list,
        description="Container prefixes allowed to attach it; empty means every repo.",
    )

    @field_validator("path")
    @classmethod
    def _path_is_a_safe_resolved_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("remote.ssh.mount_roots paths must be absolute")
        resolved = Path(os.path.realpath(value))
        if str(value) != str(resolved):
            raise ValueError(
                f"remote.ssh.mount_roots path {value} is not fully resolved; write {resolved}"
            )
        home = Path(os.path.realpath(Path.home()))
        if resolved == Path("/") or home.is_relative_to(resolved):
            raise ValueError(
                f"remote.ssh.mount_roots path {resolved} is too broad: it is /, your home "
                "directory or one of its parents"
            )
        for own in _jailbee_dirs():
            if own.is_relative_to(resolved):
                raise ValueError(
                    f"remote.ssh.mount_roots path {resolved} holds JailBee's own directory {own}"
                )
        return value

    @field_validator("repos")
    @classmethod
    def _repos_are_prefixes(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("remote.ssh.mount_roots repos contains duplicates")
        if any(not _PREFIX_RE.fullmatch(v) for v in values):
            raise ValueError("remote.ssh.mount_roots repos entries must be valid container prefixes")
        return values


class RemoteCommandPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: CommandMode = Field(
        default="disabled",
        description="Command policy for remote console and exec entry points.",
    )
    allow: list[str] = Field(
        default_factory=list,
        description="Command paths accepted in allowlist mode, such as `ls` or `git pull`.",
    )

    @field_validator("allow")
    @classmethod
    def _validate_allow(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("remote.ssh.commands.allow contains duplicates")
        for value in values:
            if not _COMMAND_PATH_RE.fullmatch(value):
                raise ValueError(
                    "remote.ssh.commands.allow entries must be command paths "
                    "such as 'ls' or 'git pull'"
                )
        return values

    @model_validator(mode="after")
    def _allowlist_is_not_empty(self) -> Self:
        if self.mode == "allowlist" and not self.allow:
            raise ValueError("remote.ssh.commands.mode=allowlist requires a non-empty allow list")
        return self


class RemoteSSHConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    listen: str = Field(
        default="127.0.0.1",
        description="IP address the SSH server binds to.",
    )
    port: int = Field(
        default=8022,
        ge=1,
        le=65535,
        description="TCP port the SSH server listens on.",
    )
    dashboard: bool = Field(
        default=True,
        description="Whether SSH clients may open the dashboard.",
    )
    console: bool = Field(
        default=True,
        description="Enable the interactive JailBee console entry point.",
    )
    exec: bool = Field(
        default=True,
        description="Enable one-shot JailBee command routing.",
    )
    default_entrypoint: Literal["help", "dashboard", "console"] = Field(
        default="help",
        description="Entry point opened when an SSH client supplies no command.",
    )
    commands: RemoteCommandPolicy = Field(
        default_factory=lambda: RemoteCommandPolicy(mode="full"),
        description="Shared command policy for all remote entry points.",
    )
    restrict_host: bool = Field(
        default=True,
        description=(
            "Keep remote sessions off the host itself: no host-path arguments, no "
            "`new --mount`, no config editor, pager or GUI apps in the dashboard. "
            "`false` makes an allowed command behave exactly as it does locally."
        ),
    )
    gui: bool = Field(
        default=False,
        description=(
            "Let remote sessions launch GUI apps (`ide`, `chrome`, `firefox`, "
            "`browser`, `apps run`) onto a shared RDP display instead of the "
            "host's screen, and allow SSH port forwarding to that display only. "
            "Off by default."
        ),
    )
    network: bool = Field(
        default=False,
        description=(
            "Let remote sessions widen a container's network: `net loose` and "
            "container-scope `net egress add`. Either can open the host's LAN "
            "to the container. Narrowing (`net strict`, `net egress rm`) is "
            "always allowed; `--repo` on `net egress add`/`rm` never is. "
            "Off by default."
        ),
    )
    files: bool = Field(
        default=False,
        description=(
            "Let remote sessions use `sftp` and `scp` on a container's repo "
            "directory (and nothing else: never the host, never the rest of the "
            "container). Off by default; changing it needs a restart."
        ),
    )
    mount_roots: list[RemoteMountRoot] = Field(
        default_factory=list,
        description=(
            "Host directories a restricted session may attach with `mount`: an "
            "`optional_mounts` entry whose host path resolves to exactly one of "
            "them. Requires restrict_host=true."
        ),
    )
    mounts: bool = Field(
        default=False,
        description=(
            "Let remote sessions attach every `optional_mounts` entry as defined, "
            "with no path check. The repo config then decides which host path a "
            "remote session can reach. Off by default; prefer `mount_roots`."
        ),
    )
    excluded_repos: list[str] = Field(
        default_factory=list,
        description="Registered repository prefixes unavailable through remote SSH.",
    )

    @field_validator("listen")
    @classmethod
    def _listen_is_an_ip_literal(cls, value: str) -> str:
        ipaddress.ip_address(value)
        return value

    @model_validator(mode="after")
    def _entrypoints_are_usable(self) -> Self:
        if self.excluded_repos and not self.restrict_host:
            raise ValueError("remote.ssh.excluded_repos requires restrict_host=true")
        if self.mount_roots and not self.restrict_host:
            raise ValueError("remote.ssh.mount_roots requires restrict_host=true")
        paths = [root.path for root in self.mount_roots]
        if len(paths) != len(set(paths)):
            raise ValueError("remote.ssh.mount_roots contains duplicate paths")
        if not (self.dashboard or self.console or self.exec):
            raise ValueError("remote.ssh must enable at least one entry point")
        if self.default_entrypoint != "help" and not getattr(self, self.default_entrypoint):
            raise ValueError("remote.ssh.default_entrypoint must be an enabled entry point")
        return self

    @field_validator("excluded_repos")
    @classmethod
    def _validate_excluded_repos(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("remote.ssh.excluded_repos contains duplicates")
        if any(not _PREFIX_RE.fullmatch(value) for value in values):
            raise ValueError("remote.ssh.excluded_repos entries must be valid container prefixes")
        return values


class RemoteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ssh: RemoteSSHConfig = Field(
        default_factory=RemoteSSHConfig,
        description="Host-global SSH server settings.",
    )
