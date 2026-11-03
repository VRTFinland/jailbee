"""Validate accepted refresh plans and their declared temporary rights."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jailbee.config.models_services import SERVICE_NAME_RE
from jailbee.orchestration.grant import ChildRights, Grant, check_rights
from jailbee.orchestration.profiles import repo_cap

if TYPE_CHECKING:
    from jailbee.config import Config


class RefreshError(RuntimeError):
    """A refresh plan cannot safely be accepted."""


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class _Step(_Strict):
    name: str
    run: str
    env: dict[str, str]
    working_dir: str
    timeout: int = Field(gt=0)
    continue_on_error: bool


class _Elevation(_Strict):
    network: Literal["loose"] | None
    mounts: list[str]


class _Service(_Strict):
    name: str
    depends_on: list[str]
    will_prepare: bool
    prepare: list[_Step]
    elevation: _Elevation
    ready_timeout: int = Field(gt=0)
    stop_timeout: int = Field(gt=0)


class _Payload(_Strict):
    version: Literal[1]
    op: Literal["refresh"]
    order: list[str]
    services: list[_Service]


def _name(value: str) -> bool:
    return bool(SERVICE_NAME_RE.fullmatch(value))


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True)
class PlannedService:
    name: str
    will_prepare: bool
    prepare: tuple[Mapping[str, object], ...]
    network: Literal["loose"] | None
    mounts: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    ready_timeout: int = 120
    stop_timeout: int = 10

    @property
    def elevated(self) -> bool:
        return self.will_prepare and (self.network == "loose" or bool(self.mounts))


@dataclass(frozen=True)
class Plan:
    order: tuple[str, ...]
    named: tuple[str, ...]
    services: tuple[PlannedService, ...]

    @property
    def elevated(self) -> bool:
        return any(service.elevated for service in self.services)

    def declared(self) -> ChildRights:
        return ChildRights(
            loose=any(s.elevated and s.network == "loose" for s in self.services),
            mounts=tuple(m for s in self.services if s.elevated for m in s.mounts),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "version": 1,
            "op": "refresh",
            "order": list(self.order),
            "services": [
                {
                    "name": s.name,
                    "depends_on": list(s.depends_on),
                    "will_prepare": s.will_prepare,
                    "prepare": [dict(step) for step in s.prepare],
                    "elevation": {"network": s.network, "mounts": list(s.mounts)},
                    "ready_timeout": s.ready_timeout,
                    "stop_timeout": s.stop_timeout,
                }
                for s in self.services
            ],
        }

    @classmethod
    def from_payload(cls, raw: str | bytes, named: Sequence[str]) -> "Plan":
        try:
            data = raw.encode("utf-8") if isinstance(raw, str) else raw
            if not isinstance(data, bytes) or len(data) > 256 * 1024:
                raise ValueError("invalid payload size")
            decoded = json.loads(data, object_pairs_hook=_object)
            # Literal[1] otherwise accepts True because bool is an int subclass.
            if not isinstance(decoded, dict) or type(decoded.get("version")) is not int:
                raise ValueError("invalid version")
            payload = _Payload.model_validate(decoded)
            if isinstance(named, (str, bytes)) or any(
                not isinstance(n, str) or not _name(n) for n in named
            ):
                raise ValueError("invalid requested service names")
            targets = tuple(named) if named else tuple(payload.order)
            if len(set(targets)) != len(targets) or not set(targets) <= set(payload.order):
                raise ValueError("invalid requested services")
            if (
                any(not _name(n) for n in payload.order)
                or len(set(payload.order)) != len(payload.order)
                or payload.order != [s.name for s in payload.services]
            ):
                raise ValueError("invalid service order")
            for s in payload.services:
                if (
                    any(not _name(n) for n in s.depends_on)
                    or any(not m for m in s.elevation.mounts)
                    or s.will_prepare != (s.name in targets and bool(s.prepare))
                ):
                    raise ValueError("invalid service declaration")
            services = tuple(
                PlannedService(
                    name=s.name,
                    will_prepare=s.will_prepare,
                    prepare=tuple(step.model_dump() for step in s.prepare),
                    network=s.elevation.network,
                    mounts=tuple(s.elevation.mounts),
                    depends_on=tuple(s.depends_on),
                    ready_timeout=s.ready_timeout,
                    stop_timeout=s.stop_timeout,
                )
                for s in payload.services
            )
            return cls(tuple(payload.order), targets, services)
        except (ValueError, TypeError, UnicodeError, ValidationError, RecursionError) as exc:
            raise RefreshError(f"invalid refresh plan: {exc}") from exc


def orchestrator_policy(cfg: "Config", grant: Grant, plan: Plan) -> list[str]:
    return check_rights(grant, repo_cap(cfg), plan.declared(), cfg.optional_mounts.keys())


def host_policy(cfg: "Config", plan: Plan) -> list[str]:
    return [
        f"service {s.name!r} prepare needs optional mount {mount!r}, "
        "which this host does not define"
        for s in plan.services
        if s.will_prepare
        for mount in s.mounts
        if mount not in cfg.optional_mounts
    ]
