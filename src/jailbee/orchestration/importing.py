"""Broker handler for an orchestrator importing a child's branch."""

from __future__ import annotations

import re
from dataclasses import dataclass

from jailbee import forking, git, lifecycle, submodules, sync
from jailbee.git import GitError
from jailbee.incus import IncusError
from jailbee.orchestration import labels, outbox
from jailbee.orchestration.actions import (
    CHILD_EXEC_TIMEOUT_S,
    CHILD_TRANSPORT_TIMEOUT_S,
    ActionContext,
    Node,
    Outcome,
    done,
    refused,
)
from jailbee.orchestration.protocol import (
    ImportManifest,
    InboxMessage,
    Manifest,
    UpdateManifest,
    new_host_id,
)
from jailbee.sync import SyncError

_FULL_OID = re.compile(r"[0-9a-f]{40}")


def handle_import(ctx: ActionContext, manifest: Manifest) -> Outcome:
    """Transport the child's branch into the orchestrator, then record the import.

    Transport only (`plain=True`): the orchestrator merges from the ref named in
    the outcome. `ff_local_branch=False` keeps the branch name the child chose
    from touching the orchestrator's own `refs/heads/*` (spec 7.2: the branch
    lands at `refs/jailbee/from/<child>/<branch>` without merging). Execs into
    both containers and every git transport are bounded, as for `done`.
    Labels are written only after the transport succeeded.
    """
    if not isinstance(manifest, ImportManifest):
        return refused("not an import manifest")
    child = ctx.child(manifest.child)
    if isinstance(child, Outcome):
        return child
    if not child.running:
        return refused(f"{child.short} is not running; restart it first")
    result = import_child(ctx, child, ctx.sender)
    if isinstance(result, str):
        return refused(result)
    return done(f"imported as {result.ref} at {result.oid[:12]}")


@dataclass(frozen=True)
class Imported:
    ref: str
    oid: str


def imported_ref_matches(ctx: ActionContext, orchestrator: Node, ref: str, oid: str) -> bool:
    """A child baseline label alone is not proof of an import into this target."""
    bounded = ctx.incus.with_exec_timeout(CHILD_EXEC_TIMEOUT_S)
    try:
        repo_dir = lifecycle.container_repo_dir(ctx.cfg, bounded, orchestrator.full)
        return (
            sync._container_ref_oid(
                bounded, orchestrator.full, repo_dir, ref, uid=ctx.cfg.container_user.uid
            )
            == oid
        )
    except (SyncError, GitError, IncusError):
        return False


def import_child(ctx: ActionContext, child: Node, orchestrator: Node) -> Imported | str:
    """Transport a child's branch, recording LAST_IMPORT before STATE."""
    try:
        result = sync.merge_container_into_container(
            ctx.cfg,
            ctx.incus.with_exec_timeout(CHILD_EXEC_TIMEOUT_S),
            child.short,
            orchestrator.short,
            plain=True,
            ff_local_branch=False,
            require_clean_target=False,
            timeout=CHILD_TRANSPORT_TIMEOUT_S,
        )
    except (SyncError, GitError, IncusError) as exc:
        return str(exc)
    ref = result.push.container_ref
    oid = result.push.new_oid
    if not _FULL_OID.fullmatch(oid or ""):
        return f"imported as {ref}, but its commit id `{oid}` could not be recorded"
    try:
        # STATE last: a crash in between never shows `imported` without a commit.
        ctx.incus.config_set(child.full, labels.LAST_IMPORT, oid)
        ctx.incus.config_set(child.full, labels.STATE, labels.STATE_IMPORTED)
    except IncusError as exc:
        return f"imported but could not record the import: {exc}"
    return Imported(ref, oid)


def _update_source(ctx: ActionContext, manifest: UpdateManifest) -> str:
    """Resolve explicit sources using the same policies as spawn."""
    bounded = ctx.incus.with_exec_timeout(CHILD_EXEC_TIMEOUT_S)
    if manifest.ref is not None:
        return forking.prepare_fork(
            ctx.cfg, bounded, ctx.sender.short, ref=manifest.ref,
            timeout=CHILD_TRANSPORT_TIMEOUT_S,
        ).commit
    assert manifest.base is not None
    opts = lifecycle.NewContainerOptions(
        container_branch=manifest.base,
        base=manifest.base,
        name=None,
        network="strict",
        memory=None,
        cpu=ctx.cfg.defaults.cpu,
        from_base=ctx.cfg.golden.alias,
        clone=True,
    )
    resolved = lifecycle.resolve_clone_ref(ctx.cfg, opts, autofetch=ctx.cfg.new.autofetch)
    commit = resolved.checkout_commit or git.rev_parse(
        ctx.cfg.repo_root, f"refs/heads/{resolved.source_branch}^{{commit}}"
    )
    if commit is None:
        raise SyncError(f"Cannot resolve base branch '{manifest.base}' to a commit")
    return commit


def handle_update(ctx: ActionContext, manifest: Manifest) -> Outcome:
    """Relay a parent source to a child; the child chooses when to merge."""
    if not isinstance(manifest, UpdateManifest):
        return refused("not an update manifest")
    child = ctx.child(manifest.child)
    if isinstance(child, Outcome):
        return child
    if not child.running:
        return refused(f"{child.short} is not running; restart it first")
    bounded = ctx.incus.with_exec_timeout(CHILD_EXEC_TIMEOUT_S)
    try:
        if manifest.ref is not None or manifest.base is not None:
            branch = manifest.ref or manifest.base
            commit = _update_source(ctx, manifest)
            repo_dir = lifecycle.container_repo_dir(ctx.cfg, bounded, child.full)
            submodules.transport_submodules_to_container(
                ctx.cfg, bounded, child.full, repo_dir=repo_dir,
                commit=commit, timeout=CHILD_TRANSPORT_TIMEOUT_S, pin_commits=True,
            )
            push = sync.push_to_container(
                ctx.cfg, bounded, child.short,
                source=manifest.id, source_ref=commit,
                namespace=f"update/{ctx.sender.short}", ff_local_branch=False,
                timeout=CHILD_TRANSPORT_TIMEOUT_S,
            )
        else:
            repo_dir = lifecycle.container_repo_dir(ctx.cfg, bounded, ctx.sender.full)
            branch = sync._container_current_branch(
                bounded, ctx.sender.full, repo_dir, uid=ctx.cfg.container_user.uid
            )
            if branch is None:
                return refused("detached HEAD: check out your branch before update")
            result = sync.merge_container_into_container(
                ctx.cfg,
                bounded,
                ctx.sender.short,
                child.short,
                branch=branch,
                plain=True,
                ff_local_branch=False,
                require_clean_target=False,
                timeout=CHILD_TRANSPORT_TIMEOUT_S,
            )
            push = result.push
    except (SyncError, GitError, IncusError, ValueError, submodules.SubmoduleError) as exc:
        return refused(str(exc))
    ref, oid = push.container_ref, push.new_oid
    if not _FULL_OID.fullmatch(oid or ""):
        return refused(f"updated as {ref}, but its commit id `{oid}` could not be recorded")
    msg = InboxMessage.model_validate(
        {
            "version": 1,
            "id": new_host_id(),
            "type": "message",
            "kind": "update",
            "from": ctx.sender.short,
            "imported_ref": ref,
            "imported_sha": oid,
            "body": (
                f"{ctx.sender.short} updated {branch}: merge {ref} ({oid[:12]}) "
                "into your branch when convenient"
            ),
        }
    )
    outbox.deliver(
        ctx.incus,
        ctx.root,
        child.full,
        msg,
        running=True,
        uid=ctx.cfg.container_user.uid,
        now=ctx.now,
        seq=next(ctx.seq),
    )
    return done(f"{child.short} has {ref} at {oid[:12]}")
