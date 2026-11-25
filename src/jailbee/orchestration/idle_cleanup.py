"""Durable post-destroy acknowledgement of obsolete child idle notices."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from jailbee.orchestration.state import atomic_write_text

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jailbee.orchestration.actions import ActionContext, Node


log = logging.getLogger(__name__)


def prepare(ctx: ActionContext, child: Node) -> Path | None:
    """Persist intent before the irreversible destroy; missing identity cannot be guessed."""
    uuid = ctx.sender.config.get("volatile.uuid")
    if not uuid:
        return None
    key = hashlib.sha256(
        f"{uuid}\0{child.full}\0{child.config.get('volatile.uuid', '')}".encode()
    ).hexdigest()
    path = ctx.root / "idle-cleanup" / f"{key}.json"
    from jailbee.orchestration.outbox import load_pending
    from jailbee.orchestration.state import pending_dir
    from jailbee.orchestration.store import BrokerStore, store_path

    ids = {
        row.id
        for row in BrokerStore(store_path(ctx.root)).unacked(uuid)
        if row.type == "message" and row.kind_or_state == "idle" and row.from_name == child.short
    }
    pending: list[str] = []
    for entry in pending_dir(ctx.root, ctx.sender.full).glob("*.json"):
        try:
            parked = load_pending(entry)
        except ValueError:
            continue
        if (
            parked.recipient_uuid == uuid
            and parked.item.get("type") == "message"
            and parked.item.get("kind") == "idle"
            and parked.item.get("from") == child.short
            and isinstance(parked.item.get("id"), str)
        ):
            pending.append(entry.name)
            ids.add(str(parked.item["id"]))
    atomic_write_text(
        path,
        json.dumps(
            {
                "recipient_uuid": uuid,
                "recipient_full": ctx.sender.full,
                "child_full": child.full,
                "child_uuid": child.config.get("volatile.uuid"),
                "child_short": child.short,
                "pending": pending,
                "ids": sorted(ids),
                "at": ctx.now.isoformat(),
            }
        ),
    )
    return path


@dataclass(frozen=True)
class _Intent:
    recipient_full: str
    recipient_uuid: str
    child_full: str
    child_short: str
    child_uuid: str | None
    at: datetime
    pending: frozenset[str]
    ids: frozenset[str]


def _load(path: Path) -> _Intent | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("idle cleanup intent must be an object")
        for key in ("recipient_full", "recipient_uuid", "child_full", "child_short", "at"):
            if not isinstance(data[key], str) or not data[key]:
                raise ValueError(f"invalid idle cleanup {key}")
        if data["child_uuid"] is not None and not isinstance(data["child_uuid"], str):
            raise ValueError("invalid idle cleanup child_uuid")
        for key in ("pending", "ids"):
            if not isinstance(data[key], list) or any(
                not isinstance(value, str) or not value for value in data[key]
            ):
                raise ValueError(f"invalid idle cleanup {key}")
        return _Intent(
            recipient_full=data["recipient_full"],
            recipient_uuid=data["recipient_uuid"],
            child_full=data["child_full"],
            child_short=data["child_short"],
            child_uuid=data["child_uuid"],
            at=datetime.fromisoformat(data["at"]),
            pending=frozenset(data["pending"]),
            ids=frozenset(data["ids"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        log.exception("broker: cannot read idle cleanup intent; quarantining: %s", path)
        try:
            path.rename(path.with_name(path.name + ".bad"))
        except OSError:
            log.exception("broker: cannot quarantine idle cleanup intent: %s", path)
        return None


def settle(root: Path, path: Path | None) -> None:
    """Keep intent on a settlement I/O failure; quarantine unusable intent files."""
    from jailbee.orchestration.outbox import settle_child_idle

    if path is None or (intent := _load(path)) is None:
        return
    try:
        settle_child_idle(
            root,
            intent.recipient_full,
            intent.child_short,
            recipient_uuid=intent.recipient_uuid,
            now=intent.at,
            pending_names=intent.pending,
            ids=intent.ids,
        )
        path.unlink(missing_ok=True)
    except OSError:
        log.exception("broker: idle cleanup deferred: %s", path)


def retry(root: Path, present: Mapping[str, str]) -> None:
    """Retry after a broker restart even when the destroyed child is no longer listed."""
    for path in (root / "idle-cleanup").glob("*.json"):
        intent = _load(path)
        if intent is None:
            continue
        if intent.child_full in present and (
            not intent.child_uuid or present[intent.child_full] == intent.child_uuid
        ):
            continue  # Destruction failed, or the pre-destroy intent is still in flight.
        settle(root, path)
