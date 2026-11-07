"""Local, revision-checked outbox deletion coordinated with publication."""

from __future__ import annotations

import time
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING

from jailbee.incus import IncusError
from jailbee.outbox.delete import DeletePlan, plan_delete
from jailbee.outbox.inspect import build_views
from jailbee.outbox.io import READ_TIMEOUT, PrManagement
from jailbee.outbox.models import (
    ContainerView,
    Kind,
    OutboxChanged,
    OutboxExecutionError,
    ProposalView,
    StoreSnapshot,
)
from jailbee.outbox_io import (
    ContainerIdentity,
    IssueJournal,
    JournalError,
)


def _instance(incus: Incus, container: str) -> tuple[ContainerIdentity, str | None]:
    """The instance's identity and its Incus ``volatile.uuid`` (None if unset)."""
    # `outbox_io.container_identity` has no timeout parameter. Keep the same
    # raw/nonzero timestamp contract while bounding this interactive service.
    # --fast: created_at and config are top-level keys; no instance state is needed.
    raw = next(
        (
            item
            for item in incus.list_containers(fast=True, timeout=READ_TIMEOUT)
            if item.get("name") == container
        ),
        None,
    )
    created = raw.get("created_at") if raw is not None else None
    if not isinstance(created, str) or not created or created.startswith("0001-01-01T00:00:00"):
        raise JournalError(f"{container}: stable instance identity is unavailable")
    config = raw.get("config") if raw is not None else None
    uuid = config.get("volatile.uuid") if isinstance(config, dict) else None
    return ContainerIdentity(container, created), uuid if isinstance(uuid, str) and uuid else None


def instance_identity(incus: Incus, container: str) -> tuple[ContainerIdentity, str]:
    """The instance's identity and its uuid, the outbox store's key for it."""
    identity, uuid = _instance(incus, container)
    if uuid is None:
        raise JournalError(f"{container}: instance uuid is unavailable")
    return identity, uuid


def _identity(incus: Incus, container: str) -> ContainerIdentity:
    return _instance(incus, container)[0]


def issue_snapshot(container: str, uuid: str | None) -> StoreSnapshot:
    """The instance's pending issue proposals from the host outbox store.

    Reads ``broker.db`` read-only: no Incus call, and it works with the broker
    stopped. Bodies appear next to their manifests, as in the file layout.
    """
    from jailbee.db import state_dir
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import store_db

    if uuid is None:
        raise JournalError(f"{container}: instance uuid is unavailable")
    try:
        rows = store_db.read_proposals(state_dir() / "orchestration", uuid, "issue")
    except StoreError as exc:
        raise OutboxExecutionError(f"outbox store is unreadable: {exc}") from exc
    files: list[tuple[str, str]] = []
    for row in rows:
        files.append((row.name, row.manifest))
        files.extend(row.bodies.items())
    versions = tuple((row.name, row.revision) for row in rows)
    return StoreSnapshot("issue", tuple(sorted(files)), (), (), versions)


def issue_journals(
    container: str, identity: ContainerIdentity, uuid: str | None
) -> dict[str, IssueJournal]:
    """The instance's active issue publication journals from the host outbox store."""
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import publication_db

    if uuid is None:
        return {}  # no store key, so no journal (issue_snapshot refuses such an instance)
    try:
        stored = publication_db.read_journals(publication_db.default_root(), uuid, "issue")
    except StoreError as exc:
        raise OutboxExecutionError(f"outbox store is unreadable: {exc}") from exc
    return {
        name: IssueJournal(identity, name, j.digest, j.action_count, j.actions)
        for (_, name), j in stored.items()
    }


def pr_snapshot(container: str, uuid: str | None) -> StoreSnapshot:
    """The instance's pending PR proposals (every kind) from the host outbox store.

    Read-only on ``broker.db``, like `issue_snapshot`: no Incus call. The row
    versions mark every proposal as store-held (`execute_delete` routes on it).
    """
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import publication_db, store_db

    if uuid is None:
        raise JournalError(f"{container}: instance uuid is unavailable")
    try:
        rows = store_db.read_proposals(publication_db.default_root(), uuid, "pr")
    except StoreError as exc:
        raise OutboxExecutionError(f"outbox store is unreadable: {exc}") from exc
    files: list[tuple[str, str]] = []
    for row in rows:
        files.append((row.name, row.manifest))
        files.extend(row.bodies.items())
    versions = tuple((row.name, row.revision) for row in rows)
    return StoreSnapshot("pr", tuple(sorted(files)), (), (), versions)


def pr_journals(uuid: str | None) -> dict[str, Journal]:
    """The instance's active PR publication journals by proposal name."""
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import publication_db

    if uuid is None:
        return {}
    try:
        stored = publication_db.read_journals(publication_db.default_root(), uuid, "pr")
    except StoreError as exc:
        raise OutboxExecutionError(f"outbox store is unreadable: {exc}") from exc
    return {name: journal for (_, name), journal in stored.items()}


def _source_uncertainty(
    proposals: tuple[ProposalView, ...], uuid: str | None
) -> tuple[ProposalView, ...]:
    """Show a store-held PR proposal whose publication outcome is unknown as uncertain."""
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import publication_db

    if uuid is None:
        return proposals
    try:
        journals = publication_db.read_journals(publication_db.default_root(), uuid, "pr")
    except StoreError as exc:
        raise OutboxExecutionError(f"outbox store is unreadable: {exc}") from exc
    result = []
    for view in proposals:
        journal = journals.get(("pr", view.id.name)) if view.id.kind == "pr" else None
        if journal is not None and journal.uncertain:
            detail = (
                "publication outcome uncertain; check GitHub, then rerun jb pr push"
                if view.state == "awaiting-push"
                else "publication outcome uncertain; check GitHub, then jb review resolve"
            )
            view = replace(
                view,
                state="uncertain",
                error="; ".join(v for v in (view.error, detail) if v),
                edit_block="recorded publication progress prevents editing",
            )
        result.append(view)
    return tuple(result)


if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.outbox.publication_db import Journal


def load_container(
    cfg: Config,
    incus: Incus,
    container: str,
    *,
    raise_errors: bool = False,
) -> ContainerView:
    """Inspect stores; explicit operations retain typed failures, overviews do not."""
    identity = None
    try:
        identity, uuid = _instance(incus, container)
        # Every proposal lives in the host outbox store (no `incus exec`).
        stores = (pr_snapshot(container, uuid), issue_snapshot(container, uuid))
        if _instance(incus, container) != (identity, uuid):
            raise OutboxChanged("container changed while reading; refresh required")
        # Canonical freshness matches publication, not optional PR display labels.
        proposals = _source_uncertainty(
            build_views(
                identity,
                stores,
                issue_journals=issue_journals(container, identity, uuid),
                pr_journals=pr_journals(uuid),
            ),
            uuid,
        )
        return ContainerView(identity, container, True, None, stores, proposals)
    except (IncusError, JournalError, OutboxExecutionError, OutboxChanged) as exc:
        if raise_errors:
            if isinstance(exc, (OutboxChanged, OutboxExecutionError)):
                raise
            raise OutboxExecutionError(str(exc)) from exc
        return ContainerView(identity, container, False, str(exc), (), ())


def execute_delete(
    cfg: Config,
    incus: Incus,
    container: str,
    plan: DeletePlan,
    *,
    lock_timeout: float | None = None,
) -> tuple[str, ...]:
    """Recheck deletion under its lock, optionally bounding the lock wait only."""
    try:
        identity, uuid = _instance(incus, container)
        # Issue proposals are guarded by their row version in the host store.
        if plan.proposal.kind == "issue":
            lock: AbstractContextManager[object] = nullcontext()
        else:
            manager = PrManagement()
            lock = (
                manager.lock(identity)
                if lock_timeout is None
                else manager.lock(identity, timeout=lock_timeout)
            )
        with lock:
            if _identity(incus, container) != identity:
                raise OutboxChanged("container changed while waiting; refresh required")
            fresh = load_container(cfg, incus, container)
            if not fresh.available or fresh.identity != identity:
                raise OutboxChanged(fresh.error or "container changed; refresh required")
            selected = next((v for v in fresh.proposals if v.id == plan.proposal), None)
            if selected is None or selected.revision != plan.expected_revision:
                raise OutboxChanged("proposal changed; refresh required")
            checked = plan_delete(fresh, plan.proposal, plan.selection)
            if checked != plan:
                raise OutboxChanged("deletion scope changed; refresh required")
            snapshot = next(s for s in fresh.stores if s.kind == plan.proposal.kind)
            if _identity(incus, container) != identity:
                raise OutboxChanged("container changed before mutation; refresh required")
            # The store archives the proposal's journal in the deleting transaction.
            return _delete_store_rows(container, uuid, plan, snapshot, kind=plan.proposal.kind)
    except (IncusError, JournalError) as exc:
        raise OutboxExecutionError(str(exc)) from exc


def _delete_store_rows(
    container: str,
    uuid: str | None,
    plan: DeletePlan,
    snapshot: StoreSnapshot,
    *,
    kind: Kind = "issue",
) -> tuple[str, ...]:
    """Delete or edit a store-held proposal, guarded by its row version."""
    from jailbee.db import state_dir
    from jailbee.orchestration.store import StoreError
    from jailbee.outbox import store_db

    if uuid is None:
        raise OutboxExecutionError(f"{container}: instance uuid is unavailable")
    version = dict(snapshot.versions).get(plan.proposal.name)
    if version is None:
        raise OutboxChanged("proposal changed; refresh required")
    try:
        store_db.host_store(state_dir() / "orchestration").edit_proposal(
            uuid,
            kind,
            plan.proposal.name,
            version,
            plan.new_text,
            archive_journal=plan.selection.archive_journal,
            now=time.time(),
        )
    except store_db.ProposalChangedError as exc:
        raise OutboxChanged("proposal changed; refresh required") from exc
    except (store_db.ProposalLockedError, StoreError) as exc:
        raise OutboxExecutionError(str(exc)) from exc
    return plan.delete_names
