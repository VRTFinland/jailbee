"""Orchestration config models: host-level profiles and the per-repo opt-out."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from jailbee.config.errors import ConfigError
from jailbee.egress import validate_allow_entry
from jailbee.inbox.cli import parse_duration
from jailbee.inbox.errors import InboxUsage
from jailbee.inbox.waiting import valid_wake_token
from jailbee.orchestration.grant import (
    DEFAULT_MAX_RUNNING_CHILDREN,
    DEFAULT_OPERATIONS,
    IMPLIED_OPERATIONS,
    ISSUE_ACTION_KINDS,
    OPERATIONS,
    ProjectActionKind,
    ProjectRights,
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

    repos: list[str] = Field(
        default_factory=list,
        description="Foreign owner/name repositories, or '*' alone for any repository.",
    )
    actions: list[str] = Field(
        default_factory=list,
        description=(
            "Allowed issue action kinds on the listed foreign repositories "
            "(assign_self: assign issues to itself, and unassign itself, even from issues "
            "the user claimed by hand; assign: assign issues to anyone)."
        ),
    )

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


class ProjectRightsConfig(BaseModel):
    """Editor-friendly board entry, validated with the grant's offline rules."""

    model_config = ConfigDict(extra="forbid")

    board: str = Field(description="Project board as owner/number; wildcards are not allowed.")
    actions: list[ProjectActionKind] = Field(
        default_factory=list,
        description="Allowed project actions: add, set (also add/clear), remove.",
    )

    @field_validator("board")
    @classmethod
    def _board(cls, value: str) -> str:
        return ProjectRights(board=value).board

    @field_validator("actions")
    @classmethod
    def _actions(cls, values: list[ProjectActionKind]) -> list[ProjectActionKind]:
        return sorted(set(values))


class PublishRightsConfig(BaseModel):
    """Editor-friendly lists; frozen tuple grant models are built at the boundary."""

    model_config = ConfigDict(extra="forbid")

    issues: list[str] = Field(
        default_factory=list,
        description=(
            "Issue action kinds allowed on the own repository and declared submodules "
            "(assign_self: assign issues to itself, and unassign itself, even from issues "
            "the user claimed by hand; assign: assign issues to anyone)."
        ),
    )
    foreign_issues: ForeignIssueRightsConfig = Field(
        default_factory=ForeignIssueRightsConfig,
        description="Additional rights on explicitly listed foreign repositories.",
    )
    projects: list[ProjectRightsConfig] = Field(
        default_factory=list, description="Board-scoped project publication rights."
    )
    pull_requests: bool = Field(
        default=False,
        description="Publish source manifests for the orchestrator's own PR branches.",
    )
    pr_reviews: bool = Field(
        default=False, description="Publish reviews and descriptions on bound PRs."
    )
    pr_comment_edits: bool = Field(
        default=False,
        description="Edit submitted PR review comments on bound PRs (pr_reviews does not imply it).",
    )
    max_actions_per_hour: int = Field(
        default=30, ge=1, le=500, description="Maximum reserved actions in a sliding hour."
    )

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

    publish: PublishRightsConfig | None = Field(
        default=None,
        description="Opt-in publication rights for this orchestrator's own issue and PR outboxes.",
    )

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


class FollowConfig(BaseModel):
    """Notification policy for an orchestrator's managed inbox follower."""

    model_config = ConfigDict(extra="forbid")

    wake_on: list[str] | None = Field(
        default_factory=lambda: [
            "msg:done",
            "msg:question",
            "msg:blocked",
            "msg:idle",
            "msg:note",
            "status:failed",
            "status:refused",
        ],
        description="Wait wake tokens; null announces every unread item.",
    )
    quiet_after: str = Field(default="120m", description="Quiet-child interval, or off.")
    digest: str = Field(default="off", description="Status digest interval, or off.")

    @field_validator("wake_on")
    @classmethod
    def _wake_tokens(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and not all(valid_wake_token(token) for token in values):
            raise ValueError("wake_on must contain valid inbox wait wake tokens")
        return values

    @field_validator("quiet_after", "digest", mode="before")
    @classmethod
    def _yaml_interval(cls, value: object) -> object:
        # YAML 1.1 reads unquoted off as false and bare seconds as integers.
        if value is False:
            return "off"
        return str(value) if type(value) is int else value

    @field_validator("quiet_after", "digest")
    @classmethod
    def _interval(cls, value: str) -> str:
        if value == "off":
            return value
        try:
            seconds = parse_duration(value)
        except InboxUsage as exc:
            raise ValueError(str(exc)) from exc
        if seconds <= 0:
            raise ValueError("follow interval must be positive, or off")
        return value


class RepoOrchestration(BaseModel):
    """A repo's `orchestration:` block: it can only narrow what the host grants."""

    model_config = ConfigDict(extra="forbid")

    publish: PublishRightsConfig | Literal[False] | None = Field(
        default=None,
        description="Narrow the host's publish rights; false forbids all publishing in this repo.",
    )

    follow: FollowConfig = Field(
        default_factory=FollowConfig,
        description="Managed inbox follower policy for orchestrators; children stay unfiltered.",
    )

    idle_after_seconds: int = Field(
        default=900,
        ge=1,
        description="Seconds awaiting input before a broker idle report; also its repeat interval.",
    )

    message_retention_days: int = Field(
        default=30,
        ge=1,
        description=(
            "Days the broker keeps a container's read (acked) orchestration messages "
            "before deleting them."
        ),
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


def peel_agents_runner(
    raw: dict[str, object], origin: str
) -> tuple[dict[str, object], Runner | None]:
    """Split `agents.default_runner` off one raw layer, validated.

    `agents` is a mapping of agent name to settings, so the key is not an agent:
    it is peeled before the layers merge and never reaches `Config.agents`.
    The error names `origin`, the file the key came from.
    """
    agents = raw.get("agents")
    if not isinstance(agents, dict) or "default_runner" not in agents:
        return raw, None
    rest = {k: v for k, v in agents.items() if k != "default_runner"}
    out = {k: v for k, v in raw.items() if k != "agents"}
    if rest:
        out["agents"] = rest
    block = agents["default_runner"]
    if block is None:
        return out, None
    try:
        return out, Runner.model_validate(block)
    except ValidationError as e:
        raise ConfigError(f"Invalid `agents.default_runner` in {origin}:\n{e}") from e


@dataclass(frozen=True)
class AgentsRunnerLayers:
    """`agents.default_runner` as each layer set it (None: the layer did not)."""

    host: Runner | None = None
    repo: Runner | None = None
    local: Runner | None = None


def build_view(
    host: OrchestrationHostConfig,
    local: OrchestrationHostConfig | None,
    repo: RepoOrchestration,
    agents: AgentsRunnerLayers | None = None,
) -> OrchestrationView:
    """Merge global and host-local blocks; local profiles replace global ones by name.

    The default runner layers local -> repo -> global; within a layer the
    `agents.default_runner` key beats its deprecated alias
    `orchestration.default_runner`.

    Raises `ValueError` when the resulting default names no profile.
    """
    agents = agents or AgentsRunnerLayers()
    profiles = dict(host.profiles)
    default = host.default_profile
    if local is not None:
        profiles.update(local.profiles)
        if "default_profile" in local.model_fields_set:
            default = local.default_profile
    if default is not None and default not in profiles:
        raise ValueError(f"default_profile {default!r} is not in `profiles`")
    local_alias = (
        local.default_runner
        if local is not None and "default_runner" in local.model_fields_set
        else None
    )
    runner: Runner | None = None
    source: DefaultRunnerSource | None = None
    layered: tuple[tuple[DefaultRunnerSource, Runner | None], ...] = (
        ("local", agents.local or local_alias),
        ("repo", agents.repo or repo.default_runner),
        ("global", agents.host or host.default_runner),
    )
    for layer, candidate in layered:
        if candidate is not None:
            runner, source = candidate, layer
            break
    return OrchestrationView(
        profiles=profiles,
        default_profile=default,
        repo=repo,
        default_runner=runner,
        default_runner_source=source,
    )
