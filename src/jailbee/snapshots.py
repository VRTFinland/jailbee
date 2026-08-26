"""Snapshot lifecycle operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jailbee.config import Config
from jailbee.incus import Incus


def _now() -> datetime:
    """Indirected for test mocking."""
    return datetime.now(UTC)


def snapshot_default_tag() -> str:
    """Return a sortable default snapshot tag (e.g. snap-2026-05-05-120000Z)."""
    return _now().strftime("snap-%Y-%m-%d-%H%M%SZ")


def create_snapshot(incus: Incus, container: str, tag: str | None) -> str:
    """Create a snapshot. Returns the tag used."""
    actual = tag if tag else snapshot_default_tag()
    incus.snapshot_create(container, actual)
    return actual


def restore_snapshot(cfg: Config, incus: Incus, container: str, tag: str) -> None:
    """Restore a snapshot, then rebuild the container's egress ACL.

    The `user.jailbee.egress_extra` label travels inside the snapshot but the
    ACL it drives does not, so a restore can leave the two disagreeing. The
    label is the source of truth; this makes Incus match it again.
    """
    from sqlmodel import Session

    from jailbee import egress_scope
    from jailbee.db import get_engine
    from jailbee.lifecycle import current_network_mode

    incus.snapshot_restore(container, tag)
    mode = current_network_mode(cfg, incus, container) or "strict"
    with Session(get_engine()) as session:
        egress_scope.apply_container_acl(cfg, session, incus, container, mode=mode)


def delete_snapshot(incus: Incus, container: str, tag: str) -> None:
    incus.snapshot_delete(container, tag)


def list_snapshots(incus: Incus, container: str) -> list[dict[str, Any]]:
    return incus.snapshot_list(container)
