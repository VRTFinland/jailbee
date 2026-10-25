"""Broker handler for an orchestrator importing a child's branch."""

from __future__ import annotations

import re

from jailbee import sync
from jailbee.git import GitError
from jailbee.incus import IncusError
from jailbee.orchestration import labels
from jailbee.orchestration.actions import (
    CHILD_EXEC_TIMEOUT_S,
    CHILD_TRANSPORT_TIMEOUT_S,
    ActionContext,
    Outcome,
    done,
    refused,
)
from jailbee.orchestration.protocol import ImportManifest, Manifest
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
    try:
        result = sync.merge_container_into_container(
            ctx.cfg,
            ctx.incus.with_exec_timeout(CHILD_EXEC_TIMEOUT_S),
            child.short,
            ctx.sender.short,
            plain=True,
            ff_local_branch=False,
            timeout=CHILD_TRANSPORT_TIMEOUT_S,
        )
    except (SyncError, GitError, IncusError) as exc:
        return refused(str(exc))
    ref = result.push.container_ref
    oid = result.push.new_oid
    if not _FULL_OID.fullmatch(oid or ""):
        return refused(f"imported as {ref}, but its commit id `{oid}` could not be recorded")
    try:
        # STATE last: a crash in between never shows `imported` without a commit.
        ctx.incus.config_set(child.full, labels.LAST_IMPORT, oid)
        ctx.incus.config_set(child.full, labels.STATE, labels.STATE_IMPORTED)
    except IncusError as exc:
        return refused(f"imported but could not record the import: {exc}")
    return done(f"imported as {ref} at {oid[:12]}")
