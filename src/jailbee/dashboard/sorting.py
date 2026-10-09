"""Row order inside each repo group: which column, which way, and the transitions.

Frontend-agnostic and pure. Both dashboards sort with :func:`sort_groups` on
every refresh; the terminal's keys and header clicks and the Qt header clicks
move the state with :func:`click_sort`, :func:`cycle_sort` and
:func:`invert_sort`. Groups are never reordered, only the containers inside
them.

The default order — undated containers first, then newest first, as
``lifecycle.list_containers`` returns them — is applied explicitly as every
sort's tie-breaker, so returning to the default after a sort restores it
exactly instead of keeping the previous sort's order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from jailbee import table_format
from jailbee.dashboard.model import RepoGroup
from jailbee.lifecycle import ContainerInfo, ls_field_specs


@dataclass(frozen=True)
class SortSpec:
    """The sort column (None: the default order) and its direction."""

    field: str | None = None
    desc: bool = False


DEFAULT_SORT = SortSpec()


def _specs(now: datetime) -> dict[str, table_format.FieldSpec[ContainerInfo]]:
    return {spec.name: spec for spec in ls_field_specs(now=now) if spec.sort is not None}


def default_order_key(c: ContainerInfo) -> tuple[bool, float]:
    """Undated first, then newest first."""
    if c.created_at is None:
        return (False, 0.0)
    return (True, -c.created_at.timestamp())


def active_sort(sort: SortSpec, enabled: Sequence[str], *, now: datetime) -> SortSpec:
    """``sort`` if its column is enabled and sortable, else the default order."""
    if sort.field is None or sort.field not in enabled or sort.field not in _specs(now):
        return DEFAULT_SORT
    return sort


def sort_groups(
    groups: Sequence[RepoGroup], sort: SortSpec, enabled: Sequence[str], *, now: datetime
) -> list[RepoGroup]:
    """Each group's containers in ``sort`` order; groups keep their order.

    A row whose key is None goes last in both directions. Never mutates the
    input: the state client's snapshot is shared.
    """
    sort = active_sort(sort, enabled, now=now)
    key = None if sort.field is None else _specs(now)[sort.field].sort
    out: list[RepoGroup] = []
    for group in groups:
        rows = sorted(group.containers, key=default_order_key)
        if key is not None:
            keyed = [(key(c), c) for c in rows]
            present = [(k, c) for k, c in keyed if k is not None]
            # Python's sort is stable under reverse=True too: ties keep the default order.
            present.sort(key=lambda kc: kc[0], reverse=sort.desc)
            rows = [c for _, c in present] + [c for k, c in keyed if k is None]
        out.append(replace(group, containers=rows))
    return out


def click_sort(sort: SortSpec, field: str, *, now: datetime) -> SortSpec:
    """A click on ``field``'s header: flip it if it is the sort column, else sort by it."""
    if sort.field == field:
        return invert_sort(sort)
    spec = _specs(now).get(field)
    return DEFAULT_SORT if spec is None else SortSpec(field, spec.sort_desc_first)


def cycle_sort(sort: SortSpec, shown: Sequence[str], step: int, *, now: datetime) -> SortSpec:
    """Move the sort ``step`` stops through the shown sortable columns and the default stop."""
    specs = _specs(now)
    stops: list[str | None] = [None, *(name for name in shown if name in specs)]
    index = stops.index(sort.field) if sort.field in stops else 0
    field = stops[(index + step) % len(stops)]
    return DEFAULT_SORT if field is None else SortSpec(field, specs[field].sort_desc_first)


def invert_sort(sort: SortSpec) -> SortSpec:
    """The other direction; the default order has none."""
    return sort if sort.field is None else SortSpec(sort.field, not sort.desc)


def sort_mark(sort: SortSpec) -> str:
    return "▼" if sort.desc else "▲"


def sort_notice(sort: SortSpec) -> str:
    """One line saying what the rows are sorted by (the column may be scrolled away)."""
    if sort.field is None:
        return "Sorted: newest first"
    return f"Sorted by {sort.field} {sort_mark(sort)}"
