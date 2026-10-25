"""Orchestration config models: host-level profiles and the per-repo opt-out."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from jailbee.egress import validate_allow_entry
from jailbee.orchestration.grant import DEFAULT_OPERATIONS, IMPLIED_OPERATIONS, OPERATIONS

PROFILE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def _check_egress(values: list[str] | None) -> list[str] | None:
    for entry in values or []:
        try:
            validate_allow_entry(entry)
        except ValueError as e:
            raise ValueError(f"invalid egress entry {entry!r}: {e}") from e
    return values


class GrantableConfig(BaseModel):
    """What a profile's holder may hand on to the children it spawns."""

    model_config = ConfigDict(extra="forbid")

    mounts: list[str] = Field(
        default_factory=list, description="Optional mount names the holder may hand to children."
    )
    loose: bool = Field(
        default=False, description="Whether the holder may hand loose network to children."
    )
    egress: list[str] = Field(
        default_factory=list,
        description="Egress allow entries the holder may hand to children.",
    )

    @field_validator("egress")
    @classmethod
    def _egress_valid(cls, v: list[str]) -> list[str]:
        return _check_egress(v) or []


class OrchestrationProfile(BaseModel):
    """A named set of orchestration rights."""

    model_config = ConfigDict(extra="forbid")

    max_running_children: int = Field(
        default=2, ge=1, le=64, description="Most children the holder may have running at once."
    )
    operations: list[str] = Field(
        default_factory=lambda: [str(op) for op in DEFAULT_OPERATIONS],
        description="Operations the holder may perform on its children (spawn, message required).",
    )
    grantable: GrantableConfig = Field(
        default_factory=lambda: GrantableConfig(),
        description="What the holder may hand on to the children it spawns.",
    )

    @field_validator("operations")
    @classmethod
    def _operations_valid(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(OPERATIONS))
        if unknown:
            raise ValueError(
                f"unknown operation(s) {', '.join(unknown)}; known: {', '.join(OPERATIONS)}"
            )
        missing = sorted(IMPLIED_OPERATIONS - set(v))
        if missing:
            raise ValueError(f"operations must include {', '.join(missing)}")
        return v


class OrchestrationHostConfig(BaseModel):
    """The host-level `orchestration:` block (global.yaml and the host-local file)."""

    model_config = ConfigDict(extra="forbid")

    profiles: dict[str, OrchestrationProfile] = Field(
        default_factory=dict, description="Named orchestration profiles."
    )
    default_profile: str | None = Field(
        default=None, description="Profile applied when none is named; must be a key of `profiles`."
    )

    @field_validator("profiles")
    @classmethod
    def _names_valid(cls, v: dict[str, OrchestrationProfile]) -> dict[str, OrchestrationProfile]:
        for name in v:
            if not PROFILE_NAME_RE.match(name):
                raise ValueError(
                    f"invalid profile name {name!r}: use lowercase letters, digits and "
                    "dashes, starting with a letter (max 32 characters)"
                )
        return v

    @model_validator(mode="after")
    def _default_exists(self, info: ValidationInfo) -> Self:
        partial = isinstance(info.context, dict) and info.context.get("partial") is True
        if (
            not partial
            and self.default_profile is not None
            and self.default_profile not in self.profiles
        ):
            raise ValueError(f"default_profile {self.default_profile!r} is not in `profiles`")
        return self


class RepoOrchestration(BaseModel):
    """A repo's `orchestration:` block: it can only narrow what the host grants."""

    model_config = ConfigDict(extra="forbid")

    allowed: bool = Field(
        default=True, description="Whether containers of this repo may orchestrate."
    )
    max_running_children: int | None = Field(
        default=None, ge=1, le=64, description="Cap on running children, lowering the profile's."
    )
    mounts: list[str] | None = Field(
        default=None, description="Mounts children of this repo may be given (narrows the profile)."
    )
    egress: list[str] | None = Field(
        default=None, description="Egress entries children of this repo may be given (narrows)."
    )
    loose: bool | None = Field(
        default=None, description="`false` forbids handing loose network to children in this repo."
    )

    @field_validator("egress")
    @classmethod
    def _egress_valid(cls, v: list[str] | None) -> list[str] | None:
        return _check_egress(v)


@dataclass(frozen=True)
class OrchestrationView:
    """The effective orchestration inputs for one repo."""

    profiles: Mapping[str, OrchestrationProfile] = field(default_factory=dict)
    default_profile: str | None = None
    repo: RepoOrchestration = field(default_factory=lambda: RepoOrchestration())
