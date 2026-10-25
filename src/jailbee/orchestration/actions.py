"""Broker actions: the context a manifest runs in, roles, and the permission gate."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jailbee.orchestration import labels, profiles
from jailbee.orchestration.grant import Grant, GrantError, effective_grant
from jailbee.orchestration.protocol import CHILD_TYPES, Manifest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime
    from pathlib import Path

    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

Role = Literal["orchestrator", "child", "none"]

_SHORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_NOT_ALLOWED = "orchestration is not allowed in this repo"


@dataclass(frozen=True)
class Node:
    """One container as the broker sees it (from raw `incus list`)."""

    full: str
    short: str
    running: bool
    config: Mapping[str, str]


@dataclass(frozen=True)
class Outcome:
    state: Literal["queued", "started", "done", "refused"]
    detail: str


def refused(detail: str) -> Outcome:
    return Outcome("refused", detail)


def done(detail: str) -> Outcome:
    return Outcome("done", detail)


@dataclass
class ActionContext:
    cfg: Config
    gcfg: GlobalConfig
    incus: Incus
    root: Path
    sender: Node
    nodes: Mapping[str, Node]
    now: datetime

    def role(self) -> Role:
        if self.sender.config.get(labels.SPAWNED_BY):
            return "child"
        if self.raw_grant() is not None:
            return "orchestrator"
        return "none"

    def raw_grant(self) -> Grant | None:
        raw = self.sender.config.get(labels.GRANT)
        if not raw:
            return None
        try:
            return Grant.from_label(raw)
        except GrantError:
            return None

    def grant(self) -> Grant | None:
        """The sender's grant clamped to the repo cap; None when none or disallowed."""
        raw = self.raw_grant()
        if raw is None:
            return None
        return effective_grant(raw, profiles.repo_cap(self.cfg))

    def children(self) -> list[Node]:
        return [
            n for n in self.nodes.values() if n.config.get(labels.SPAWNED_BY) == self.sender.full
        ]

    def child(self, short: str) -> Node | Outcome:
        if _SHORT_RE.match(short):
            for n in self.children():
                if n.short == short:
                    return n
        return refused(f"`{short}` is not a child of this orchestrator")


OPERATION_OF: Mapping[str, str] = {
    "spawn": "spawn",
    "message": "message",
    "import": "import",
    "restart": "restart",
    "stop": "stop",
    "destroy": "destroy",
}

# Plain module-level registry; later tasks and plan E add handlers.
HANDLERS: dict[str, Callable[[ActionContext, Manifest], Outcome]] = {}


def dispatch(ctx: ActionContext, manifest: Manifest) -> Outcome | None:
    """Gate `manifest` by the sender's role and grant, then run its handler.

    None means the sender has no role and the manifest is to be ignored.
    """
    role = ctx.role()
    if role == "none":
        return None
    if role == "child":
        if manifest.type not in CHILD_TYPES:
            return refused("children may only send messages")
    else:
        grant = ctx.grant()
        if grant is None:
            return refused(_NOT_ALLOWED)
        op = OPERATION_OF[manifest.type]
        if op not in grant.operations:
            return refused(f"operation `{op}` is not in this container's grant")
    handler = HANDLERS.get(manifest.type)
    if handler is None:
        return refused(f"`{manifest.type}` is not supported by this broker version")
    return handler(ctx, manifest)
