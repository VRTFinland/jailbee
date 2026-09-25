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
    from jailbee import egress_scope
    from jailbee.lifecycle import current_network_mode
    from jailbee.network_generation import generation_of

    incus.snapshot_restore(container, tag)
    mode = current_network_mode(cfg, incus, container) or "strict"
    raw = next((item for item in incus.list_containers() if item.get("name") == container), {})
    if generation_of(cfg, raw) == "work":
        from jailbee.work_acl import apply_work_container_acl, reconcile_work_acl
        from jailbee.work_network import work_network_lock

        with work_network_lock():
            apply_work_container_acl(cfg, incus, container)
            reconcile_work_acl(cfg, incus)
    else:
        egress_scope.apply_container_acl(cfg, incus, container, mode=mode)


def delete_snapshot(incus: Incus, container: str, tag: str) -> None:
    incus.snapshot_delete(container, tag)


def list_snapshots(incus: Incus, container: str) -> list[dict[str, Any]]:
    return incus.snapshot_list(container)
