"""TTL-driven auto-unmount for `jailbee mount` optional mounts.

Called once per repo per ``jailbee-net-refresh.timer`` tick from
``egress_pool.refresh_all``, right after `loose_revert`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from jailbee.loose_revert import _autostart_holds
from jailbee.mounts import DEVICE_NAME_PREFIX, MOUNT_UNTIL_PREFIX

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MountRevertResult:
    """One label acted on; ``removed`` is False for an orphan or failed removal."""

    container: str
    kind: str
    removed: bool
    error: str | None = None


def check_and_revert_mounts(cfg: Config, incus: Incus, *, now: datetime) -> list[MountRevertResult]:
    """Detach expired optional mounts, even for kinds since deleted from config.

    Written deadlines are honoured independently of the current auto-revert
    policy. Containers still owned by an autostart run are skipped. Labels and
    devices come from the listing, avoiding per-container reads without labels.
    """
    prefix = cfg.container_prefix
    out: list[MountRevertResult] = []
    for raw in incus.list_containers():
        name = raw["name"]
        profiles = raw.get("profiles") or []
        if f"{prefix}-base" not in profiles:
            continue
        config = raw.get("config") or {}
        labels = {k: v for k, v in config.items() if k.startswith(MOUNT_UNTIL_PREFIX)}
        if not labels:
            continue
        try:
            if _autostart_holds(incus, name):
                continue
            devices = raw.get("devices") or {}
            for key, value in sorted(labels.items()):
                kind = key.removeprefix(MOUNT_UNTIL_PREFIX)
                out.extend(_check_one(incus, name, kind, key, value, devices, now))
        except Exception as e:  # never let one container break the loop
            log.warning("mount_revert: %s raised: %s", name, e)
            out.append(MountRevertResult(container=name, kind="", removed=False, error=str(e)))
    return out


def _check_one(
    incus: Incus,
    name: str,
    kind: str,
    key: str,
    value: object,
    devices: dict[str, object],
    now: datetime,
) -> list[MountRevertResult]:
    try:
        until = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        until = None
    # Naive labels cannot be compared with the timer's aware UTC clock.
    if until is None or until.utcoffset() is None:
        log.warning("mount_revert: %s - malformed %s %r, clearing", name, key, value)
        incus.config_unset(name, key)
        return []
    device = f"{DEVICE_NAME_PREFIX}{kind}"
    if device not in devices:
        incus.config_unset(name, key)
        return [MountRevertResult(container=name, kind=kind, removed=False)]
    if until > now:
        return []
    try:
        incus.config_device_remove(name, device)
    except Exception as e:  # log + retry next cycle
        log.warning("mount_revert: failed to detach %s from %s: %s", kind, name, e)
        return [MountRevertResult(container=name, kind=kind, removed=False, error=str(e))]
    incus.config_unset(name, key)
    log.info("mount_revert: %s - detached %s (TTL expired)", name, kind)
    return [MountRevertResult(container=name, kind=kind, removed=True)]
