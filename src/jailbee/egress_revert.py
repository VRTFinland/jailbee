"""Expire per-container egress entries through the shared host mutation path."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from jailbee.egress_scope import (
    container_extras,
    egress_deadlines,
    remove_container_entry,
    set_egress_deadlines,
)
from jailbee.loose_revert import autostart_holds

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

log = logging.getLogger(__name__)


def check_and_revert_egress(cfg: Config, incus: Incus, *, now: datetime) -> list[str]:
    removed: list[str] = []
    for raw in incus.list_containers():
        if f"{cfg.container_prefix}-base" not in (raw.get("profiles") or []):
            continue
        name = raw["name"]
        try:
            if autostart_holds(incus, name):
                continue
            deadlines = egress_deadlines(incus, name)
            extras = container_extras(incus, name)
            for entry, value in list(deadlines.items()):
                try:
                    until = datetime.fromisoformat(value)
                    valid = until.utcoffset() is not None
                except ValueError:
                    valid = False
                    until = now
                if entry not in extras or not valid:
                    deadlines.pop(entry)
                    set_egress_deadlines(incus, name, deadlines)
                elif until <= now:
                    remove_container_entry(cfg, incus, name, entry)
                    deadlines.pop(entry)
                    removed.append(f"{name}:{entry}")
        except Exception as exc:
            log.warning("egress_revert: %s raised: %s", name, exc)
    return removed
