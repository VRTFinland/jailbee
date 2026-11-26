"""Resolve and deliver web chat through the shared agent input boundary."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from jailbee import agent_input
from jailbee.agent_input import Outcome
from jailbee.config import load_repo_config
from jailbee.incus import Incus
from jailbee.web.protocol import ChatAvailability

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.web.context import WebContext


def target(ctx: WebContext, prefix: str, agent: str, session_id: str) -> tuple[Config, Incus, str] | Outcome:
    if not ctx.deps.agent_chat:
        return Outcome("permission_denied", "Agent chat is disabled")
    if agent != "claude":
        return Outcome("unsupported_agent", "Unsupported agent")
    snapshot = ctx.hub.latest()
    if snapshot is None:
        return Outcome("no_live_session", "No live session")
    group = next((g for g in snapshot.groups if g.prefix == prefix), None)
    if group is None or group.repo_root is None:
        return Outcome("no_live_session", "No live session")
    owners = [c.name for c in group.containers if any(
        summary.agent == agent and session_id in summary.session_ids
        for summary in c.agent_status
    )]
    if len(owners) != 1:
        return Outcome("no_live_session", "No live session")
    owner = owners[0]
    cfg = load_repo_config(Path(group.repo_root))
    return cfg, (ctx.deps.incus_factory or Incus)(), owner


def availability(ctx: WebContext, prefix: str, agent: str, session_id: str) -> ChatAvailability:
    result = target(ctx, prefix, agent, session_id)
    if not isinstance(result, Outcome):
        cfg, incus, owner = result
        result = agent_input.availability(cfg, incus, owner, session_id)
    return ChatAvailability(available=result.code == "available", code=result.code, message=result.message)


def send(ctx: WebContext, prefix: str, agent: str, session_id: str, text: str) -> Outcome:
    result = target(ctx, prefix, agent, session_id)
    if isinstance(result, Outcome):
        return result
    cfg, incus, owner = result
    return agent_input.send(cfg, incus, owner, session_id, text)
