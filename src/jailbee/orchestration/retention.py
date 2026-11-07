"""Message retention and periodic purge for the orchestration broker."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jailbee.orchestration import broker_state

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from jailbee.config import Config

log = logging.getLogger(__name__)

PURGE_INTERVAL = 3600.0
DEFAULT_RETENTION_DAYS = 30


def _retention_days(
    full: str, config: Mapping[str, str], configs: Mapping[str, Config | None]
) -> int:
    cfg = broker_state.owner_config(full, config, configs)
    if cfg is None:
        return DEFAULT_RETENTION_DAYS
    return cfg.orchestration_view().repo.message_retention_days


def purge_messages(
    memory: broker_state.BrokerMemory,
    raw: list[dict[str, object]],
    now: datetime,
    everywhere: list[dict[str, object]] | None = None,
) -> None:
    """Once per hour: age out acked messages per repo and drop vanished containers' rows.

    ``raw`` is the current project's listing, ``everywhere`` every project's
    (None when it could not be listed: then no container counts as vanished).
    Never raises: a failed purge is logged and tried again next hour.
    """
    store = memory.store
    if store is None:
        return
    mono = broker_state.monotonic()
    if memory.last_purge is not None and mono - memory.last_purge < PURGE_INTERVAL:
        return
    memory.last_purge = mono
    configs: dict[str, Config | None] | None = None
    try:
        entry_config: dict[str, dict[str, str]] = {}
        for e in raw:
            conf = e.get("config")
            if isinstance(e.get("name"), str) and isinstance(conf, dict):
                entry_config[str(e["name"])] = conf
        live = {
            str(conf["volatile.uuid"])
            for e in everywhere or []
            if isinstance(conf := e.get("config"), dict) and conf.get("volatile.uuid")
        }
        # A uuid is gone only when it is in no project at all (an `incus
        # project switch` hides nothing from `--all-projects`), and only when
        # two consecutive purges (an hour apart) missed it. Its unread items
        # still stay for the retention of the repo it belonged to. An empty
        # or failed listing never counts as an absence.
        if live:
            absent = store.known_uuids() - live
            gone = absent & memory.absent_uuids
            memory.absent_uuids = absent
            if gone:
                configs = broker_state.repo_configs(memory)
                names = store.recipient_names(gone)
                by_days: dict[int, set[str]] = {}
                for uuid in gone:
                    full = names.get(uuid)
                    days = _retention_days(full, {}, configs) if full else DEFAULT_RETENTION_DAYS
                    by_days.setdefault(days, set()).add(uuid)
                removed = 0
                for days, uuids in by_days.items():
                    removed += store.purge_uuids(
                        uuids, keep_unacked_after=now.timestamp() - days * 86400
                    )
                if removed:
                    log.info("broker: purged %d message rows of vanished containers", removed)
        recipients = store.recipients()
        if not recipients:
            return
        if configs is None:
            configs = broker_state.repo_configs(memory)
    except Exception:
        log.exception("broker: message purge failed")
        return
    for full, uuid in recipients.items():
        try:
            days = DEFAULT_RETENTION_DAYS
            if full in entry_config:
                days = _retention_days(full, entry_config[full], configs)
            store.purge_acked_older_than(uuid, now.timestamp() - days * 86400)
        except Exception:
            log.exception("broker: message purge of %s failed", full)
