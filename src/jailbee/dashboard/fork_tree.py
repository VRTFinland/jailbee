"""Orchestration children under their orchestrator, frontend-agnostically.

Rows are matched by ``ContainerInfo.child.orchestrator`` and full name.
Roots and siblings keep input order; traversal is depth-first. A missing
orchestrator marks an orphan; self-references and cycles remain unmarked roots.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jailbee.lifecycle import ContainerInfo

ORPHAN_PREFIX = "⑂ "
FORK_GLYPH = "└ "
INDENT = "  "


@dataclass(frozen=True)
class ForkLayout:
    depth: Mapping[str, int]  # full name -> 0 for roots, n for an n-deep child
    orphans: frozenset[str]  # full names of children whose orchestrator is not in the rows


def _chain_cycles(start: str, parent_of: Mapping[str, str | None]) -> bool:
    """True when walking the parent chain from ``start`` revisits a row."""
    seen = {start}
    cur = parent_of.get(start)
    while cur is not None and cur in parent_of:
        if cur in seen:
            return True
        seen.add(cur)
        cur = parent_of[cur]
    return False


def _walk(rows: Sequence[ContainerInfo]) -> tuple[list[ContainerInfo], dict[str, int], set[str]]:
    parent_of = {c.name: (c.child.orchestrator if c.child is not None else None) for c in rows}
    children: dict[str, list[ContainerInfo]] = {}
    roots: list[ContainerInfo] = []
    orphans: set[str] = set()
    for c in rows:
        parent = parent_of[c.name]
        if parent is not None and parent not in parent_of:
            orphans.add(c.name)
            roots.append(c)
        elif parent is None or _chain_cycles(c.name, parent_of):
            roots.append(c)
        else:
            children.setdefault(parent, []).append(c)

    order: list[ContainerInfo] = []
    depth: dict[str, int] = {}
    stack = [(c, 0) for c in reversed(roots)]
    while stack:
        c, d = stack.pop()
        if c.name in depth:
            continue
        depth[c.name] = d
        order.append(c)
        stack.extend((k, d + 1) for k in reversed(children.get(c.name, [])))
    return order, depth, orphans


def fork_layout(rows: Sequence[ContainerInfo]) -> ForkLayout:
    _, depth, orphans = _walk(rows)
    return ForkLayout(depth=depth, orphans=frozenset(orphans))


def tree_order(rows: Sequence[ContainerInfo]) -> list[ContainerInfo]:
    return _walk(rows)[0]


def name_prefix(layout: ForkLayout, name: str) -> str:
    """The glyphs shown before a container name: indent plus a child mark."""
    if name in layout.orphans:
        return ORPHAN_PREFIX
    d = layout.depth.get(name, 0)
    if d < 1:
        return ""
    return INDENT * (d - 1) + FORK_GLYPH
