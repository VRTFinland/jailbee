"""Bounded, path-free transport for a dashboard's pre-confirmation clear plan.

This is untrusted CLI input, not authorization: execution still resolves scoped
containers and replans every whole-manifest deletion with fresh safety checks.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jailbee.outbox.delete import ClearItem, ContainerClearPlan, DeleteSelection
from jailbee.outbox.models import OutboxError, ProposalId

MAX_SNAPSHOT_BYTES = 64_000


class _Item(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    proposal: str = Field(max_length=512)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["pending", "partial", "applied", "uncertain", "invalid", "awaiting-pr"]
    archive: bool
    reason: str | None = Field(max_length=4096)


class _Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    available: bool
    error: str | None = Field(max_length=4096)
    delete: list[_Item] = Field(max_length=1000)
    keep: list[_Item] = Field(max_length=1000)
    warnings: list[str] = Field(max_length=1000)


class _Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int
    plans: list[_Plan] = Field(min_length=1, max_length=100)


def encode_snapshot(plans: Sequence[ContainerClearPlan]) -> str:
    def item(value: ClearItem) -> dict[str, object]:
        return {
            "proposal": str(value.proposal),
            "revision": value.revision,
            "state": value.state,
            "archive": bool(value.selection and value.selection.archive_journal),
            "reason": value.reason,
        }

    token = json.dumps(
        {
            "version": 1,
            "plans": [
                {
                    "name": p.name,
                    "available": p.available,
                    "error": p.error,
                    "delete": [item(v) for v in p.delete],
                    "keep": [item(v) for v in p.keep],
                    "warnings": list(p.warnings),
                }
                for p in plans
            ],
        },
        separators=(",", ":"),
        ensure_ascii=True,
    )
    decode_snapshot(token)
    return token


def decode_snapshot(token: str) -> tuple[ContainerClearPlan, ...]:
    try:
        if len(token.encode()) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot too large")
        snapshot = _Snapshot.model_validate_json(token)
        if snapshot.version != 1:
            raise ValueError("unsupported snapshot version")
        names: set[str] = set()
        plans = []
        for plan in snapshot.plans:
            if plan.name in names or (not plan.available and (plan.delete or plan.keep)):
                raise ValueError("invalid container plan")
            names.add(plan.name)
            ids: set[ProposalId] = set()

            def items(
                values: list[_Item], ids: set[ProposalId], *, delete: bool
            ) -> tuple[ClearItem, ...]:
                result = []
                for value in values:
                    pid = ProposalId.parse(value.proposal)
                    if pid in ids or (delete and value.reason is not None):
                        raise ValueError("invalid proposal plan")
                    ids.add(pid)
                    result.append(
                        ClearItem(
                            pid,
                            value.revision,
                            value.state,
                            DeleteSelection(archive_journal=value.archive) if delete else None,
                            value.reason,
                        )
                    )
                return tuple(result)

            plans.append(
                ContainerClearPlan(
                    plan.name,
                    plan.available,
                    plan.error,
                    items(plan.delete, ids, delete=True),
                    items(plan.keep, ids, delete=False),
                    tuple(plan.warnings),
                )
            )
        return tuple(plans)
    except (ValueError, ValidationError, RecursionError) as exc:
        # Never echo the payload (nor Pydantic's input_value) into notices.
        raise OutboxError("invalid approved clear snapshot") from exc
