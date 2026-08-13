"""Snapshot lifecycle operations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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


def restore_snapshot(incus: Incus, container: str, tag: str) -> None:
    incus.snapshot_restore(container, tag)


def delete_snapshot(incus: Incus, container: str, tag: str) -> None:
    incus.snapshot_delete(container, tag)


def list_snapshots(incus: Incus, container: str) -> list[dict[str, Any]]:
    return incus.snapshot_list(container)
