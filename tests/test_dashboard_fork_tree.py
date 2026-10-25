"""The fork-tree core: forks ordered under their parent, depth and name prefix."""

from __future__ import annotations

import dataclasses
import random

from jailbee.dashboard import fork_tree as ft
from jailbee.lifecycle import ContainerInfo


def _c(name: str, fork_of: str | None = None) -> ContainerInfo:
    c = ContainerInfo(
        name=name,
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="p",
        created_at=None,
    )
    return dataclasses.replace(c, fork_of=fork_of)


def test_forks_follow_their_parent_in_input_order():
    rows = [
        _c("p-a"),
        _c("p-b"),
        _c("p-a2", fork_of="p-a"),
        _c("p-b1", fork_of="p-b"),
        _c("p-a1", fork_of="p-a"),
    ]
    assert [c.name for c in ft.tree_order(rows)] == ["p-a", "p-a2", "p-a1", "p-b", "p-b1"]


def test_nested_forks_nest_depth_first():
    rows = [_c("p-a"), _c("p-x", fork_of="p-a1"), _c("p-a1", fork_of="p-a"), _c("p-z")]
    assert [c.name for c in ft.tree_order(rows)] == ["p-a", "p-a1", "p-x", "p-z"]
    layout = ft.fork_layout(rows)
    assert dict(layout.depth) == {"p-a": 0, "p-a1": 1, "p-x": 2, "p-z": 0}
    assert layout.orphans == frozenset()


def test_fork_of_a_missing_parent_is_an_orphan_root_in_place():
    rows = [_c("p-a"), _c("p-o", fork_of="p-gone"), _c("p-b")]
    assert [c.name for c in ft.tree_order(rows)] == ["p-a", "p-o", "p-b"]
    layout = ft.fork_layout(rows)
    assert layout.orphans == frozenset({"p-o"}) and layout.depth["p-o"] == 0


def test_self_and_cyclic_fork_of_terminate_and_keep_every_row_once():
    rows = [
        _c("p-s", fork_of="p-s"),
        _c("p-a", fork_of="p-b"),
        _c("p-b", fork_of="p-a"),
        _c("p-r"),
    ]
    out = [c.name for c in ft.tree_order(rows)]
    assert sorted(out) == sorted(c.name for c in rows) and len(out) == len(rows)
    assert ft.fork_layout(rows).orphans == frozenset()


def test_name_prefix_glyphs():
    rows = [
        _c("p-a"),
        _c("p-a1", fork_of="p-a"),
        _c("p-a11", fork_of="p-a1"),
        _c("p-o", fork_of="p-gone"),
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
            rows.append(_c(n, fork_of=choice))
        out = ft.tree_order(rows)
        order = [c.name for c in out]
        assert sorted(order) == sorted(names)
        depth = ft.fork_layout(rows).depth
        by_name = {c.name: c for c in rows}
        for i, name in enumerate(order):
            d = depth[name]
            if d < 1:
                continue
            parent = by_name[name].fork_of
            assert parent is not None
            p = order.index(parent)
            assert p < i
            assert all(depth[order[k]] >= d for k in range(p + 1, i))
