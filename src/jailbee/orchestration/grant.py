"""Orchestration grant models, stored as JSON in Incus instance labels."""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jailbee.egress import WildcardSpec, validate_allow_entry
from jailbee.orchestration.runner_model import Runner

Operation = Literal["spawn", "message", "import", "restart", "stop", "destroy", "rights"]

OPERATIONS: tuple[Operation, ...] = ("spawn", "message", "import", "restart", "stop", "destroy", "rights")
IMPLIED_OPERATIONS = frozenset({"spawn", "message"})
DEFAULT_OPERATIONS: tuple[Operation, ...] = (
    "destroy",
    "import",
    "message",
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


class Grant(_Label):
    version: Literal[1] = 1
    max_running_children: int = Field(
        DEFAULT_MAX_RUNNING_CHILDREN, ge=1, le=MAX_RUNNING_CHILDREN_CEILING
    )
    operations: tuple[Operation, ...] = DEFAULT_OPERATIONS
    grantable: Grantable = Grantable()
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
            "grantable": Grantable(
                mounts=mounts, loose=g.loose and cap.loose is not False, egress=egress
            ),
        }
    )


def egress_grantable(entry: str, eff: Grant, cap: RepoCap) -> bool:
    """Loose permission includes narrower egress, still bounded by the repo cap."""
    return egress_covered(entry, eff.grantable.egress) or (
        eff.grantable.loose
        and (cap.egress is None or egress_covered(entry, cap.egress))
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
