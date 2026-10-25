"""Incus instance config keys and values used by orchestration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

GRANT = "user.jailbee.orchestration.grant"
RIGHTS = "user.jailbee.orchestration.rights"
SPAWNED_BY = "user.jailbee.spawned_by"
STATE = "user.jailbee.orchestration.state"
DONE_SHA = "user.jailbee.orchestration.done_sha"
LAST_IMPORT = "user.jailbee.orchestration.last_import"

STATE_WORKING = "working"
STATE_DONE = "done"
STATE_IMPORTED = "imported"


def child_env(cfg: Config, incus: Incus, full: str) -> dict[str, str]:
    """Role env for a spawned child's agent steps; ``{}`` for any other container."""
    from jailbee.lifecycle import short_name

    orchestrator = incus.config_get(full, SPAWNED_BY)
    if not orchestrator:
        return {}
    return {"JAILBEE_ROLE": "child", "JAILBEE_ORCHESTRATOR": short_name(cfg, orchestrator)}
