"""Orchestration config models: host-level profiles and the per-repo opt-out."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from jailbee.egress import validate_allow_entry
from jailbee.orchestration.grant import (
    DEFAULT_MAX_RUNNING_CHILDREN,
    DEFAULT_OPERATIONS,
    IMPLIED_OPERATIONS,
    OPERATIONS,
    ISSUE_ACTION_KINDS,
    PublishRights,
)
from jailbee.orchestration.runner_model import DefaultRunnerSource, Runner

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


class ForeignIssueRightsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repos: list[str] = Field(default_factory=list, description="Foreign owner/name repositories, or '*' alone for any repository.")
    actions: list[str] = Field(default_factory=list, description="Allowed issue action kinds on the listed foreign repositories.")

    @field_validator("repos")
    @classmethod
    def _repos(cls, values: list[str]) -> list[str]:
        from jailbee.orchestration.grant import ForeignIssueRights
        return list(ForeignIssueRights(repos=tuple(values)).repos)

    @field_validator("actions")
    @classmethod
    def _actions(cls, values: list[str]) -> list[str]:
        return _publish_kinds(values)


def _publish_kinds(values: list[str]) -> list[str]:
    unknown = sorted(set(values) - set(ISSUE_ACTION_KINDS))
    if unknown:
        raise ValueError(f"unknown issue action(s): {', '.join(unknown)}")
    return sorted(set(values))


class PublishRightsConfig(BaseModel):
    """Editor-friendly lists; frozen tuple grant models are built at the boundary."""
    model_config = ConfigDict(extra="forbid")

    issues: list[str] = Field(default_factory=list, description="Issue action kinds allowed on the own repository and declared submodules.")
    foreign_issues: ForeignIssueRightsConfig = Field(default_factory=ForeignIssueRightsConfig, description="Additional rights on explicitly listed foreign repositories.")
    pull_requests: bool = Field(default=False, description="Publish source manifests for the orchestrator's own PR branches.")
    pr_reviews: bool = Field(default=False, description="Publish reviews and descriptions on bound PRs.")
    max_actions_per_hour: int = Field(default=30, ge=1, le=500, description="Maximum reserved actions in a sliding hour.")

    @field_validator("issues")
    @classmethod
    def _issues(cls, values: list[str]) -> list[str]:
        return _publish_kinds(values)

    def to_rights(self) -> PublishRights:
        return PublishRights.model_validate(self.model_dump())


class OrchestrationProfile(BaseModel):
    """A named set of orchestration rights."""

    model_config = ConfigDict(extra="forbid")

    max_running_children: int = Field(
        default=DEFAULT_MAX_RUNNING_CHILDREN,
        ge=1,
        le=64,
        description="Most children the holder may have running at once.",
    )
    operations: list[str] = Field(
        default_factory=lambda: [str(op) for op in DEFAULT_OPERATIONS],
        description="Operations the holder may perform on its children (spawn, message required).",
    )
    grantable: GrantableConfig = Field(
        default_factory=lambda: GrantableConfig(),
        description="What the holder may hand on to the children it spawns.",
    )

    publish: PublishRightsConfig | None = Field(default=None, description="Opt-in rights to publish this orchestrator's own issue and PR outbox manifests.")

    default_runner: Runner | None = Field(
        default=None, description="Default runner for children of a container granted this profile."
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

    default_runner: Runner | None = Field(
        default=None, description="Preferred runner for spawned children."
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

    publish: PublishRightsConfig | Literal[False] | None = Field(default=None, description="Narrow the host's publish rights; false forbids all publishing in this repo.")

    idle_after_seconds: int = Field(
        default=900,
        ge=1,
        description="Seconds awaiting input before a broker idle report; also its repeat interval.",
    )

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

    default_runner: Runner | None = Field(
        default=None, description="Preferred runner for spawned children of this repo."
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
    default_runner: Runner | None = None
    default_runner_source: DefaultRunnerSource | None = None


def build_view(
    host: OrchestrationHostConfig,
    local: OrchestrationHostConfig | None,
    repo: RepoOrchestration,
) -> OrchestrationView:
    """Merge global and host-local blocks; local profiles replace global ones by name.

    Raises `ValueError` when the resulting default names no profile.
    """
    profiles = dict(host.profiles)
    default = host.default_profile
    if local is not None:
        profiles.update(local.profiles)
        if "default_profile" in local.model_fields_set:
            default = local.default_profile
    if default is not None and default not in profiles:
        raise ValueError(f"default_profile {default!r} is not in `profiles`")
    runner: Runner | None = None
    source: DefaultRunnerSource | None = None
    if local is not None and "default_runner" in local.model_fields_set and local.default_runner:
        runner, source = local.default_runner, "local"
    elif repo.default_runner is not None:
        runner, source = repo.default_runner, "repo"
    elif host.default_runner is not None:
        runner, source = host.default_runner, "global"
    return OrchestrationView(
        profiles=profiles,
        default_profile=default,
        repo=repo,
        default_runner=runner,
        default_runner_source=source,
    )
