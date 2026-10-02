from __future__ import annotations

import ipaddress
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jailbee.config.models_host import _PREFIX_RE

CommandMode = Literal["disabled", "allowlist", "full"]
_COMMAND_PATH_RE = re.compile(r"^[a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*)*$")


class RemoteCommandPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: CommandMode = Field(
        default="disabled",
        description="Command policy for remote shell and exec entry points.",
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
    shell: bool = Field(
        default=True,
        description="Enable the restricted JailBee console.",
    )
    exec: bool = Field(
        default=True,
        description="Enable one-shot JailBee command routing.",
    )
    default_entrypoint: Literal["help", "dashboard", "shell"] = Field(
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
    files: bool = Field(
        default=False,
        description=(
            "Let remote sessions use `sftp` and `scp` on a container's repo "
            "directory (and nothing else: never the host, never the rest of the "
            "container). Off by default; changing it needs a restart."
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
        if not (self.dashboard or self.shell or self.exec):
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
