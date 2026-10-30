"""Publish child lifecycle observations for non-consuming inbox wakeups."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from jailbee.orchestration import labels
from jailbee.orchestration.outbox import _BASE, _atomic_write
from jailbee.orchestration.state import atomic_write_text

if TYPE_CHECKING:
    from jailbee.orchestration.actions import ActionContext


def sync_children(ctx: ActionContext) -> None:
    """Refresh the heartbeat and retain fallback boot times across broker restarts."""
    path = ctx.root / "inbox-state" / f"{ctx.sender.full}.json"
    previous: dict[str, dict[str, object]] = {}
    try:
        saved = json.loads(path.read_text())
        if saved["owner"] == ctx.sender.key:
            previous = {c["full"]: c for c in saved["children"]}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    children = []
    durable = []
    for child in sorted(ctx.children(), key=lambda c: c.short):
        old = previous.get(child.full, {})
        started = child.started_at
        if started is None:
            started = ctx.now.timestamp()
            if old.get("key") == child.key and (not child.running or old.get("running")):
                value = old.get("started_at")
                if isinstance(value, (int, float)):
                    started = value
        # A finished child may keep running until import/destroy; it must not
        # trip the helper's quiet watchdog, so it is published as not running.
        item = {
            "name": child.short,
            "running": child.running and not labels.is_finished(child.config),
            "started_at": started,
        }
        children.append(item)
        durable.append({**item, "running": child.running, "full": child.full, "key": child.key})
    atomic_write_text(path, json.dumps({"owner": ctx.sender.key, "children": durable}))
    text = json.dumps({"version": 1, "updated_at": ctx.now.timestamp(), "children": children})
    _atomic_write(
        ctx.incus,
        ctx.sender.full,
        _BASE,
        "children.json",
        text + "\n",
        "0444",
        uid=ctx.cfg.container_user.uid,
    )
