"""Orchestration grant models, stored as JSON in Incus instance labels."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jailbee.egress import validate_allow_entry

Operation = Literal["spawn", "message", "import", "restart", "stop", "destroy"]

OPERATIONS: tuple[Operation, ...] = ("spawn", "message", "import", "restart", "stop", "destroy")
IMPLIED_OPERATIONS = frozenset({"spawn", "message"})
DEFAULT_OPERATIONS: tuple[Operation, ...] = ("import", "message", "restart", "spawn", "stop")
DEFAULT_MAX_RUNNING_CHILDREN = 2
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
