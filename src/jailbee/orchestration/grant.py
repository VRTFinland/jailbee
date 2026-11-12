"""Orchestration grant models, stored as JSON in Incus instance labels."""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jailbee.egress import WildcardSpec, validate_allow_entry
from jailbee.issue_manifest import valid_project
from jailbee.orchestration.runner_model import Runner

Operation = Literal["spawn", "message", "import", "restart", "stop", "destroy", "rights", "refresh"]

OPERATIONS: tuple[Operation, ...] = (
    "spawn",
    "message",
    "import",
    "restart",
    "refresh",
    "stop",
    "destroy",
    "rights",
)
IMPLIED_OPERATIONS = frozenset({"spawn", "message"})
DEFAULT_OPERATIONS: tuple[Operation, ...] = (
    "destroy",
    "import",
    "message",
    "refresh",
    "restart",
    "rights",
    "spawn",
    "stop",
)
DEFAULT_MAX_RUNNING_CHILDREN = 4
MAX_RUNNING_CHILDREN_CEILING = 64


class GrantError(ValueError):
    """A grant or rights label cannot be used; the message is user-facing."""


def _sorted_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


def _checked_egress(values: tuple[str, ...]) -> tuple[str, ...]:
    for entry in values:
        try:
            validate_allow_entry(entry)
        except ValueError as e:
            raise ValueError(f"invalid egress entry {entry!r}: {e}") from e
    return _sorted_unique(values)


class _Label(BaseModel):
    """Shared JSON label (de)serialisation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    def to_label(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_label(cls, raw: str) -> Self:
        try:
            return cls.model_validate_json(raw)
        except ValidationError as e:
            raise GrantError(f"invalid grant label: {e}") from e


class Grantable(BaseModel):
    """What a granted container may hand on to the children it spawns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mounts: tuple[str, ...] = ()
    loose: bool = False
    egress: tuple[str, ...] = ()

    @field_validator("mounts")
    @classmethod
    def _mounts(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(v)

    @field_validator("egress")
    @classmethod
    def _egress(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _checked_egress(v)


IssueActionKind = Literal["create", "edit", "comment", "labels", "state", "assign_self", "assign"]
ISSUE_ACTION_KINDS: tuple[IssueActionKind, ...] = (
    "assign",
    "assign_self",
    "comment",
    "create",
    "edit",
    "labels",
    "state",
)


def kind_granted(kind: str, granted: Iterable[str]) -> bool:
    """Whether ``granted`` issue kinds cover ``kind``; `assign` implies `assign_self`."""
    held = set(granted)
    return kind in held or (kind == "assign_self" and "assign" in held)


def narrow_kinds(
    kinds: tuple[IssueActionKind, ...], cap: tuple[IssueActionKind, ...]
) -> tuple[IssueActionKind, ...]:
    """The kinds of ``kinds`` that ``cap`` covers, `assign` degrading to `assign_self`."""
    out = {k for k in kinds if kind_granted(k, cap)}
    if "assign" in kinds and "assign" not in cap and "assign_self" in cap:
        out.add("assign_self")
    return tuple(sorted(out))


class ForeignIssueRights(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: tuple[str, ...] = Field(
        default=(), description="Foreign owner/name repositories, or '*' alone for any repository."
    )
    actions: tuple[IssueActionKind, ...] = Field(
        default=(),
        description=(
            "Issue action kinds permitted on the listed foreign repositories "
            "(assign_self: assign issues to itself, and unassign itself, even from issues "
            "the user claimed by hand; assign: assign issues to anyone)."
        ),
    )

    @field_validator("repos")
    @classmethod
    def _repos(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        repos = tuple(sorted({v.casefold() for v in values}))
        if "*" in repos:
            if len(repos) != 1:
                raise ValueError("'*' must be the only foreign repository")
        elif any(not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", r) for r in repos):
            raise ValueError("foreign repositories must be owner/name slugs")
        return repos

    @field_validator("actions")
    @classmethod
    def _actions(cls, values: tuple[IssueActionKind, ...]) -> tuple[IssueActionKind, ...]:
        return tuple(sorted(set(values)))


ProjectActionKind = Literal["add", "set", "remove"]
PROJECT_ACTION_KINDS: tuple[ProjectActionKind, ...] = ("add", "remove", "set")


def project_kind_granted(kind: str, granted: Iterable[str]) -> bool:
    held = set(granted)
    return kind in held or (kind in {"add", "clear"} and "set" in held)


class ProjectRights(BaseModel):
    """Offline, board-scoped publication rights; no repository permission implied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    board: str
    actions: tuple[ProjectActionKind, ...] = ()

    @field_validator("board")
    @classmethod
    def _board(cls, value: str) -> str:
        if not valid_project(value):
            raise ValueError("project board must be owner/number (positive number, no wildcard)")
        return value.casefold()

    @field_validator("actions")
    @classmethod
    def _actions(cls, values: tuple[ProjectActionKind, ...]) -> tuple[ProjectActionKind, ...]:
        return tuple(sorted(set(values)))


class PublishRights(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    issues: tuple[IssueActionKind, ...] = Field(
        default=(),
        description=(
            "Issue actions allowed on the own repository and declared submodules "
            "(assign_self: assign issues to itself, and unassign itself, even from issues "
            "the user claimed by hand; assign: assign issues to anyone)."
        ),
    )
    foreign_issues: ForeignIssueRights = Field(
        default=ForeignIssueRights(),
        description="Additional issue rights for explicitly listed foreign repositories.",
    )
    projects: tuple[ProjectRights, ...] = Field(
        default=(), description="Publication actions allowed on explicitly named project boards."
    )
    pull_requests: bool = Field(
        default=False,
        description="Publish source manifests for the orchestrator's own PR branches.",
    )
    pr_reviews: bool = Field(
        default=False, description="Publish comments, replies and descriptions on bound PRs."
    )
    max_actions_per_hour: int = Field(
        default=30,
        ge=1,
        le=500,
        description="Maximum reserved publish actions per orchestrator in a sliding hour.",
    )

    @field_validator("issues")
    @classmethod
    def _issues(cls, values: tuple[IssueActionKind, ...]) -> tuple[IssueActionKind, ...]:
        return tuple(sorted(set(values)))

    @field_validator("projects")
    @classmethod
    def _projects(cls, values: tuple[ProjectRights, ...]) -> tuple[ProjectRights, ...]:
        boards: dict[str, set[ProjectActionKind]] = {}
        for entry in values:
            boards.setdefault(entry.board, set()).update(entry.actions)
        return tuple(
            ProjectRights(board=board, actions=tuple(sorted(actions)))
            for board, actions in sorted(boards.items())
            if actions
        )

    def covers_project(self, kind: str, board: str) -> bool:
        if kind not in {"project_add", "project_set", "project_clear", "project_remove"}:
            return False
        return any(
            entry.board == board.casefold()
            and project_kind_granted(kind.removeprefix("project_"), entry.actions)
            for entry in self.projects
        )

    def empty(self) -> bool:
        return not (
            self.issues
            or self.projects
            or (self.foreign_issues.repos and self.foreign_issues.actions)
            or self.pull_requests
            or self.pr_reviews
        )

    def covers_issue(self, kind: str, slug: str, *, foreign: bool) -> bool:
        if not foreign:
            return kind_granted(kind, self.issues)
        f = self.foreign_issues
        return kind_granted(kind, f.actions) and ("*" in f.repos or slug.casefold() in f.repos)

    def narrowed(self, cap: PublishRights | None) -> PublishRights:
        if cap is None:
            return self
        f, c = self.foreign_issues, cap.foreign_issues
        if "*" in f.repos:
            repos = c.repos
        elif "*" in c.repos:
            repos = f.repos
        else:
            repos = tuple(r for r in f.repos if r in c.repos)
        caps = {entry.board: entry.actions for entry in cap.projects}
        projects = []
        for entry in self.projects:
            allowed = caps.get(entry.board, ())
            actions = {kind for kind in entry.actions if project_kind_granted(kind, allowed)}
            if "set" in entry.actions and "set" not in allowed and "add" in allowed:
                actions.add("add")
            if actions:
                projects.append(ProjectRights(board=entry.board, actions=tuple(sorted(actions))))
        return PublishRights(
            projects=tuple(projects),
            issues=narrow_kinds(self.issues, cap.issues),
            foreign_issues=ForeignIssueRights(
                repos=repos, actions=narrow_kinds(f.actions, c.actions)
            ),
            pull_requests=self.pull_requests and cap.pull_requests,
            pr_reviews=self.pr_reviews and cap.pr_reviews,
            max_actions_per_hour=min(self.max_actions_per_hour, cap.max_actions_per_hour),
        )


class Grant(_Label):
    version: Literal[1] = 1
    max_running_children: int = Field(
        DEFAULT_MAX_RUNNING_CHILDREN, ge=1, le=MAX_RUNNING_CHILDREN_CEILING
    )
    operations: tuple[Operation, ...] = DEFAULT_OPERATIONS
    grantable: Grantable = Grantable()
    publish: PublishRights = PublishRights()
    default_runner: Runner | None = None

    @field_validator("operations")
    @classmethod
    def _operations(cls, v: tuple[Operation, ...]) -> tuple[Operation, ...]:
        ops = tuple(sorted(set(v)))
        missing = sorted(IMPLIED_OPERATIONS - set(ops))
        if missing:
            raise ValueError(f"operations must include {', '.join(missing)}")
        return ops


class ChildRights(_Label):
    version: Literal[1] = 1
    mounts: tuple[str, ...] = ()
    loose: bool = False
    egress: tuple[str, ...] = ()

    @field_validator("mounts")
    @classmethod
    def _mounts(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(v)

    @field_validator("egress")
    @classmethod
    def _egress(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _checked_egress(v)


@dataclass(frozen=True)
class RepoCap:
    """Per-repo ceiling on grants; None means no restriction."""

    allowed: bool = True
    max_running_children: int | None = None
    mounts: tuple[str, ...] | None = None
    egress: tuple[str, ...] | None = None
    loose: bool | None = None
    publish: PublishRights | Literal[False] | None = None


_NOT_ALLOWED = "orchestration is not allowed in this repo"


def _parse(entry: str) -> tuple[bool, str, int | None]:
    """(is_wildcard, lower-cased host or domain, port) of one egress entry."""
    spec = validate_allow_entry(entry)
    if isinstance(spec, WildcardSpec):
        return True, spec.domain.lower(), spec.port
    return False, spec.target.lower(), spec.port


def egress_covered(requested: str, allowed: Iterable[str]) -> bool:
    """True when `requested` is permitted by one of the `allowed` egress entries."""
    req_wild, req_name, req_port = _parse(requested)
    for entry in allowed:
        wild, name, port = _parse(entry)
        if port != req_port:
            continue
        if not wild:
            if not req_wild and req_name == name:
                return True
        elif req_name == name or req_name.endswith("." + name):
            return True
    return False


def cap_excess(grant: Grant, cap: RepoCap) -> list[str]:
    """Human messages for everything in `grant` that exceeds `cap`; empty = within."""
    if not cap.allowed:
        return [f"{_NOT_ALLOWED} (.jailbee/config.yaml)"]
    out: list[str] = []
    limit = cap.max_running_children
    if limit is not None and grant.max_running_children > limit:
        out.append(
            f"max_running_children {grant.max_running_children} is above the repo cap of {limit}"
        )
    if cap.mounts is not None:
        out.extend(
            f"mount {m!r} is not allowed by the repo cap"
            for m in grant.grantable.mounts
            if m not in cap.mounts
        )
    if cap.egress is not None:
        out.extend(
            f"egress {e!r} is not covered by the repo cap"
            for e in grant.grantable.egress
            if not egress_covered(e, cap.egress)
        )
    if grant.grantable.loose and cap.loose is False:
        out.append("loose network mode is forbidden by the repo cap")
    if cap.publish is not None:
        requested = grant.publish
        narrowed = PublishRights() if cap.publish is False else requested.narrowed(cap.publish)
        for kind in requested.issues:
            if not kind_granted(kind, narrowed.issues):
                out.append(f"publish right `issues: {kind}` is above the repo cap")
        for repo in requested.foreign_issues.repos:
            for kind in requested.foreign_issues.actions:
                if not narrowed.covers_issue(kind, repo, foreign=True):
                    out.append(
                        f"publish right `foreign_issues: {repo}:{kind}` is above the repo cap"
                    )
        for entry in requested.projects:
            for project_kind in entry.actions:
                if not narrowed.covers_project(f"project_{project_kind}", entry.board):
                    out.append(
                        f"publish right `projects: {entry.board}:{project_kind}` "
                        "is above the repo cap"
                    )
        for name in ("pull_requests", "pr_reviews"):
            if getattr(requested, name) and not getattr(narrowed, name):
                out.append(f"publish right `{name}` is above the repo cap")
        if (
            cap.publish is not False
            and requested.max_actions_per_hour > narrowed.max_actions_per_hour
        ):
            out.append(
                f"publish rate {requested.max_actions_per_hour} is above the repo cap "
                f"of {narrowed.max_actions_per_hour}"
            )
    return out


def effective_grant(grant: Grant, cap: RepoCap) -> Grant | None:
    """`grant` clamped to `cap`; None when the repo forbids orchestration."""
    if not cap.allowed:
        return None
    g = grant.grantable
    limit = grant.max_running_children
    if cap.max_running_children is not None:
        limit = min(limit, cap.max_running_children)
    mounts = g.mounts if cap.mounts is None else tuple(m for m in g.mounts if m in cap.mounts)
    egress = (
        g.egress
        if cap.egress is None
        else tuple(e for e in g.egress if egress_covered(e, cap.egress))
    )
    return grant.model_copy(
        update={
            "max_running_children": limit,
            "publish": PublishRights()
            if cap.publish is False
            else grant.publish.narrowed(cap.publish),
            "grantable": Grantable(
                mounts=mounts, loose=g.loose and cap.loose is not False, egress=egress
            ),
        }
    )


def egress_grantable(entry: str, eff: Grant, cap: RepoCap) -> bool:
    """Loose permission includes narrower egress, still bounded by the repo cap."""
    return egress_covered(entry, eff.grantable.egress) or (
        eff.grantable.loose and (cap.egress is None or egress_covered(entry, cap.egress))
    )


def check_rights(
    grant: Grant, cap: RepoCap, rights: ChildRights, optional_mounts: Collection[str]
) -> list[str]:
    """Why a child may not receive `rights` from `grant` under `cap`; empty = fine."""
    eff = effective_grant(grant, cap)
    if eff is None:
        return [_NOT_ALLOWED]
    out: list[str] = []
    for m in rights.mounts:
        if m not in eff.grantable.mounts:
            out.append(f"mount {m!r} is not grantable")
        elif m not in optional_mounts:
            out.append(f"mount {m!r} is not an optional mount of this repo")
    if rights.loose and not eff.grantable.loose:
        out.append("loose network mode is not grantable")
    out.extend(
        f"egress {e!r} is not covered by the grant"
        for e in rights.egress
        if not egress_grantable(e, eff, cap)
    )
    return out
