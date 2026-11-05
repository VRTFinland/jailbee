"""Repository-defined long-running services and their preparation steps."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jailbee.config.models_net import _reject_offline

SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class ServicePrepareStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: str = Field(description="Shell command to run during preparation.")
    network: Literal["strict", "loose"] | None = Field(
        default=None, description="Temporary network mode."
    )
    mounts: list[str] = Field(
        default_factory=list, description="Optional mounts used during this step."
    )
    env: dict[str, str] = Field(
        default_factory=dict, description="Environment variables for this step."
    )
    working_dir: str = Field(
        default="", description="Working directory relative to the repository."
    )
    timeout: int | None = Field(
        default=None, description="Timeout in seconds, or the autostart default."
    )
    continue_on_error: bool = Field(
        default=False, description="Continue preparation if this step fails."
    )

    @field_validator("network", mode="before")
    @classmethod
    def _no_offline(cls, value: object) -> object:
        return _reject_offline(value)

    @property
    def elevated(self) -> bool:
        return self.network == "loose" or bool(self.mounts)


class ServiceDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    restart: bool = Field(
        default=False, description="Restart this service when the dependency restarts."
    )


class ServiceLogs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filters: dict[str, str] = Field(
        default_factory=dict,
        description="Named regular-expression filters for service log queries.",
    )
    history_limit: int = Field(
        default=50000,
        ge=1000,
        description=(
            "This service's tmux scrollback limit; session uses the maximum across services."
        ),
    )

    @field_validator("filters")
    @classmethod
    def _validate_filters(cls, filters: dict[str, str]) -> dict[str, str]:
        for name, pattern in filters.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
                raise ValueError(f"invalid filter name {name!r}")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex for filter {name!r}: {exc}") from exc
        return filters


class Service(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prepare: list[ServicePrepareStep] = Field(
        default_factory=list, description="Serial preparation steps."
    )
    run: str = Field(min_length=1, description="Long-running foreground shell command.")
    args: bool = Field(default=False, description="Allow positional runtime arguments.")
    ready: str | None = Field(default=None, description="Optional readiness probe command.")
    ready_timeout: int = Field(default=120, gt=0, description="Readiness timeout in seconds.")
    health: str | None = Field(default=None, description="Optional health probe command.")
    health_timeout: int = Field(default=5, ge=1, description="Health probe timeout in seconds.")
    status: str | None = Field(default=None, description="Optional status probe command.")
    status_timeout: int = Field(default=5, ge=1, description="Status probe timeout in seconds.")
    logs: ServiceLogs = Field(
        default_factory=ServiceLogs, description="Log query filters and retained history limit."
    )
    stop: str | None = Field(default=None, description="Optional stop command.")
    stop_timeout: int = Field(default=10, gt=0, description="Stop timeout in seconds.")
    working_dir: str = Field(
        default="", description="Working directory relative to the repository."
    )
    env: dict[str, str] = Field(default_factory=dict, description="Service environment variables.")
    depends_on: dict[str, ServiceDependency] = Field(
        default_factory=dict, description="Services required before starting."
    )
