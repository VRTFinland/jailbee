"""Local, revision-checked outbox deletion coordinated with publication."""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.incus import IncusError
from jailbee.outbox.delete import DeletePlan, plan_delete
from jailbee.outbox.inspect import build_views
from jailbee.outbox.io import READ_TIMEOUT, PrManagement, mutate_store, read_store
from jailbee.outbox.models import ContainerView, Kind, OutboxChanged, OutboxExecutionError
from jailbee.outbox_io import ContainerIdentity, JournalError, JournalStore, journal_key


def _identity(incus: Incus, container: str) -> ContainerIdentity:
    # The shared legacy identity helper has no timeout parameter. Keep the same
    # raw/nonzero timestamp contract while bounding this interactive service.
    # --fast: created_at is a top-level key; every instance's state is not needed.
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
    return ContainerIdentity(container, created)


if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus


def load_container(
    cfg: Config,
    incus: Incus,
    container: str,
    *,
    journal_store: JournalStore,
    raise_errors: bool = False,
) -> ContainerView:
    """Inspect stores; explicit operations retain typed failures, overviews do not."""
    identity = None
    try:
        identity = _identity(incus, container)
        kinds: tuple[Kind, ...] = ("pr", "issue")
        stores = tuple(
            read_store(incus, container, kind, uid=cfg.container_user.uid) for kind in kinds
        )
        if _identity(incus, container) != identity:
            raise OutboxChanged("container changed while reading; refresh required")
        # Canonical freshness matches publication, not optional PR display labels.
        proposals = build_views(identity, stores, journal_store=journal_store)
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
    journal_store: JournalStore,
    lock_timeout: float | None = None,
) -> tuple[str, ...]:
    """Recheck deletion under its lock, optionally bounding the lock wait only."""
    try:
        identity = _identity(incus, container)
        key = journal_key(identity, plan.proposal.name)
        # Preserve the legacy no-keyword call for blocking callers and adapters.
        if plan.proposal.kind == "issue":
            lock = (
                journal_store.lock(key)
                if lock_timeout is None
                else journal_store.lock(key, timeout=lock_timeout)
            )
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
            fresh = load_container(cfg, incus, container, journal_store=journal_store)
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
            removed = mutate_store(
                incus,
                container,
                plan.proposal.kind,
                uid=cfg.container_user.uid,
                expected=snapshot.as_dict(),
                new_manifest=(plan.proposal.name, plan.new_text)
                if plan.new_text is not None
                else None,
                delete_names=plan.delete_names,
                forbidden_progress=f"{plan.proposal.name}.progress.json"
                if plan.proposal.kind == "pr"
                else None,
                rejected_names=snapshot.rejected,
            )
            if removed != plan.delete_names:
                raise OutboxExecutionError("incomplete deletion; journal retained")
            if plan.selection.archive_journal:
                journal = journal_store.load(key)
                if journal is not None and journal.actions:
                    journal_store.archive(key)
            return removed
    except (IncusError, JournalError) as exc:
        raise OutboxExecutionError(str(exc)) from exc
