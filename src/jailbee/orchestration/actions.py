"""Broker actions: the context a manifest runs in, roles, and the permission gate."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from jailbee import forking, lifecycle
from jailbee.create_flow import CreateError, Interaction, NeedsOperator, start_create
from jailbee.forking import ForkError, ForkSource
from jailbee.lifecycle import NewContainerOptions
from jailbee.orchestration import labels, profiles
from jailbee.orchestration.grant import (
    ChildRights,
    Grant,
    GrantError,
    check_rights,
    effective_grant,
)
from jailbee.orchestration.protocol import CHILD_TYPES, Manifest, SpawnManifest
from jailbee.orchestration.state import QueuedRequest, load_queue, save_queue

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping
    from datetime import datetime
    from pathlib import Path

    from jailbee.branch_config import EscalationVerdict
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


GRANT_REVOKED = "orchestration grant revoked"


def live_create_jobs(prefix: str) -> set[str]:
    """Container names of this repo's create jobs whose worker is still alive."""
    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.db.models import JOB_CREATE

    with Session(get_engine()) as session:
        rows = background.list_all_jobs(session)
    return {
        name
        for name, job in rows.items()
        if job.op_kind == JOB_CREATE
        and job.container_prefix == prefix
        and background.worker_alive(job.pid)
    }


def running_children(ctx: ActionContext) -> int:
    """Children running plus children still being created, counted once each.

    A job row does not record its orchestrator, so a live create job counts
    when its container does not exist yet or is one of this sender's children.
    """
    mine = {n.full for n in ctx.children()}
    names = {n.full for n in ctx.children() if n.running}
    for name in live_create_jobs(ctx.cfg.container_prefix):
        if name in mine or name not in ctx.nodes:
            names.add(name)
    return len(names)


def _queued(ctx: ActionContext) -> list[QueuedRequest]:
    return load_queue(ctx.root).get(ctx.sender.full, [])


def _store(ctx: ActionContext, entries: list[QueuedRequest]) -> None:
    queues = load_queue(ctx.root)
    queues[ctx.sender.full] = entries
    save_queue(ctx.root, queues)


def handle_spawn(ctx: ActionContext, m: Manifest) -> Outcome:
    """Plan a spawn; start it when a slot is free, else queue it FIFO."""
    from jailbee import git

    if not isinstance(m, SpawnManifest):
        return refused("not a spawn manifest")
    grant = ctx.grant()
    if grant is None:
        return refused(_NOT_ALLOWED)
    queued = _queued(ctx)
    plan = plan_spawn(ctx, m, queued_names={q.child_full for q in queued})
    if isinstance(plan, Outcome):
        return plan
    fork: ForkSource | None = None
    if plan.kind == "fork":
        try:
            fork = forking.prepare_fork(ctx.cfg, ctx.incus, ctx.sender.short)
            forking.pin_fork_commit(ctx.cfg, plan.child_full, fork.commit)
        except (ForkError, git.GitError) as e:
            return refused(str(e))
    if running_children(ctx) < grant.max_running_children:
        return start_child(ctx, plan, fork=fork)
    queued.append(
        QueuedRequest(
            manifest_id=m.id,
            child_full=plan.child_full,
            kind=plan.kind,
            commit=fork.commit if fork is not None else None,
            queued_at=ctx.now.isoformat(),
            branch=plan.branch,
            base=plan.base,
            prompt=plan.prompt,
            rights=plan.rights.model_dump(mode="json"),
        )
    )
    _store(ctx, queued)
    return Outcome("queued", f"queued at position {len(queued)}")


def _start_spawn(ctx: ActionContext, req: QueuedRequest) -> Outcome:
    short = lifecycle.short_name(ctx.cfg, req.child_full)
    if req.child_full in ctx.nodes or ctx.incus.exists(req.child_full):
        return refused(f"container `{short}` already exists")
    plan = SpawnPlan(
        child_full=req.child_full,
        kind="fork" if req.kind == "fork" else "new",
        branch=req.branch,
        base=req.base,
        prompt=req.prompt,
        rights=ChildRights.model_validate(req.rights),
    )
    fork: ForkSource | None = None
    if req.kind == "fork":
        if req.commit is None:
            return refused("queued fork has no pinned commit")
        fork = ForkSource(
            full_name=ctx.sender.full,
            branch=req.branch,
            commit=req.commit,
            base_branch=ctx.incus.config_get(ctx.sender.full, "user.jailbee.base_branch") or None,
            untrusted=bool(ctx.incus.config_get(ctx.sender.full, "user.jailbee.pr")),
        )
    return start_child(ctx, plan, fork=fork)


# Queue kind -> starter. Task 10 registers "restart" here.
STARTERS: dict[str, Callable[[ActionContext, QueuedRequest], Outcome]] = {
    "fork": _start_spawn,
    "new": _start_spawn,
}


def drain_queue(ctx: ActionContext) -> list[tuple[QueuedRequest, Outcome]]:
    """Start queued requests FIFO while slots are free; a lowered limit starts nothing."""
    grant = ctx.grant()
    queued = _queued(ctx)
    if grant is None or not queued:
        return []
    free = grant.max_running_children - running_children(ctx)
    results: list[tuple[QueuedRequest, Outcome]] = []
    while queued and free > 0:
        req = queued.pop(0)
        starter = STARTERS.get(req.kind)
        outcome = (
            starter(ctx, req) if starter else refused(f"unknown queued request kind `{req.kind}`")
        )
        if outcome.state == "started":
            free -= 1
        results.append((req, outcome))
    if results:
        _store(ctx, queued)
    return results


def drop_queue(ctx: ActionContext, reason: str) -> list[QueuedRequest]:
    """Remove and return the sender's queued requests; the caller reports `reason` per entry."""
    queued = _queued(ctx)
    if queued:
        _store(ctx, [])
    return queued


HANDLERS["spawn"] = handle_spawn
