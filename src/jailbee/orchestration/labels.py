"""Incus instance config keys and values used by orchestration."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jailbee.config import Config
    from jailbee.incus import Incus

AGENT_SESSIONS = "user.jailbee.orchestration.agent_sessions"
RESUME = "user.jailbee.orchestration.resume"


def valid_session_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value)
        is not None
    )


def sessions_of(config: Mapping[str, str]) -> list[str]:
    try:
        value = json.loads(config.get(AGENT_SESSIONS, "[]"))
    except (ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and valid_session_id(item)]


def with_session(history: list[str], session_id: str, cap: int = 10) -> list[str] | None:
    if history and history[-1] == session_id:
        return None
    return (
        ([item for item in history if item != session_id] + [session_id])[-cap:] if cap > 0 else []
    )


GRANT = "user.jailbee.orchestration.grant"
RIGHTS = "user.jailbee.orchestration.rights"
TIMED = "user.jailbee.orchestration.timed"
RUNNER = "user.jailbee.orchestration.runner"
SPAWNED_BY = "user.jailbee.spawned_by"
ROLE_MARKER_PATH = "/home/dev/.jailbee/orchestration/role"


def write_child_role(incus: Incus, full: str, *, uid: int) -> None:
    """Install the marker atomically as the container user."""
    incus.exec(
        full,
        ["sh", "-c", 'set -eu; mkdir -p "${1%/*}"; tmp=$(mktemp "${1}.XXXXXX"); '
         'trap \'rm -f "$tmp"\' EXIT; printf "child\\n" > "$tmp"; mv -f "$tmp" "$1"',
         "sh", ROLE_MARKER_PATH],
        uid=uid,
    )
STATE = "user.jailbee.orchestration.state"
# LAST_IMPORT, DONE_SHA and STOPPED_HEAD hold a commit as the full 40-hex
# OID, in exactly the form `sync._container_head_oid` returns (plan E
# writes LAST_IMPORT / DONE_SHA in that form too), so the destroy guard can
# compare them as plain strings.
DONE_SHA = "user.jailbee.orchestration.done_sha"
LAST_IMPORT = "user.jailbee.orchestration.last_import"
# The child's HEAD as the broker's `stop` recorded it, while still running.
# The destroy guard of a stopped child trusts this label alone (DONE_SHA is
# no fallback, spec 7.3 as amended); the broker unsets it whenever it sees
# the child running, so a start outside the broker leaves no stale value.
STOPPED_HEAD = "user.jailbee.orchestration.stopped_head"

ON_DONE = "user.jailbee.orchestration.on_done"
PENDING_DESTROY = "user.jailbee.orchestration.pending_destroy"
STOPPED_DIRTY = "user.jailbee.orchestration.stopped_dirty"
ON_DONE_DESTROY = "destroy"
ON_DONE_KEEP = "keep"


def on_done_of(config: Mapping[str, str]) -> Literal["destroy", "keep"]:
    """Old or invalid policies keep the child rather than risk losing work."""
    return "destroy" if config.get(ON_DONE) == ON_DONE_DESTROY else "keep"


STATE_WORKING = "working"
STATE_DONE = "done"
STATE_IMPORTED = "imported"


def is_finished(config: Mapping[str, str]) -> bool:
    """True once the child reported done, or its done commit was imported."""
    state = config.get(STATE)
    if state == STATE_DONE:
        return True
    done_sha = config.get(DONE_SHA)
    return state == STATE_IMPORTED and bool(done_sha) and done_sha == config.get(LAST_IMPORT)


def child_env(cfg: Config, incus: Incus, full: str) -> dict[str, str]:
    """Role env for a spawned child's agent steps; ``{}`` for any other container."""
    from jailbee.lifecycle import short_name

    orchestrator = incus.config_get(full, SPAWNED_BY)
    if not orchestrator:
        return {}
    return {"JAILBEE_ROLE": "child", "JAILBEE_ORCHESTRATOR": short_name(cfg, orchestrator)}


def orchestrator_env(cfg: Config, incus: Incus, full: str) -> dict[str, str]:
    """Default-agent env for an orchestrator's agent steps; ``{}`` without a grant.

    The value is the repo's primary agent, as `claude-jb` when Claude runs
    through the LiteLLM proxy, or ``none`` when no agent is enabled. Children
    never hold grants, so this and `child_env` never both apply.
    """
    if not incus.config_get(full, GRANT):
        return {}
    from jailbee.agents import primary_agent
    from jailbee.autostart import litellm_autostart_on

    name = primary_agent(cfg).name or "none"
    if name == "claude" and litellm_autostart_on(cfg):
        name = "claude-jb"
    return {"JAILBEE_DEFAULT_AGENT": name}
