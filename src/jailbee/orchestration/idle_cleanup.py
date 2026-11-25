"""Durable post-destroy acknowledgement of obsolete child idle notices."""

from __future__ import annotations

import hashlib
import json
import logging
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


def settle(root: Path, path: Path | None) -> None:
    """Keep intent on an I/O failure, without hiding a successful destruction."""
    from jailbee.orchestration.outbox import settle_child_idle

    if path is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        settle_child_idle(
            root,
            data["recipient_full"],
            data["child_short"],
            recipient_uuid=data["recipient_uuid"],
            now=datetime.fromisoformat(data["at"]),
            pending_names=frozenset(data["pending"]),
            ids=frozenset(data["ids"]),
        )
        path.unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError):
        log.exception("broker: idle cleanup deferred: %s", path)


def retry(root: Path, present: Mapping[str, str]) -> None:
    """Retry after a broker restart even when the destroyed child is no longer listed."""
    for path in (root / "idle-cleanup").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            full = data["child_full"]
            uuid = data["child_uuid"]
            if full in present and (not uuid or present[full] == uuid):
                continue  # Destruction failed, or the pre-destroy intent is still in flight.
        except (OSError, ValueError, KeyError, TypeError):
            log.exception("broker: cannot read idle cleanup intent: %s", path)
            continue
        settle(root, path)
