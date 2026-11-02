"""The tree core: orchestration children ordered under their orchestrator, depth and name prefix."""

from __future__ import annotations

import dataclasses
import random

from jailbee.dashboard import fork_tree as ft
from jailbee.lifecycle import ContainerInfo
from jailbee.orchestration.view import ChildView


def _c(name: str, child_of: str | None = None, fork_of: str | None = None) -> ContainerInfo:
    c = ContainerInfo(
        name=name,
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="p",
        created_at=None,
    )
    return dataclasses.replace(
        c,
        fork_of=fork_of,
        child=ChildView(child_of, "working", (), False, ()) if child_of else None,
    )


def test_children_follow_their_orchestrator_in_input_order():
    rows = [
        _c("p-a"),
        _c("p-b"),
        _c("p-a2", child_of="p-a"),
        _c("p-b1", child_of="p-b"),
        _c("p-a1", child_of="p-a"),
    ]
    assert [c.name for c in ft.tree_order(rows)] == ["p-a", "p-a2", "p-a1", "p-b", "p-b1"]


def test_nested_children_nest_depth_first():
    rows = [_c("p-a"), _c("p-x", child_of="p-a1"), _c("p-a1", child_of="p-a"), _c("p-z")]
    assert [c.name for c in ft.tree_order(rows)] == ["p-a", "p-a1", "p-x", "p-z"]
    layout = ft.fork_layout(rows)
    assert dict(layout.depth) == {"p-a": 0, "p-a1": 1, "p-x": 2, "p-z": 0}
    assert layout.orphans == frozenset()


def test_missing_orchestrator_is_an_orphan_root_in_place():
    rows = [_c("p-a"), _c("p-o", child_of="p-gone"), _c("p-b")]
    assert [c.name for c in ft.tree_order(rows)] == ["p-a", "p-o", "p-b"]
    layout = ft.fork_layout(rows)
    assert layout.orphans == frozenset({"p-o"}) and layout.depth["p-o"] == 0


def test_self_and_cyclic_children_terminate_and_keep_every_row_once():
    rows = [
        _c("p-s", child_of="p-s"),
        _c("p-a", child_of="p-b"),
        _c("p-b", child_of="p-a"),
        _c("p-r"),
    ]
    out = [c.name for c in ft.tree_order(rows)]
    assert sorted(out) == sorted(c.name for c in rows) and len(out) == len(rows)
    assert ft.fork_layout(rows).orphans == frozenset()


def test_name_prefix_glyphs():
    rows = [
        _c("p-a"),
        _c("p-a1", child_of="p-a"),
        _c("p-a11", child_of="p-a1"),
        _c("p-o", child_of="p-gone"),
    ]
    layout = ft.fork_layout(rows)
    assert ft.name_prefix(layout, "p-a") == ""
    assert ft.name_prefix(layout, "p-a1") == "└ "
    assert ft.name_prefix(layout, "p-a11") == "  └ "
    assert ft.name_prefix(layout, "p-o") == "⑂ "
    assert ft.name_prefix(layout, "p-unknown") == ""


def test_random_forests_keep_every_row_once_and_children_after_parents():
    rng = random.Random(0)
    for _ in range(30):
        names = [f"p-{i}" for i in range(8)]
        rows = []
        for n in names:
            choice = rng.choice([*names, None, "p-missing"])
            rows.append(_c(n, child_of=choice))
        out = ft.tree_order(rows)
        order = [c.name for c in out]
        assert sorted(order) == sorted(names)
        depth = ft.fork_layout(rows).depth
        by_name = {c.name: c for c in rows}
        for i, name in enumerate(order):
            d = depth[name]
            if d < 1:
                continue
            child = by_name[name].child
            assert child is not None
            parent = child.orchestrator
            assert parent is not None
            p = order.index(parent)
            assert p < i
            assert all(depth[order[k]] >= d for k in range(p + 1, i))


def test_plain_fork_is_flat():
    rows = [_c("p-a"), _c("p-f", fork_of="p-a")]
    layout = ft.fork_layout(rows)
    assert [c.name for c in ft.tree_order(rows)] == ["p-a", "p-f"]
    assert ft.name_prefix(layout, "p-a") == ""
    assert ft.name_prefix(layout, "p-f") == ""


def test_spawned_fork_indents_under_orchestrator_not_fork_source():
    rows = [_c("p-o"), _c("p-s"), _c("p-c", child_of="p-o", fork_of="p-s")]
    assert [c.name for c in ft.tree_order(rows)] == ["p-o", "p-c", "p-s"]
    assert ft.name_prefix(ft.fork_layout(rows), "p-c") == ft.FORK_GLYPH


def test_child_of_missing_orchestrator_is_orphan():
    rows = [_c("p-c", child_of="p-gone")]
    assert ft.name_prefix(ft.fork_layout(rows), "p-c") == ft.ORPHAN_PREFIX
