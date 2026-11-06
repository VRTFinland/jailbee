"""Read-only view model of orchestration state for listings and dashboards.

Parses the grant / child labels and the broker queue into small frozen
records; a corrupt label or file reads as absent, never as an exception.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jailbee.db import state_dir
from jailbee.orchestration import labels
from jailbee.orchestration.grant import ChildRights, Grant, GrantError
from jailbee.orchestration.runner import parse_runner_label
from jailbee.orchestration.state import QUEUE_FILE
from jailbee.orchestration.store import read_items

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from rich.table import Table

    from jailbee.config import Config
    from jailbee.incus import Incus
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
    default_runner: str | None = None
    # publish rights (`PublishRights`), flattened; the defaults are "none granted"
    publish_issues: tuple[str, ...] = ()
    publish_foreign_repos: tuple[str, ...] = ()
    publish_foreign_actions: tuple[str, ...] = ()
    publish_prs: bool = False
    publish_pr_reviews: bool = False
    publish_rate: int = 30


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
        g.default_runner.describe() if g.default_runner else None,
        publish_issues=tuple(g.publish.issues),
        publish_foreign_repos=g.publish.foreign_issues.repos,
        publish_foreign_actions=tuple(g.publish.foreign_issues.actions),
        publish_prs=g.publish.pull_requests,
        publish_pr_reviews=g.publish.pr_reviews,
        publish_rate=g.publish.max_actions_per_hour,
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


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def messages_view(
    root: Path,
    *,
    full: str | None,
    include_acked: bool,
    short: Callable[[str], str] | None = None,
) -> list[dict[str, object]]:
    """Rows of the broker message store, read-only: unread first, then acked history.

    Unread rows come in ``seq`` order; with ``include_acked`` the acked rows
    follow, oldest acknowledgement first (newest last). A missing store is no rows.
    """
    items = read_items(root, full)
    unread = [i for i in items if i.acked_at is None]
    acked = sorted((i for i in items if i.acked_at is not None), key=lambda i: (i.acked_at, i.seq))
    rows: list[dict[str, object]] = []
    for i in unread + (acked if include_acked else []):
        rows.append(
            {
                "container": short(i.recipient_full) if short else i.recipient_full,
                "id": i.id,
                "type": i.type,
                "kind": i.kind_or_state,
                "from": i.from_name,
                "ref": i.ref,
                "created_at": _iso(i.created_at),
                "acked_at": _iso(i.acked_at),
            }
        )
    return rows


_MESSAGE_COLUMNS = (
    ("CONTAINER", "container"),
    ("ID", "id"),
    ("TYPE", "type"),
    ("KIND", "kind"),
    ("FROM", "from"),
    ("REF", "ref"),
    ("CREATED", "created_at"),
    ("ACKED", "acked_at"),
)


def messages_target(cfg: Config, incus: Callable[[], Incus], name: str | None) -> str | None:
    """The full name `jb orchestrate messages NAME` filters on; None for all.

    Never prompts: a name that resolves to no live container (the store keeps
    a gone container's rows) is taken as written, prefixed when it is short.
    """
    from jailbee.lifecycle import resolve_container_name

    if name is None:
        return None
    try:
        return resolve_container_name(cfg, incus(), name)
    except ValueError:
        prefix = f"{cfg.container_prefix}-"
        return name if name.startswith(prefix) else prefix + name


def messages_table(rows: Sequence[Mapping[str, object]]) -> Table:
    """`messages_view` rows as the table `jb orchestrate messages` prints."""
    from rich.table import Table

    table = Table(show_edge=False, pad_edge=False)
    for header, _ in _MESSAGE_COLUMNS:
        table.add_column(header)
    for r in rows:
        table.add_row(*(str(r[k]) if r[k] is not None else "-" for _, k in _MESSAGE_COLUMNS))
    return table
