"""Broker actions: the context a manifest runs in, roles, and the permission gate."""

from __future__ import annotations

import itertools
import logging
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
    CancelManifest,
    DestroyManifest,
    Manifest,
    MessageManifest,
    RestartManifest,
    RightsManifest,
    SpawnManifest,
    StopManifest,
    UpdateManifest,
    restart_sets_runner,
)
from jailbee.orchestration.runner import Runner, parse_runner_label, runner_problems
from jailbee.orchestration.state import (
    QueuedRequest,
    Reservation,
    load_queue,
    load_reservations,
    purge_pending,
    reservation_alive,
    save_queue,
    save_reservations,
)
from jailbee.orchestration.store import PendingImportError

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator, Mapping
    from datetime import datetime
    from pathlib import Path

    from jailbee.branch_config import EscalationVerdict
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus
    from jailbee.orchestration.runner_model import DefaultRunnerSource

#: ``plain``: a container with no orchestration label. It keeps a broker
#: socket (outbox ops only); ``none``: a grant label that does not parse.
Role = Literal["orchestrator", "child", "plain", "none"]

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
    started_at: float | None = None
    init_pid: int | None = None
    cpu_usage_ns: int | None = None
    memory_usage: int | None = None

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
    results: list[dict[str, object]] = field(default_factory=list)


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
        if self.sender.config.get(labels.GRANT):
            return "orchestrator" if self.raw_grant() is not None else "none"
        return "plain"

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

    def default_runner(self) -> tuple[Runner | None, DefaultRunnerSource | None]:
        return profiles.default_runner(self.cfg, self.raw_grant())

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
    "cancel": "spawn",
    "message": "message",
    "import": "import",
    "update": "import",
    "restart": "restart",
    "refresh": "refresh",
    "stop": "stop",
    "destroy": "destroy",
    "rights": "rights",
}

# Plain module-level registry; later tasks and plan E add handlers.
HANDLERS: dict[str, Callable[[ActionContext, Manifest], Outcome]] = {}


def dispatch(ctx: ActionContext, manifest: Manifest) -> Outcome | None:
    """Gate `manifest` by the sender's role and grant, then run its handler.

    None means the sender has no role and the manifest is to be ignored.
    """
    role = ctx.role()
    if role in ("none", "plain"):
        return refused(NOT_ALLOWED) if manifest.type == "publish" else None
    if role == "child":
        if isinstance(manifest, RightsManifest):
            if "grant" in manifest.model_fields_set or manifest.child not in (
                None,
                ctx.sender.short,
            ):
                return refused("children may only revoke their own rights")
        elif manifest.type not in CHILD_TYPES:
            return refused("children may only send messages")
    else:
        if isinstance(manifest, RightsManifest):
            raw = ctx.incus.config_get(ctx.sender.full, labels.GRANT)
            if not raw:
                return None
            try:
                grant = effective_grant(Grant.from_label(raw), profiles.repo_cap(ctx.cfg))
            except GrantError:
                return None
        else:
            grant = ctx.grant()
        if grant is None:
            return refused(NOT_ALLOWED)
        if manifest.type == "publish":
            if grant.publish.empty():
                return refused("publishing is not in this container's grant")
        else:
            op = OPERATION_OF[manifest.type]
            revoke_only = isinstance(manifest, RightsManifest) and manifest.grant is None
            if op not in grant.operations and not revoke_only:
                return refused(f"operation `{op}` is not in this container's grant")
    handler = HANDLERS.get(manifest.type)
    if handler is None:
        return refused(f"`{manifest.type}` is not supported by this broker version")
    try:
        outcome = handler(ctx, manifest)
    except PendingImportError as exc:
        # Slot accounting is unknown while the reservations import is pending:
        # fail closed instead of letting spawns exceed the cap.
        log.warning("refusing %s manifest %s: %s", manifest.type, manifest.id, exc)
        return refused(RESERVATIONS_PENDING)
    _note_delivery(ctx, manifest, outcome, role)
    return outcome


def _note_delivery(ctx: ActionContext, manifest: Manifest, outcome: Outcome, role: Role) -> None:
    """Stamp `last_to_child_at` for a child the orchestrator just wrote to.

    Only an accepted message, update or restart counts: a refused or failed
    one never reached the child, and must not make a silent child look
    recently spoken to.
    """
    if outcome.state not in ("done", "queued", "started"):
        return
    target: str | None = None
    if isinstance(manifest, MessageManifest) and role == "orchestrator":
        target = manifest.to
    elif isinstance(manifest, UpdateManifest | RestartManifest):
        target = manifest.child
    if target is None:
        return
    child = ctx.child(target)
    if isinstance(child, Outcome):
        return
    from jailbee.orchestration import inbox_state

    try:
        inbox_state.note_delivery(ctx.root, ctx.sender.full, child.full, ctx.now.timestamp())
    except Exception:
        # The message is already delivered; a bookkeeping failure must not
        # turn it into a broker error.
        log.exception("could not record delivery to %s", child.full)


def child_preamble(orchestrator_short: str) -> str:
    return (
        f"You are a child container of orchestrator `{orchestrator_short}`. "
        "Use the `jailbee-orchestration-child` skill. "
        "Do not stage GitHub writes; send the orchestrator the exact repository, "
        "target, title, body and action content using `jailbee inbox send message --text`; "
        "use a question when you need a result. "
        "Keep `jailbee inbox wait --timeout none` running in the background "
        "and re-arm it after handling messages. "
        "Message the orchestrator only when it must act or decide "
        "(question, blocked, a scope change, done); send no progress notes. "
        "Within your container you are the controller: run long builds and tests "
        "yourself. Your own subagents never use `jailbee inbox`; tell them to "
        "report back to you, not to the orchestrator."
    )


@dataclass(frozen=True)
class SpawnPlan:
    child_full: str
    kind: Literal["fork", "new", "pr"]
    branch: str
    base: str | None
    prompt: str
    rights: ChildRights
    runner: Runner | None = None
    on_done: Literal["destroy", "keep"] = "destroy"
    ref: str | None = None
    pr: int | None = None
    clone_commit: str | None = None
    pr_base: str | None = None
    autostart_profile: str | None = None
    manifest_id: str = ""
    services: list[str] | None = None
    warm_paths: list[str] | None = None


def _profile_problem(cfg: Config, name: str | None) -> str | None:
    from jailbee.autostart_profiles import ProfileError, validate_profile_name

    if name is not None:
        try:
            validate_profile_name(cfg.autostart, name)
        except ProfileError:
            return f"autostart profile `{name}` is not defined"
    return None


def plan_spawn(
    ctx: ActionContext, m: SpawnManifest, *, queued_names: Collection[str]
) -> SpawnPlan | Outcome:
    """Validate a spawn request; `queued_names` are full names of queued spawns."""
    problem = _profile_problem(ctx.cfg, m.autostart_profile)
    if problem is not None:
        return refused(problem)
    grant = ctx.raw_grant()
    if grant is None:
        return refused(NOT_ALLOWED)
    problems = check_rights(
        grant, profiles.repo_cap(ctx.cfg), m.rights, ctx.cfg.optional_mounts.keys()
    )
    runner = m.runner if m.runner is not None else ctx.default_runner()[0]
    if runner is not None:
        problems += runner_problems(ctx.cfg, ctx.incus, ctx.gcfg, runner)
    if problems:
        return refused("; ".join(problems))
    branch = m.branch
    commit = None
    pr_base = None
    if m.kind == "pr":
        from jailbee import pr

        if m.pr is None:
            return refused("PR number is required")
        try:
            prepared = pr.prepare_pr(
                ctx.cfg.repo_root,
                m.pr,
                remote=ctx.cfg.upstream_remote,
                info=log.info,
                warn=log.warning,
            )
        except pr.PrError as exc:
            return refused(str(exc))
        branch = prepared.info.head_ref
        commit = prepared.commit
        pr_base = prepared.info.base_ref
    try:
        full = lifecycle.derive_container_name(ctx.cfg, branch)
    except ValueError as exc:
        return refused(str(exc))
    short = lifecycle.short_name(ctx.cfg, full)
    if full in ctx.nodes or ctx.incus.exists(full):
        return refused(f"container `{short}` already exists")
    if full in creating_children(ctx):
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
    plan = SpawnPlan(
        full,
        m.kind,
        branch,
        m.base,
        prompt,
        m.rights,
        runner,
        policy,
        m.ref,
        m.pr,
        commit,
        pr_base,
        m.autostart_profile,
        m.id,
        services=m.services,
        warm_paths=m.warm_paths,
    )
    problem = _base_problem(ctx, plan)
    return refused(problem) if problem is not None else plan


def _base_problem(ctx: ActionContext, plan: SpawnPlan) -> str | None:
    """Why a `new` spawn's `--base` can never resolve, or None when it may.

    Orchestrator spawns create with `Interaction.none()`, which sets
    autofetch off (create_flow), so creation never fetches and
    `resolve_clone_ref(autofetch=False)` is exactly the creation-time verdict.
    """
    if plan.kind != "new" or plan.base is None:
        return None
    cfg = ctx.cfg
    opts = child_options(cfg, plan, orchestrator_full=ctx.sender.full, fork=None)
    try:
        lifecycle.resolve_clone_ref(cfg, opts, autofetch=False)
    except ValueError as exc:
        # The creation error spans several lines; a reply detail is one line.
        return " ".join(str(exc).split())
    return None


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
    from jailbee.fork_warm import resolve_warm_paths

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
        clone_commit=fork.commit if fork is not None else plan.clone_commit,
        pr=plan.pr,
        fork_of=fork.full_name if fork is not None else None,
        warm_paths=resolve_warm_paths(cfg, plan.warm_paths) if fork is not None else None,
        inherited_labels=(fork.pr_labels or None) if fork else None,
        base_branch_label=fork.base_branch if fork is not None else plan.pr_base,
        untrusted_head=True,
        initial_prompt=plan.prompt,
        spawned_by=orchestrator_full,
        rights=plan.rights,
        runner=plan.runner,
        on_done=plan.on_done,
        autostart_profile=plan.autostart_profile,
        autostart_services=plan.services,
        assume_yes=False,
    )


def _drop_watch(ctx: ActionContext, child_full: str) -> None:
    """Forget a watch registered for a launch that was refused: nothing runs to supervise."""
    from jailbee.orchestration.watch import cancel

    try:
        cancel(ctx.root, ctx.sender.full, child_full)
    except OSError as exc:
        log.warning("could not drop the watch of refused child %s: %s", child_full, exc)


def start_child(ctx: ActionContext, plan: SpawnPlan, *, fork: ForkSource | None) -> Outcome:
    """Start the background create of a planned child; refusals become outcomes."""
    from jailbee.orchestration.watch import WatchEntry, register

    register(
        ctx.root,
        ctx.sender.full,
        WatchEntry(plan.manifest_id, plan.child_full, "spawn", ctx.now.isoformat(), supervise=True),
    )
    opts = child_options(ctx.cfg, plan, orchestrator_full=ctx.sender.full, fork=fork)
    interaction = replace(Interaction.none(), accept_escalation=escalation_policy(plan.rights))
    try:
        # config_path=None: the worker reloads the repo config from `cfg.repo_root`.
        started = start_create(
            ctx.cfg, ctx.incus, ctx.gcfg, opts, interaction=interaction, config_path=None
        )
    except NeedsOperator as e:
        _drop_watch(ctx, plan.child_full)
        return refused(e.reason)
    except CreateError as e:
        _drop_watch(ctx, plan.child_full)
        return refused(e.message)
    except (ForkError, ValueError) as e:
        _drop_watch(ctx, plan.child_full)
        return refused(str(e))
    try:
        reservations = load_reservations(ctx.root)
        entries = [
            r for r in reservations.get(ctx.sender.full, []) if r.child_full != plan.child_full
        ]
        entries.append(Reservation(plan.child_full, started.pid, ctx.now.isoformat()))
        reservations[ctx.sender.full] = entries
        save_reservations(ctx.root, reservations)
    except OSError as exc:
        log.warning(
            "could not reserve creating child %s for %s: %s", plan.child_full, ctx.sender.full, exc
        )
    # Only the worker is launched here; a later create failure is not reported
    # (plan F), so the status must not claim the child exists.
    short = lifecycle.short_name(ctx.cfg, plan.child_full)
    return Outcome("started", f"creation of child `{short}` started; on done: {plan.on_done}")


GRANT_REVOKED = "orchestration grant revoked"
RESERVATIONS_PENDING = "broker state is still being migrated; try again shortly"


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


def refresh_guard(child: Node) -> Outcome | None:
    from jailbee.db.models import JOB_REFRESH

    if live_job_kind(child.full) == JOB_REFRESH:
        return refused(f"refresh is running in `{child.short}`; try again when it finishes")
    return None


def prune_reservations(root: Path) -> None:
    """Drop finished create workers across orchestrators, reaping our exited children."""
    try:
        reservations = load_reservations(root)
    except PendingImportError as exc:
        log.warning("not pruning reservations: %s", exc)
        return
    live = {
        sender: kept
        for sender, entries in reservations.items()
        if (kept := [r for r in entries if reservation_alive(r.pid)])
    }
    if live != reservations:
        save_reservations(root, live)


def creating_children(ctx: ActionContext) -> set[str]:
    """Names already being created, including reservations with missing job rows."""
    reserved = {r.child_full for entries in load_reservations(ctx.root).values() for r in entries}
    return live_create_jobs(ctx.cfg.container_prefix) | reserved


def slot_breakdown(
    prefix: str, root: Path, sender: str, mine: set[str], running: set[str], existing: set[str]
) -> tuple[int, int, int, int]:
    """Count running children and accepted creations, deduplicated by name."""
    jobs = {name for name in live_create_jobs(prefix) if name in mine or name not in existing}
    reserved = {r.child_full for r in load_reservations(root).get(sender, [])}
    return len(running), len(jobs), len(reserved), len(running | jobs | reserved)


def count_breakdown(ctx: ActionContext) -> tuple[int, int, int, int]:
    """Running, live job, reservation counts and their deduplicated total."""
    children = ctx.children()
    return slot_breakdown(
        ctx.cfg.container_prefix,
        ctx.root,
        ctx.sender.full,
        {n.full for n in children},
        {n.full for n in children if n.running},
        set(ctx.nodes),
    )


def running_children(ctx: ActionContext) -> int:
    """Children running plus children still being created, counted once each.

    Reservations make the count independent of the job row, whose insert is best-effort.
    """
    return count_breakdown(ctx)[-1]


def _log_spawn_decision(
    ctx: ActionContext, child: str, counts: tuple[int, int, int, int], limit: int, state: str
) -> None:
    log.info(
        "spawn %s for %s: running=%d creating(jobs)=%d reserved=%d total=%d limit=%d -> %s",
        child,
        ctx.sender.full,
        *counts,
        limit,
        state,
    )


def _queued(ctx: ActionContext) -> list[QueuedRequest]:
    return load_queue(ctx.root).get(ctx.sender.full, [])


def _queued_spawn_refusal(ctx: ActionContext, short: str) -> Outcome | None:
    for req in _queued(ctx):
        if req.kind != "restart" and lifecycle.short_name(ctx.cfg, req.child_full) == short:
            return refused(
                f"`{short}` is a queued spawn, not a child yet; "
                f"use jailbee inbox send cancel --request {req.manifest_id}"
            )
    return None


def _store(ctx: ActionContext, entries: list[QueuedRequest]) -> None:
    queues = load_queue(ctx.root)
    queues[ctx.sender.full] = entries
    save_queue(ctx.root, queues)


def handle_spawn(ctx: ActionContext, m: Manifest) -> Outcome:
    from jailbee.orchestration.watch import mutation_locked

    with mutation_locked(ctx.root):
        return _handle_spawn_locked(ctx, m)


def _handle_spawn_locked(ctx: ActionContext, m: Manifest) -> Outcome:
    """Plan a spawn; start it when a slot is free, else queue it FIFO."""
    from jailbee import git

    if not isinstance(m, SpawnManifest):
        return refused("not a spawn manifest")
    grant = ctx.grant()
    if grant is None:
        return refused(NOT_ALLOWED)
    from jailbee.orchestration.state import load_cancellations

    _settle_cancellations_locked(ctx)
    pending = load_cancellations(ctx.root)
    queued = _queued(ctx)
    plan = plan_spawn(
        ctx, m,
        queued_names={q.child_full for q in queued} | {e.request.child_full for e in pending},
    )
    if isinstance(plan, Outcome):
        return plan
    fork: ForkSource | None = None
    if plan.kind == "fork":
        try:
            if plan.ref is None:
                fork = forking.prepare_fork(ctx.cfg, ctx.incus, ctx.sender.short)
            else:
                fork = forking.prepare_fork(ctx.cfg, ctx.incus, ctx.sender.short, ref=plan.ref)
            forking.pin_fork_commit(ctx.cfg, plan.child_full, fork.commit)
        except (ForkError, git.GitError) as e:
            return refused(str(e))
    if plan.kind == "pr":
        try:
            assert plan.clone_commit is not None
            forking.pin_fork_commit(ctx.cfg, plan.child_full, plan.clone_commit)
        except git.GitError as e:
            return refused(str(e))
    # A non-empty queue goes first: a new spawn never overtakes it.
    counts = count_breakdown(ctx)
    if not queued and counts[-1] < grant.max_running_children:
        outcome = start_child(ctx, plan, fork=fork)
        _log_spawn_decision(ctx, plan.child_full, counts, grant.max_running_children, outcome.state)
        return outcome
    _log_spawn_decision(ctx, plan.child_full, counts, grant.max_running_children, "queued")
    queued.append(
        QueuedRequest(
            manifest_id=m.id,
            child_full=plan.child_full,
            kind=plan.kind,
            commit=fork.commit if fork is not None else plan.clone_commit,
            queued_at=ctx.now.isoformat(),
            branch=plan.branch,
            base=plan.base,
            prompt=plan.prompt,
            rights=plan.rights.model_dump(mode="json"),
            runner=plan.runner.model_dump(mode="json") if plan.runner else None,
            runner_set=plan.runner is not None,
            on_done=plan.on_done,
            autostart_profile=plan.autostart_profile,
            services=plan.services,
            warm_paths=plan.warm_paths,
            ref=plan.ref,
            pr=plan.pr,
            pr_base=plan.pr_base,
            fork_base=fork.base_branch if fork is not None else None,
            fork_base_pinned=fork is not None and plan.ref is not None,
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
    autostart_profile: str | None = None,
    services: list[str] | None = None,
    manifest_id: str = "",
    resume: str | None = None,
) -> Outcome:
    problem = _profile_problem(ctx.cfg, autostart_profile)
    if problem is not None:
        return refused(problem)
    # Before the boot: a restarted child commits anew, so a HEAD recorded at
    # its last stop (or its last `done`) no longer describes it, and it is
    # `working` again (absent STATE = working). Clearing first means a running
    # child never carries a stale value.
    try:
        for key in (
            labels.STOPPED_HEAD,
            labels.DONE_SHA,
            labels.STATE,
            labels.PENDING_DESTROY,
            labels.STOPPED_DIRTY,
            labels.STOPPED_OUTBOX,
        ):
            ctx.incus.config_unset_checked(child.full, key)
        if set_runner:
            if runner is not None:
                ctx.incus.config_set(child.full, labels.RUNNER, runner.to_label())
            else:
                ctx.incus.config_unset_checked(child.full, labels.RUNNER)
        _set_resume(ctx, child, resume)
        if on_done is not None:
            ctx.incus.config_set(child.full, labels.ON_DONE, on_done)
        if autostart_profile is not None:
            from jailbee.autostart_profiles import PROFILE_KEY

            ctx.incus.config_set(child.full, PROFILE_KEY, autostart_profile)
        if services is not None:
            from jailbee.autostart_profiles import SERVICES_KEY, encode_names

            ctx.incus.config_set(child.full, SERVICES_KEY, encode_names(services))
    except Exception as e:
        return refused(f"child `{child.short}` could not be restarted: {e}")
    try:
        # boot_container alone never runs post-start actions or launches agents.
        # The full worker is required for unprofiled containers too.
        from jailbee.create_flow import launch_boot_background
        from jailbee.orchestration.watch import WatchEntry, register

        register(
            ctx.root,
            ctx.sender.full,
            WatchEntry(manifest_id, child.full, "restart", ctx.now.isoformat(), supervise=True),
        )
        worker = launch_boot_background(
            ctx.cfg, container_name=child.full, initial_prompt=prompt, config_path=None
        )
        try:
            reservations = load_reservations(ctx.root)
            entries = [
                r for r in reservations.get(ctx.sender.full, []) if r.child_full != child.full
            ]
            entries.append(Reservation(child.full, worker.pid, ctx.now.isoformat()))
            reservations[ctx.sender.full] = entries
            save_reservations(ctx.root, reservations)
        except OSError as exc:
            log.warning("could not reserve booting child %s: %s", child.full, exc)
        log.info("restart of %s started; boot log: %s", child.full, worker.log_path)
        return Outcome("started", f"child `{child.short}` restarting")
    except Exception as e:
        _drop_watch(ctx, child.full)
        return refused(f"child `{child.short}` could not be restarted: {e}")


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
    problem = _profile_problem(ctx.cfg, req.autostart_profile)
    if problem is not None:
        return refused(problem)
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
    if req.child_full in creating_children(ctx):
        return refused(f"container `{short}` is already being created")
    grant = ctx.grant()
    plan = SpawnPlan(
        child_full=req.child_full,
        kind=req.kind if req.kind != "restart" else "new",
        branch=req.branch,
        base=req.base,
        prompt=req.prompt,
        rights=rights,
        runner=runner,
        ref=req.ref,
        pr=req.pr,
        clone_commit=req.commit if req.kind == "pr" else None,
        pr_base=req.pr_base,
        autostart_profile=req.autostart_profile,
        services=req.services,
        warm_paths=req.warm_paths,
        manifest_id=req.manifest_id,
        on_done=(
            "destroy"
            if req.on_done == "destroy" and grant is not None and "destroy" in grant.operations
            else "keep"
        ),
    )
    fork: ForkSource | None = None
    if req.kind == "fork":
        if req.commit is None:
            return refused("queued fork has no pinned commit")
        fork = ForkSource(
            full_name=ctx.sender.full,
            branch=req.branch,
            commit=req.commit,
            base_branch=(
                req.fork_base
                if req.fork_base_pinned
                else ctx.incus.config_get(ctx.sender.full, "user.jailbee.base_branch") or None
            ),
            untrusted=bool(ctx.incus.config_get(ctx.sender.full, "user.jailbee.pr")),
            pr_labels=(
                {}
                if req.ref is not None
                else forking.inherited_pr_labels(ctx.incus, ctx.sender.full)
            ),
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
    if node.full in creating_children(ctx):
        return refused(f"child `{short}` is still being created")
    runner = Runner.model_validate(req.runner) if req.runner is not None else None
    effective = runner if req.runner_set else parse_runner_label(node.config.get(labels.RUNNER))
    if effective is not None:
        problems = runner_problems(ctx.cfg, ctx.incus, ctx.gcfg, effective)
        if problems:
            return refused("; ".join(problems))
    if req.resume is not None:
        from jailbee.agents import primary_agent

        agent = "claude" if effective is not None else primary_agent(ctx.cfg).name
        if not labels.valid_session_id(req.resume):
            return refused("queued restart has an invalid pinned session")
        resume = _resolve_resume(ctx, node, req.resume, agent, pinned=True)
        if isinstance(resume, Outcome):
            return resume
    grant = ctx.grant()
    policy = req.on_done
    if policy == "destroy" and (grant is None or "destroy" not in grant.operations):
        policy = "keep"
    return _boot(
        ctx,
        node,
        req.prompt,
        started=True,
        runner=runner,
        set_runner=req.runner_set,
        on_done=policy,
        autostart_profile=req.autostart_profile,
        services=req.services,
        manifest_id=req.manifest_id,
        resume=req.resume,
    )


# Queue kind -> starter.
STARTERS: dict[str, Callable[[ActionContext, QueuedRequest], Outcome]] = {
    "fork": _start_spawn,
    "new": _start_spawn,
    "pr": _start_spawn,
    "restart": _start_restart,
}


def drain_queue(ctx: ActionContext) -> list[tuple[QueuedRequest, Outcome]]:
    from jailbee.orchestration.watch import mutation_locked

    with mutation_locked(ctx.root):
        return _drain_queue_locked(ctx)


def _drain_queue_locked(ctx: ActionContext) -> list[tuple[QueuedRequest, Outcome]]:
    """Start queued requests FIFO while slots are free; a lowered limit starts nothing."""
    grant = ctx.grant()
    queued = _queued(ctx)
    if grant is None or not queued:
        return []
    try:
        counts = count_breakdown(ctx)
    except PendingImportError as exc:
        log.warning("queue not drained, slot accounting unavailable: %s", exc)
        return []
    free = grant.max_running_children - counts[-1]
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
        _log_spawn_decision(ctx, req.child_full, counts, grant.max_running_children, outcome.state)
        if outcome.state == "started":
            free -= 1
        results.append((req, outcome))
        # The nodes snapshot is unchanged this round; keep the drain's slot accounting.
        try:
            fresh = count_breakdown(ctx)
        except PendingImportError as exc:
            log.warning("queue drain stopped, slot accounting unavailable: %s", exc)
            break
        counts = (*fresh[:3], grant.max_running_children - free)
    if queued:
        _log_spawn_decision(ctx, queued[0].child_full, counts, grant.max_running_children, "queued")
    return results


def drop_queue(ctx: ActionContext, reason: str) -> list[QueuedRequest]:
    """Remove and return the sender's queued requests; the caller reports `reason` per entry."""
    queued = _queued(ctx)
    if queued:
        _store(ctx, [])
    return queued


def settle_cancellations(ctx: ActionContext) -> None:
    from jailbee.orchestration.watch import mutation_locked

    with mutation_locked(ctx.root):
        _settle_cancellations_locked(ctx)


def _settle_cancellations_locked(ctx: ActionContext) -> None:
    """Retry durable cancellation replies/cleanup before allowing a name to be reused."""
    from jailbee import git
    from jailbee.orchestration import broker_replies
    from jailbee.orchestration.state import finish_cancellation, load_cancellations

    for entry in load_cancellations(ctx.root, ctx.sender.full):
        if entry.recipient_key != ctx.sender.key:
            finish_cancellation(ctx.root, ctx.sender.full, entry.cancel_id)
            continue
        try:
            if entry.request.kind in {"fork", "pr"}:
                git.delete_ref_checked(ctx.cfg.repo_root, forking.fork_pin_ref(ctx.cfg, entry.request.child_full))
            broker_replies.reply(
                ctx, entry.request.manifest_id,
                refused(f"cancelled by orchestrator (cancel request {entry.cancel_id})"),
                status_id=entry.status_id,
            )
            finish_cancellation(ctx.root, ctx.sender.full, entry.cancel_id)
        except (OSError, git.GitError):
            log.exception("could not settle cancelled request %s", entry.request.manifest_id)


def handle_cancel(ctx: ActionContext, m: Manifest) -> Outcome:
    from jailbee.orchestration.protocol import new_host_id
    from jailbee.orchestration.state import CancelledRequest, cancel_queued
    from jailbee.orchestration.watch import mutation_locked

    if not isinstance(m, CancelManifest):
        return refused("not a cancel manifest")
    with mutation_locked(ctx.root):
        queued = _queued(ctx)
        req = next(
            (q for q in queued if (q.manifest_id == m.request if m.request is not None
             else lifecycle.short_name(ctx.cfg, q.child_full) == m.child)),
            None,
        )
        if req is None:
            return refused(
                "request is not queued for this orchestrator; "
                "if already started, use send stop / send destroy --child instead"
            )
        cancel_queued(ctx.root, ctx.sender.full, CancelledRequest(req, m.id, ctx.sender.key, new_host_id()))
        _settle_cancellations_locked(ctx)
        return done(f"cancelled queued request {req.manifest_id}")


HANDLERS["spawn"] = handle_spawn
HANDLERS["cancel"] = handle_cancel


def handle_stop(ctx: ActionContext, m: Manifest) -> Outcome:
    from jailbee.orchestration.watch import mutation_locked

    with mutation_locked(ctx.root):
        return _handle_stop_locked(ctx, m)


def _handle_stop_locked(ctx: ActionContext, m: Manifest) -> Outcome:
    if not isinstance(m, StopManifest):
        return refused("not a stop manifest")
    child = ctx.child(m.child)
    if isinstance(child, Outcome):
        return child
    if (guard := refresh_guard(child)) is not None:
        return guard
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
                bounded,
                child.full,
                lifecycle.container_repo_dir(ctx.cfg, bounded, child.full),
                uid=ctx.cfg.container_user.uid,
            )
            if dirty is False:
                ctx.incus.config_unset_checked(child.full, labels.STOPPED_DIRTY)
            else:
                ctx.incus.config_set(child.full, labels.STOPPED_DIRTY, "1")
        except Exception as e:
            return refused(f"child `{child.short}` could not be stopped: {e}")
        # A clean shutdown that outlives its budget escalates once to a forced
        # stop: the orchestrator has no other way to recover a wedged child.
        try:
            forced = stopping.stop_container(
                ctx.incus,
                child.full,
                show_progress=False,
                force_fallback=True,
                disposable=False,
            )
        except Exception as e:
            return refused(f"child `{child.short}` could not be stopped: {e}")
    else:
        forced = False
    from jailbee.orchestration.watch import cancel

    cancel(ctx.root, ctx.sender.full, child.full)
    if forced:
        return done(
            f"child `{child.short}` stopped (clean shutdown timed out after "
            f"{stopping.CLEAN_STOP_BUDGET}s; the stop was forced)"
        )
    return done(f"child `{child.short}` stopped")


def _set_resume(ctx: ActionContext, child: Node, resume: str | None) -> None:
    if resume is None:
        ctx.incus.config_unset_checked(child.full, labels.RESUME)
    else:
        ctx.incus.config_set(child.full, labels.RESUME, resume)


def _resolve_resume(
    ctx: ActionContext,
    child: Node,
    requested: str | None,
    agent: str | None,
    *,
    pinned: bool = False,
) -> str | Outcome | None:
    if requested is None:
        return None
    from jailbee.accounts.adapters.base import get_adapter

    try:
        adapter = get_adapter(agent or "")
        probe = "00000000-0000-0000-0000-000000000000" if requested == "last" else requested
        if adapter.resume_args(probe) is None:
            return refused(f"agent `{agent}` cannot resume a session")
    except (KeyError, LookupError):
        return refused(f"agent `{agent}` cannot resume a session")
    history = labels.sessions_of(child.config)
    if requested == "last":
        if not history:
            return refused(f"child `{child.short}` has no recorded agent session")
        session_id = history[-1]
    else:
        session_id = requested
        # A queued pin was checked on acceptance; bounded history may have evicted it.
        if not pinned and session_id not in history:
            return refused(f"session `{session_id}` was not recorded for child `{child.short}`")
    if not adapter.session_exists(ctx.cfg, child.full, session_id):
        return refused(f"session `{session_id}` has no transcript any more")
    return session_id


def _restart_agent(
    ctx: ActionContext,
    child: Node,
    m: RestartManifest,
    prompt: str,
    runner: Runner | None,
    *,
    set_runner: bool,
    resume: str | None,
) -> Outcome:
    from jailbee import initial_prompt
    from jailbee.agents import primary_agent
    from jailbee.orchestration import agent_session, agent_watch
    from jailbee.orchestration.watch import WatchEntry, load_watch, register

    if not child.running:
        return refused(f"child `{child.short}` must be running for agent_only")
    if child.full in creating_children(ctx) or live_job_kind(child.full) is not None:
        return refused(f"child `{child.short}` is working (startup is still running)")
    status = agent_watch.probe(ctx.incus, child.full, uid=ctx.cfg.container_user.uid)
    previous = next(
        (e for e in load_watch(ctx.root).get(ctx.sender.full, []) if e.child_full == child.full),
        None,
    )
    try:
        live_config = {
            key: ctx.incus.config_get_checked(child.full, key) or ""
            for key in (labels.STATE, labels.DONE_SHA, labels.LAST_IMPORT)
        }
    except Exception as exc:
        return refused(f"child `{child.short}` state could not be checked: {exc}")
    finished = labels.is_finished(live_config)
    ended = (
        status is not None
        and not status.alive
        and (
            status.rc is not None
            or status.recorded
            or (previous is not None and previous.confirmed)
        )
    )
    if not finished and not ended:
        return refused(f"child `{child.short}` is working; wait for accepted done or agent exit")
    problem = initial_prompt.prompt_problem(ctx.cfg, runner)
    if problem is not None:
        return refused(problem)
    try:
        initial_prompt.validate_prompt_text(prompt)
    except initial_prompt.PromptError as exc:
        return refused(str(exc))
    old_runner = parse_runner_label(child.config.get(labels.RUNNER))
    old_agent = (
        status.agent
        if status is not None and status.agent != "agent"
        else "claude"
        if old_runner is not None
        else primary_agent(ctx.cfg).name
    )
    if old_agent is None:
        return refused(f"child `{child.short}`: previous agent could not be identified")
    try:
        register(
            ctx.root,
            ctx.sender.full,
            WatchEntry(
                m.id,
                child.full,
                "restart",
                ctx.now.isoformat(),
                supervise=True,
                agent_only=True,
            ),
        )
        for key in (
            labels.STOPPED_HEAD,
            labels.DONE_SHA,
            labels.STATE,
            labels.PENDING_DESTROY,
            labels.STOPPED_DIRTY,
            labels.STOPPED_OUTBOX,
        ):
            ctx.incus.config_unset_checked(child.full, key)
        if set_runner:
            if runner is None:
                ctx.incus.config_unset_checked(child.full, labels.RUNNER)
            else:
                ctx.incus.config_set(child.full, labels.RUNNER, runner.to_label())
        _set_resume(ctx, child, resume)
        if m.on_done is not None:
            ctx.incus.config_set(child.full, labels.ON_DONE, m.on_done)
        agent_session.relaunch(
            ctx.cfg,
            ctx.incus,
            child.full,
            prompt,
            runner=runner,
            old_agent=old_agent,
            resume=resume,
        )
    except Exception as exc:
        _drop_watch(ctx, child.full)
        return refused(f"child `{child.short}` agent could not be restarted: {exc}")
    return Outcome("started", f"child `{child.short}` agent restarting (container kept running)")


def handle_restart(ctx: ActionContext, m: Manifest) -> Outcome:
    from jailbee.orchestration.watch import mutation_locked

    with mutation_locked(ctx.root):
        return _handle_restart_locked(ctx, m)


def _handle_restart_locked(ctx: ActionContext, m: Manifest) -> Outcome:
    """Restart a stopped child with a fresh prompt; queue FIFO when no slot is free."""
    if not isinstance(m, RestartManifest):
        return refused("not a restart manifest")
    if (queued_refusal := _queued_spawn_refusal(ctx, m.child)) is not None:
        return queued_refusal
    if m.agent_only and m.autostart_profile is not None:
        return refused("autostart_profile cannot be combined with agent_only (no autostart runs)")
    if m.agent_only and m.services is not None:
        return refused("services cannot be combined with agent_only (no autostart runs)")
    problem = _profile_problem(ctx.cfg, m.autostart_profile)
    if problem is not None:
        return refused(problem)
    grant = ctx.grant()
    if grant is None:
        return refused(NOT_ALLOWED)
    if m.on_done == "destroy" and "destroy" not in grant.operations:
        return refused("on_done destroy needs the `destroy` operation in this container's grant")
    child = ctx.child(m.child)
    if isinstance(child, Outcome):
        return child
    if (guard := refresh_guard(child)) is not None:
        return guard
    if child.running and not m.agent_only:
        return refused(f"child `{child.short}` is running; stop it first")
    if child.full in creating_children(ctx):
        return refused(f"child `{child.short}` is still being created")
    set_runner = restart_sets_runner(m)
    # A restart that names no runner keeps the child's label; that kept runner
    # must still be launchable, so it is checked like a named one.
    runner = ctx.default_runner()[0] if set_runner and m.runner is None else m.runner
    effective = runner if set_runner else parse_runner_label(child.config.get(labels.RUNNER))
    if effective is not None:
        problems = runner_problems(ctx.cfg, ctx.incus, ctx.gcfg, effective)
        if problems:
            return refused("; ".join(problems))
    from jailbee.agents import primary_agent

    agent = "claude" if effective is not None else primary_agent(ctx.cfg).name
    resume = _resolve_resume(ctx, child, m.resume, agent)
    if isinstance(resume, Outcome):
        return resume
    prompt = f"{child_preamble(ctx.sender.short)}\n\n{m.prompt}"
    queued = _queued(ctx)
    if any(q.child_full == child.full for q in queued):
        return refused(f"a request for `{child.short}` is already queued")
    if m.agent_only:
        return _restart_agent(
            ctx, child, m, prompt, effective, set_runner=set_runner, resume=resume
        )
    if not queued and running_children(ctx) < grant.max_running_children:
        return _boot(
            ctx,
            child,
            prompt,
            started=False,
            runner=runner,
            set_runner=set_runner,
            on_done=m.on_done,
            autostart_profile=m.autostart_profile,
            services=m.services,
            manifest_id=m.id,
            resume=resume,
        )
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
            runner=runner.model_dump(mode="json") if runner else None,
            runner_set=set_runner,
            resume=resume,
            on_done=m.on_done,
            autostart_profile=m.autostart_profile,
            services=m.services,
        )
    )
    _store(ctx, queued)
    return Outcome("queued", f"queued at position {len(queued)}")


def destroy_guard(ctx: ActionContext, child: Node) -> str | None:
    """Keep unimported commits, dirty trees and staged GitHub writes."""
    from jailbee.orchestration import child_outbox

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
                bounded,
                child.full,
                lifecycle.container_repo_dir(ctx.cfg, bounded, child.full),
                uid=ctx.cfg.container_user.uid,
            )
        except Exception:
            return (
                f"child `{child.short}`: its working tree could not be checked "
                "(use force: true to destroy anyway)"
            )
    else:
        dirty = child.config.get(labels.STOPPED_DIRTY) == "1"
    if dirty:
        return f"child `{child.short}` has uncommitted changes (use force: true to destroy anyway)"
    # The host outbox store holds a stopped child's proposals too.
    try:
        pending = child_outbox.probe(child.config.get("volatile.uuid"))
    except child_outbox.ChildOutboxError:
        return child_outbox.unchecked(child.short)
    if pending.count:
        return child_outbox.describe(child.short, pending)
    return None


def handle_destroy(ctx: ActionContext, m: Manifest) -> Outcome:
    from jailbee.orchestration.watch import mutation_locked

    with mutation_locked(ctx.root):
        return _handle_destroy_locked(ctx, m)


def _handle_destroy_locked(ctx: ActionContext, m: Manifest) -> Outcome:
    # No drain here: the broker drains after every round and reports each outcome.
    if not isinstance(m, DestroyManifest):
        return refused("not a destroy manifest")
    if (queued_refusal := _queued_spawn_refusal(ctx, m.child)) is not None:
        return queued_refusal
    child = ctx.child(m.child)
    if isinstance(child, Outcome):
        return child
    if (guard := refresh_guard(child)) is not None:
        return guard
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
    from jailbee.orchestration.watch import cancel

    cancel(ctx.root, ctx.sender.full, child.full)
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


def _handle_rights(ctx: ActionContext, m: Manifest) -> Outcome:
    from jailbee.orchestration.rights import handle_rights

    assert isinstance(m, RightsManifest)
    return handle_rights(ctx, m)


HANDLERS["rights"] = _handle_rights


def _handle_refresh(ctx: ActionContext, m: Manifest) -> Outcome:
    from jailbee.orchestration.refresh import handle_refresh

    return handle_refresh(ctx, m)


HANDLERS["refresh"] = _handle_refresh
