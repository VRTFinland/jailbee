"""Broker actions: the context a manifest runs in, roles, and the permission gate."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from jailbee import lifecycle
from jailbee.create_flow import CreateError, Interaction, NeedsOperator, start_create
from jailbee.forking import ForkError
from jailbee.lifecycle import NewContainerOptions
from jailbee.orchestration import labels, profiles
from jailbee.orchestration.grant import (
    Grant,
    GrantError,
    check_rights,
    effective_grant,
)
from jailbee.orchestration.protocol import CHILD_TYPES, Manifest, SpawnManifest

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping
    from datetime import datetime
    from pathlib import Path

    from jailbee.branch_config import EscalationVerdict
    from jailbee.config import Config
    from jailbee.forking import ForkSource
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus
    from jailbee.orchestration.grant import ChildRights

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


def child_preamble(orchestrator_short: str) -> str:
    return (
        f"You are a child container of orchestrator `{orchestrator_short}`. "
        "Use the `jailbee-orchestration-child` skill."
    )


@dataclass(frozen=True)
class SpawnPlan:
    child_full: str
    kind: Literal["fork", "new"]
    branch: str
    base: str | None
    prompt: str
    rights: ChildRights


def plan_spawn(
    ctx: ActionContext, m: SpawnManifest, *, queued_names: Collection[str]
) -> SpawnPlan | Outcome:
    """Validate a spawn request; `queued_names` are full names of queued spawns."""
    grant = ctx.raw_grant()
    if grant is None:
        return refused(_NOT_ALLOWED)
    problems = check_rights(
        grant, profiles.repo_cap(ctx.cfg), m.rights, ctx.cfg.optional_mounts.keys()
    )
    if problems:
        return refused("; ".join(problems))
    try:
        full = lifecycle.derive_container_name(ctx.cfg, m.branch)
    except ValueError as exc:
        return refused(str(exc))
    short = lifecycle.short_name(ctx.cfg, full)
    if full in ctx.nodes or ctx.incus.exists(full):
        return refused(f"container `{short}` already exists")
    if full in queued_names:
        return refused(f"a request for `{short}` is already queued")
    prompt = f"{child_preamble(ctx.sender.short)}\n\n{m.prompt}"
    return SpawnPlan(full, m.kind, m.branch, m.base, prompt, m.rights)


def escalation_policy(rights: ChildRights) -> Callable[[EscalationVerdict], bool]:
    """Accept an autostart escalation only when the child's rights already cover it."""
    granted = set(rights.mounts)

    def accept(verdict: EscalationVerdict) -> bool:
        if not set(verdict.attached_mounts) <= granted:
            return False
        return not verdict.widening_steps or rights.loose

    return accept


def child_options(
    cfg: Config, plan: SpawnPlan, *, orchestrator_full: str, fork: ForkSource | None
) -> NewContainerOptions:
    """The create options for a child: always an untrusted head, rights exactly as planned."""
    return NewContainerOptions(
        container_branch=plan.branch,
        name=plan.child_full,
        network="loose" if plan.rights.loose else cfg.defaults.network,
        memory=None,
        cpu=cfg.defaults.cpu,
        from_base=cfg.golden.alias,
        clone=True,
        base=None if fork is not None else plan.base,
        clone_commit=fork.commit if fork is not None else None,
        fork_of=fork.full_name if fork is not None else None,
        base_branch_label=fork.base_branch if fork is not None else None,
        untrusted_head=True,
        initial_prompt=plan.prompt,
        spawned_by=orchestrator_full,
        rights=plan.rights,
        assume_yes=False,
    )


def start_child(ctx: ActionContext, plan: SpawnPlan, *, fork: ForkSource | None) -> Outcome:
    """Start the background create of a planned child; refusals become outcomes."""
    opts = child_options(ctx.cfg, plan, orchestrator_full=ctx.sender.full, fork=fork)
    interaction = replace(Interaction.none(), accept_escalation=escalation_policy(plan.rights))
    try:
        # config_path=None: the worker reloads the repo config from `cfg.repo_root`.
        start_create(ctx.cfg, ctx.incus, ctx.gcfg, opts, interaction=interaction, config_path=None)
    except NeedsOperator as e:
        return refused(e.reason)
    except CreateError as e:
        return refused(e.message)
    except (ForkError, ValueError) as e:
        return refused(str(e))
    return Outcome("started", f"child `{lifecycle.short_name(ctx.cfg, plan.child_full)}` created")
