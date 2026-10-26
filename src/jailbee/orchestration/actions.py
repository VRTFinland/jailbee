"""Broker actions: the context a manifest runs in, roles, and the permission gate."""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from jailbee import forking, lifecycle, stopping, sync
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
from jailbee.orchestration.protocol import (
    CHILD_TYPES,
    DestroyManifest,
    Manifest,
    RestartManifest,
    SpawnManifest,
    StopManifest,
    restart_sets_runner,
)
from jailbee.orchestration.runner import Runner, parse_runner_label, runner_problems
from jailbee.orchestration.state import QueuedRequest, load_queue, purge_pending, save_queue

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator, Mapping
    from datetime import datetime
    from pathlib import Path

    from jailbee.branch_config import EscalationVerdict
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

Role = Literal["orchestrator", "child", "none"]

_SHORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
NOT_ALLOWED = "orchestration is not allowed in this repo"

#: Bounds for the git work a child's `done` and an orchestrator's `import` run
#: against containers the broker does not trust (NOPASSWD sudo inside): one
#: stalled `incus exec` or `git upload-pack` must not stall the single broker
#: loop. A probe exec (repo dir, status, branch) answers in well under a
#: minute on any sane tree — the same bound `sync`'s status probe uses.
CHILD_EXEC_TIMEOUT_S = 60
#: Each git transport (fetch from / push into a container, per submodule).
#: Generous: a first import of a large branch moves real data.
CHILD_TRANSPORT_TIMEOUT_S = 600


@dataclass(frozen=True)
class Node:
    """One container as the broker sees it (from raw `incus list`)."""

    full: str
    short: str
    running: bool
    config: Mapping[str, str]

    @property
    def key(self) -> str:
        """Identity of this Incus instance: `volatile.uuid`, else the name.

        A container destroyed and recreated under the same name gets a new
        uuid, so per-instance broker state (the journal) never carries over.
        """
        return self.config.get("volatile.uuid") or self.full


@dataclass(frozen=True)
class Outcome:
    state: Literal["queued", "started", "done", "refused", "failed"]
    detail: str


def refused(detail: str) -> Outcome:
    return Outcome("refused", detail)


def done(detail: str) -> Outcome:
    return Outcome("done", detail)


def failed(detail: str) -> Outcome:
    return Outcome("failed", detail)


@dataclass
class ActionContext:
    cfg: Config
    gcfg: GlobalConfig
    incus: Incus
    root: Path
    sender: Node
    nodes: Mapping[str, Node]
    now: datetime
    # Shared with the broker's memory so handlers keep one emission order.
    seq: Iterator[int] = field(default_factory=itertools.count)

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
    "update": "import",
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
            return refused(NOT_ALLOWED)
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
    runner: Runner | None = None
    on_done: Literal["destroy", "keep"] = "destroy"


def plan_spawn(
    ctx: ActionContext, m: SpawnManifest, *, queued_names: Collection[str]
) -> SpawnPlan | Outcome:
    """Validate a spawn request; `queued_names` are full names of queued spawns."""
    grant = ctx.raw_grant()
    if grant is None:
        return refused(NOT_ALLOWED)
    problems = check_rights(
        grant, profiles.repo_cap(ctx.cfg), m.rights, ctx.cfg.optional_mounts.keys()
    )
    if m.runner is not None:
        problems += runner_problems(ctx.cfg, ctx.incus, ctx.gcfg, m.runner)
    if problems:
        return refused("; ".join(problems))
    try:
        full = lifecycle.derive_container_name(ctx.cfg, m.branch)
    except ValueError as exc:
        return refused(str(exc))
    short = lifecycle.short_name(ctx.cfg, full)
    if full in ctx.nodes or ctx.incus.exists(full):
        return refused(f"container `{short}` already exists")
    if full in live_create_jobs(ctx.cfg.container_prefix):
        return refused(f"container `{short}` is already being created")
    if full in queued_names:
        return refused(f"a request for `{short}` is already queued")
    effective = ctx.grant()
    if effective is None:
        return refused(NOT_ALLOWED)
    if m.on_done == "destroy" and "destroy" not in effective.operations:
        return refused("on_done destroy needs the `destroy` operation in this container's grant")
    policy = m.on_done or ("destroy" if "destroy" in effective.operations else "keep")
    prompt = f"{child_preamble(ctx.sender.short)}\n\n{m.prompt}"
    return SpawnPlan(full, m.kind, m.branch, m.base, prompt, m.rights, m.runner, policy)


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
        # Never the repo default: a loose default is not one of the child's rights.
        network="loose" if plan.rights.loose else "strict",
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
        runner=plan.runner,
        on_done=plan.on_done,
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
    # Only the worker is launched here; a later create failure is not reported
    # (plan F), so the status must not claim the child exists.
    short = lifecycle.short_name(ctx.cfg, plan.child_full)
    return Outcome("started", f"creation of child `{short}` started; on done: {plan.on_done}")


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
        and job.pid > 0  # os.kill(0, 0) would probe our own process group
        and not background.clearable(job.phase, job.pid)
    }


def live_job_kind(name: str) -> str | None:
    """The `op_kind` of this container's job row while its worker is still alive.

    None when there is no row, or the row is clearable (terminal phase or dead
    worker). Unlike `live_create_jobs` this is not filtered by kind or prefix.
    """
    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as session:
        job = background.get_job(session, name)
    if job is None or job.pid <= 0:  # os.kill(0, 0) would probe our own process group
        return None
    if background.clearable(job.phase, job.pid):
        return None
    return job.op_kind


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
        return refused(NOT_ALLOWED)
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
    # A non-empty queue goes first: a new spawn never overtakes it.
    if not queued and running_children(ctx) < grant.max_running_children:
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
            runner=plan.runner.model_dump(mode="json") if plan.runner else None,
            runner_set=plan.runner is not None,
            on_done=plan.on_done,
        )
    )
    _store(ctx, queued)
    return Outcome("queued", f"queued at position {len(queued)}")


def _child_head(ctx: ActionContext, child: Node) -> str | None:
    """A running child's HEAD as the full OID, or None when it cannot be read."""
    try:
        return sync._container_head_oid(
            ctx.incus,
            child.full,
            lifecycle.container_repo_dir(ctx.cfg, ctx.incus, child.full),
            uid=ctx.cfg.container_user.uid,
        )
    except Exception:
        return None


def _boot(
    ctx: ActionContext,
    child: Node,
    prompt: str,
    *,
    started: bool,
    runner: Runner | None = None,
    set_runner: bool = False,
    on_done: str | None = None,
) -> Outcome:
    # Before the boot: a restarted child commits anew, so a HEAD recorded at
    # its last stop (or its last `done`) no longer describes it, and it is
    # `working` again (absent STATE = working). Clearing first means a running
    # child never carries a stale value.
    try:
        for key in (labels.STOPPED_HEAD, labels.DONE_SHA, labels.STATE,
                    labels.PENDING_DESTROY, labels.STOPPED_DIRTY):
            ctx.incus.config_unset_checked(child.full, key)
        if set_runner:
            if runner is not None:
                ctx.incus.config_set(child.full, labels.RUNNER, runner.to_label())
            else:
                ctx.incus.config_unset_checked(child.full, labels.RUNNER)
        if on_done is not None:
            ctx.incus.config_set(child.full, labels.ON_DONE, on_done)
    except Exception as e:
        return refused(f"child `{child.short}` could not be restarted: {e}")
    try:
        lifecycle.boot_container(
            ctx.cfg, ctx.incus, child.full, restart=False, initial_prompt=prompt
        )
    except Exception as e:
        return refused(f"child `{child.short}` could not be restarted: {e}")
    detail = f"child `{child.short}` restarted"
    return Outcome("started", detail) if started else done(detail)


def _still_granted(ctx: ActionContext, op: str) -> Outcome | None:
    """Refusal when `op` left the sender's effective grant while the request waited."""
    grant = ctx.grant()
    if grant is None:
        return refused(NOT_ALLOWED)
    if op not in grant.operations:
        return refused(f"operation `{op}` is not in this container's grant")
    return None


def _start_spawn(ctx: ActionContext, req: QueuedRequest) -> Outcome:
    short = lifecycle.short_name(ctx.cfg, req.child_full)
    revoked = _still_granted(ctx, "spawn")
    if revoked is not None:
        return revoked
    rights = ChildRights.model_validate(req.rights)
    # Spec 5.2: the grant and the repo cap apply at every broker check, so
    # rights accepted at queue time are checked again against today's.
    raw = ctx.raw_grant()
    if raw is None:
        return refused(NOT_ALLOWED)
    problems = check_rights(raw, profiles.repo_cap(ctx.cfg), rights, ctx.cfg.optional_mounts.keys())
    runner = Runner.model_validate(req.runner) if req.runner is not None else None
    if runner is not None:
        problems += runner_problems(ctx.cfg, ctx.incus, ctx.gcfg, runner)
    if problems:
        return refused("; ".join(problems))
    if req.child_full in ctx.nodes or ctx.incus.exists(req.child_full):
        return refused(f"container `{short}` already exists")
    if req.child_full in live_create_jobs(ctx.cfg.container_prefix):
        return refused(f"container `{short}` is already being created")
    grant = ctx.grant()
    plan = SpawnPlan(
        child_full=req.child_full,
        kind="fork" if req.kind == "fork" else "new",
        branch=req.branch,
        base=req.base,
        prompt=req.prompt,
        rights=rights,
        runner=runner,
        on_done=("destroy" if req.on_done == "destroy" and grant is not None
                 and "destroy" in grant.operations else "keep"),
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


def _start_restart(ctx: ActionContext, req: QueuedRequest) -> Outcome:
    short = lifecycle.short_name(ctx.cfg, req.child_full)
    revoked = _still_granted(ctx, "restart")
    if revoked is not None:
        return revoked
    node = ctx.nodes.get(req.child_full)
    if node is None or node.config.get(labels.SPAWNED_BY) != ctx.sender.full:
        return refused(f"`{short}` is not a child of this orchestrator")
    if node.running:
        return refused(f"child `{short}` is running; stop it first")
    if node.full in live_create_jobs(ctx.cfg.container_prefix):
        return refused(f"child `{short}` is still being created")
    runner = Runner.model_validate(req.runner) if req.runner is not None else None
    effective = runner if req.runner_set else parse_runner_label(node.config.get(labels.RUNNER))
    if effective is not None:
        problems = runner_problems(ctx.cfg, ctx.incus, ctx.gcfg, effective)
        if problems:
            return refused("; ".join(problems))
    grant = ctx.grant()
    policy = req.on_done
    if policy == "destroy" and (grant is None or "destroy" not in grant.operations):
        policy = "keep"
    return _boot(ctx, node, req.prompt, started=True, runner=runner,
                 set_runner=req.runner_set, on_done=policy)


# Queue kind -> starter.
STARTERS: dict[str, Callable[[ActionContext, QueuedRequest], Outcome]] = {
    "fork": _start_spawn,
    "new": _start_spawn,
    "restart": _start_restart,
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
        # At most once: the pop is on disk before anything starts, so a crash
        # inside the starter never starts the same request again.
        _store(ctx, queued)
        starter = STARTERS.get(req.kind)
        outcome = (
            starter(ctx, req) if starter else refused(f"unknown queued request kind `{req.kind}`")
        )
        if outcome.state == "started":
            free -= 1
        results.append((req, outcome))
    return results


def drop_queue(ctx: ActionContext, reason: str) -> list[QueuedRequest]:
    """Remove and return the sender's queued requests; the caller reports `reason` per entry."""
    queued = _queued(ctx)
    if queued:
        _store(ctx, [])
    return queued


HANDLERS["spawn"] = handle_spawn


def handle_stop(ctx: ActionContext, m: Manifest) -> Outcome:
    if not isinstance(m, StopManifest):
        return refused("not a stop manifest")
    child = ctx.child(m.child)
    if isinstance(child, Outcome):
        return child
    if child.running:
        # Record HEAD while it can still be read: the destroy guard of a
        # stopped child compares it with LAST_IMPORT (spec 7.3).
        head = _child_head(ctx, child)
        try:
            if head is not None:
                ctx.incus.config_set(child.full, labels.STOPPED_HEAD, head)
            else:
                ctx.incus.config_unset_checked(child.full, labels.STOPPED_HEAD)
            bounded = ctx.incus.with_exec_timeout(CHILD_EXEC_TIMEOUT_S)
            dirty = sync._container_dirty_quiet(
                bounded, child.full, lifecycle.container_repo_dir(ctx.cfg, bounded, child.full),
                uid=ctx.cfg.container_user.uid,
            )
            if dirty is False:
                ctx.incus.config_unset_checked(child.full, labels.STOPPED_DIRTY)
            else:
                ctx.incus.config_set(child.full, labels.STOPPED_DIRTY, "1")
        except Exception as e:
            return refused(f"child `{child.short}` could not be stopped: {e}")
        try:
            stopping.stop_container(ctx.incus, child.full, show_progress=False)
        except Exception as e:
            return refused(f"child `{child.short}` could not be stopped: {e}")
    return done(f"child `{child.short}` stopped")


def handle_restart(ctx: ActionContext, m: Manifest) -> Outcome:
    """Restart a stopped child with a fresh prompt; queue FIFO when no slot is free."""
    if not isinstance(m, RestartManifest):
        return refused("not a restart manifest")
    grant = ctx.grant()
    if grant is None:
        return refused(NOT_ALLOWED)
    if m.on_done == "destroy" and "destroy" not in grant.operations:
        return refused("on_done destroy needs the `destroy` operation in this container's grant")
    child = ctx.child(m.child)
    if isinstance(child, Outcome):
        return child
    if child.running:
        return refused(f"child `{child.short}` is running; stop it first")
    if child.full in live_create_jobs(ctx.cfg.container_prefix):
        return refused(f"child `{child.short}` is still being created")
    set_runner = restart_sets_runner(m)
    # A restart that names no runner keeps the child's label; that kept runner
    # must still be launchable, so it is checked like a named one.
    effective = m.runner if set_runner else parse_runner_label(child.config.get(labels.RUNNER))
    if effective is not None:
        problems = runner_problems(ctx.cfg, ctx.incus, ctx.gcfg, effective)
        if problems:
            return refused("; ".join(problems))
    prompt = f"{child_preamble(ctx.sender.short)}\n\n{m.prompt}"
    queued = _queued(ctx)
    if any(q.child_full == child.full for q in queued):
        return refused(f"a request for `{child.short}` is already queued")
    if not queued and running_children(ctx) < grant.max_running_children:
        return _boot(ctx, child, prompt, started=False, runner=m.runner,
                     set_runner=set_runner, on_done=m.on_done)
    queued.append(
        QueuedRequest(
            manifest_id=m.id,
            child_full=child.full,
            kind="restart",
            commit=None,
            queued_at=ctx.now.isoformat(),
            branch="",
            base=None,
            prompt=prompt,
            rights={},
            runner=m.runner.model_dump(mode="json") if m.runner else None,
            runner_set=set_runner,
            on_done=m.on_done,
        )
    )
    _store(ctx, queued)
    return Outcome("queued", f"queued at position {len(queued)}")


def destroy_guard(ctx: ActionContext, child: Node) -> str | None:
    """Reason a child must not be destroyed (unimported commits), or None."""
    reason = (
        f"child `{child.short}` has commits that were never imported "
        "(use force: true to destroy anyway)"
    )
    last = child.config.get(labels.LAST_IMPORT)
    if not last:
        return reason
    head: str | None
    if child.running:
        head = _child_head(ctx, child)
    else:
        # HEAD cannot be read from a stopped child, so only the one the
        # broker's `stop` recorded counts (spec 7.3 as amended). No DONE_SHA
        # fallback: a child stopped outside the broker (reboot, `jb stop`,
        # OOM) may have committed after its `done`.
        head = child.config.get(labels.STOPPED_HEAD) or None
        if head is None:
            return (
                f"child `{child.short}` is stopped and its HEAD was not recorded by a "
                "broker stop; restart it so its HEAD can be checked "
                "(or use force: true to destroy anyway)"
            )
    if head is None or head != last:
        return reason
    if child.running:
        try:
            bounded = ctx.incus.with_exec_timeout(CHILD_EXEC_TIMEOUT_S)
            dirty = sync._container_status_dirty(
                bounded, child.full, lifecycle.container_repo_dir(ctx.cfg, bounded, child.full),
                uid=ctx.cfg.container_user.uid,
            )
        except Exception:
            return (f"child `{child.short}`: its working tree could not be checked "
                    "(use force: true to destroy anyway)")
    else:
        dirty = child.config.get(labels.STOPPED_DIRTY) == "1"
    if dirty:
        return (f"child `{child.short}` has uncommitted changes "
                "(use force: true to destroy anyway)")
    return None


def handle_destroy(ctx: ActionContext, m: Manifest) -> Outcome:
    # No drain here: the broker drains after every round and reports each outcome.
    if not isinstance(m, DestroyManifest):
        return refused("not a destroy manifest")
    child = ctx.child(m.child)
    if isinstance(child, Outcome):
        return child
    if not m.force:
        reason = destroy_guard(ctx, child)
        if reason is not None:
            return refused(reason)
    try:
        lifecycle.destroy_container(ctx.cfg, ctx.incus, child.full, force=True)
    except Exception as e:
        return refused(f"child `{child.short}` could not be destroyed: {e}")
    # Parked messages were for this instance; a recreated namesake must not get them.
    purge_pending(ctx.root, child.full)
    return done(f"child `{child.short}` destroyed")


HANDLERS["stop"] = handle_stop
HANDLERS["restart"] = handle_restart
HANDLERS["destroy"] = handle_destroy


def _handle_message(ctx: ActionContext, m: Manifest) -> Outcome:
    # Late import: `messages` imports this module, whichever of the two loads first.
    from jailbee.orchestration import messages

    return messages.handle_message(ctx, m)


HANDLERS["message"] = _handle_message


def _handle_import(ctx: ActionContext, m: Manifest) -> Outcome:
    # Late import, as for `message`: `importing` imports this module.
    from jailbee.orchestration import importing

    return importing.handle_import(ctx, m)


HANDLERS["import"] = _handle_import


def _handle_update(ctx: ActionContext, m: Manifest) -> Outcome:
    from jailbee.orchestration import importing

    return importing.handle_update(ctx, m)


HANDLERS["update"] = _handle_update
