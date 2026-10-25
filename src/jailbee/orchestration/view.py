"""Read-only view model of orchestration state for listings and dashboards.

Parses the grant / child labels and the broker queue into small frozen
records; a corrupt label or file reads as absent, never as an exception.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jailbee.db import state_dir
from jailbee.orchestration import labels
from jailbee.orchestration.grant import ChildRights, Grant, GrantError
from jailbee.orchestration.runner import parse_runner_label
from jailbee.orchestration.state import QUEUE_FILE

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from jailbee.lifecycle import ContainerInfo

_STATES = frozenset({labels.STATE_WORKING, labels.STATE_DONE, labels.STATE_IMPORTED})


@dataclass(frozen=True)
class GrantView:
    """An orchestrator's grant, flattened for display."""

    limit: int
    operations: tuple[str, ...]
    mounts: tuple[str, ...]
    loose: bool
    egress: tuple[str, ...]


@dataclass(frozen=True)
class ChildView:
    """A spawned child: who spawned it, its state and the rights it was given."""

    orchestrator: str  # full container name
    state: str
    mounts: tuple[str, ...]
    loose: bool
    egress: tuple[str, ...]
    runner: str | None = None  # `Runner.describe()`; None means the default


def parse_grant_view(raw: object) -> GrantView | None:
    """Return the grant in a `GRANT` label value, or None when absent or corrupt."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        g = Grant.from_label(raw)
    except GrantError:
        return None
    return GrantView(
        g.max_running_children,
        tuple(str(op) for op in g.operations),
        g.grantable.mounts,
        g.grantable.loose,
        g.grantable.egress,
    )


def parse_child_view(
    spawned_by: object, state: object, rights_raw: object, runner_raw: object = None
) -> ChildView | None:
    """Return the child view from its labels; None when it has no spawner.

    Corrupt rights keep the child with no rights; a missing or unknown state
    reads as `working`.
    """
    if not isinstance(spawned_by, str) or not spawned_by:
        return None
    st = state if isinstance(state, str) and state in _STATES else labels.STATE_WORKING
    mounts: tuple[str, ...] = ()
    loose = False
    egress: tuple[str, ...] = ()
    if isinstance(rights_raw, str) and rights_raw:
        try:
            r = ChildRights.from_label(rights_raw)
        except GrantError:
            pass
        else:
            mounts, loose, egress = r.mounts, r.loose, r.egress
    runner = parse_runner_label(runner_raw)
    return ChildView(
        spawned_by, st, mounts, loose, egress, runner.describe() if runner is not None else None
    )


def queued_counts(path: Path | None = None) -> dict[str, int]:
    """Return queued spawn requests per orchestrator; unreadable file gives {}."""
    p = path if path is not None else state_dir() / "orchestration" / QUEUE_FILE
    try:
        queues = json.loads(p.read_text(encoding="utf-8"))["queues"]
        return {str(k): len(v) for k, v in queues.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def annotate(containers: Sequence[ContainerInfo], queued: Mapping[str, int]) -> None:
    """Set the derived orchestration fields on `containers` in place."""
    from jailbee import background

    running = {c.name for c in containers if c.state == "Running"}
    counts: dict[str, int] = {}
    for c in containers:
        if c.child is None:
            continue
        creating = c.job_kind == background.JOB_CREATE and c.job_phase is not None
        if c.state == "Running" or creating:
            counts[c.child.orchestrator] = counts.get(c.child.orchestrator, 0) + 1
    for c in containers:
        if c.grant is not None:
            c.running_children = counts.get(c.name, 0)
            c.queued_children = queued.get(c.name, 0)
        if c.child is not None:
            c.orchestrator_running = c.child.orchestrator in running
