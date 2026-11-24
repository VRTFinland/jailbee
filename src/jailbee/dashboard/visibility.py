"""Pure repository-group visibility filtering shared by dashboard front-ends."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from jailbee.dashboard.format import RECENT_IDLE
from jailbee.lifecycle import agent_compact_is_active

if TYPE_CHECKING:
    from datetime import datetime

    from jailbee.dashboard.model import RepoGroup
    from jailbee.lifecycle import ContainerInfo

ContainerFilter = Literal["all", "running", "active"]


def container_matches_filter(
    container: ContainerInfo, mode: ContainerFilter, *, now: datetime
) -> bool:
    """Match state or the same coloured agent marks the dashboard displays."""
    if mode == "all":
        return True
    if mode == "running":
        return container.state == "Running"
    return agent_compact_is_active(container.agent_status, now, recent_idle=RECENT_IDLE)


@dataclass(frozen=True)
class FilteredGroups:
    groups: list[RepoGroup]
    context: frozenset[str]
    hidden: int


def filter_repo_groups(
    groups: list[RepoGroup], mode: ContainerFilter, *, now: datetime
) -> FilteredGroups:
    """Keep matches and their listed ancestors; count only rows actually hidden.

    Parents in another repository remain there, as in the unfiltered tree.
    Missing parents are never fabricated and cycles terminate at an already kept row.
    """
    if mode == "all":
        return FilteredGroups(groups, frozenset(), 0)
    rows = {c.name: c for g in groups for c in g.containers}
    matches = {name for name, c in rows.items() if container_matches_filter(c, mode, now=now)}
    kept = set(matches)
    for name in matches:
        row = rows[name]
        while row.child is not None:
            parent = row.child.orchestrator
            if parent not in rows or parent in kept:
                break
            kept.add(parent)
            row = rows[parent]
    return FilteredGroups(
        [
            replace(g, containers=[c for c in g.containers if c.name in kept])
            for g in groups
            if any(c.name in kept for c in g.containers)
        ],
        frozenset(kept - matches),
        len(rows) - len(kept),
    )


def visible_repo_groups(
    groups: list[RepoGroup],
    *,
    show_empty_repos: bool,
    hidden_repos: frozenset[str],
) -> list[RepoGroup]:
    """Return visible groups without modifying the gathered snapshot."""
    return [
        group
        for group in groups
        if group.prefix not in hidden_repos and (show_empty_repos or group.containers)
    ]
