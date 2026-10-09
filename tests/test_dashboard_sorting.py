"""The sorting core: within-group order, fallbacks, and the sort-state transitions."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from jailbee.dashboard import sorting as ds
from jailbee.dashboard.model import RepoGroup
from jailbee.lifecycle import ContainerInfo

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _c(name: str, *, age_h: float | None = 1, **over: object) -> ContainerInfo:
    c = ContainerInfo(
        name=name,
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        repo="p",
        created_at=None if age_h is None else NOW - timedelta(hours=age_h),
    )
    return dataclasses.replace(c, **over)  # type: ignore[arg-type]  # test helper


def _names(groups: list[RepoGroup]) -> list[list[str]]:
    return [[c.name for c in g.containers] for g in groups]


ENABLED = ("name", "state", "cpu", "created")


def test_default_order_is_restored_from_any_input_order():
    """Undated first, then newest first — even when the input is in another sort's order."""
    old, new, undated = _c("p-old", age_h=5), _c("p-new", age_h=1), _c("p-undated", age_h=None)
    group = RepoGroup("p", "/r", None, [old, new, undated])
    out = ds.sort_groups([group], ds.DEFAULT_SORT, ENABLED, now=NOW)
    assert _names(out) == [["p-undated", "p-new", "p-old"]]


def test_sorts_inside_each_group_and_never_reorders_groups():
    b = RepoGroup("b", "/b", None, [_c("b-1", state="Stopped"), _c("b-2")])
    a = RepoGroup("a", "/a", None, [_c("a-1", state="Stopped"), _c("a-2")])
    out = ds.sort_groups([b, a], ds.SortSpec("state", False), ENABLED, now=NOW)
    assert [g.prefix for g in out] == ["b", "a"]
    assert _names(out) == [["b-2", "b-1"], ["a-2", "a-1"]]


def test_missing_values_go_last_in_both_directions():
    group = RepoGroup(
        "p",
        "/r",
        None,
        [_c("p-none", cpu_percent=None), _c("p-low", cpu_percent=1), _c("p-hi", cpu_percent=90)],
    )
    up = ds.sort_groups([group], ds.SortSpec("cpu", False), ENABLED, now=NOW)
    down = ds.sort_groups([group], ds.SortSpec("cpu", True), ENABLED, now=NOW)
    assert _names(up) == [["p-low", "p-hi", "p-none"]]
    assert _names(down) == [["p-hi", "p-low", "p-none"]]


def test_ties_keep_the_default_order_in_both_directions():
    group = RepoGroup("p", "/r", None, [_c("p-old", age_h=5), _c("p-new", age_h=1)])
    for desc in (False, True):
        out = ds.sort_groups([group], ds.SortSpec("state", desc), ENABLED, now=NOW)
        assert _names(out) == [["p-new", "p-old"]]


def test_a_sort_column_that_is_not_enabled_sorts_by_default():
    group = RepoGroup(
        "p", "/r", None, [_c("p-old", age_h=5, ip="10.0.0.1"), _c("p-new", ip="10.0.0.9")]
    )
    out = ds.sort_groups([group], ds.SortSpec("ip", True), ENABLED, now=NOW)
    assert _names(out) == [["p-new", "p-old"]]
    assert ds.active_sort(ds.SortSpec("ip", True), ENABLED, now=NOW) == ds.DEFAULT_SORT
    assert ds.active_sort(ds.SortSpec("nope", True), ("nope",), now=NOW) == ds.DEFAULT_SORT


def test_input_groups_are_not_mutated():
    containers = [_c("p-a", state="Stopped"), _c("p-b")]
    group = RepoGroup("p", "/r", None, containers)
    ds.sort_groups([group], ds.SortSpec("state", False), ENABLED, now=NOW)
    assert group.containers is containers
    assert [c.name for c in containers] == ["p-a", "p-b"]


def test_click_a_new_column_uses_its_first_direction_and_a_second_click_flips():
    first = ds.click_sort(ds.DEFAULT_SORT, "created", now=NOW)
    assert first == ds.SortSpec("created", True)
    assert ds.click_sort(first, "created", now=NOW) == ds.SortSpec("created", False)
    assert ds.click_sort(first, "name", now=NOW) == ds.SortSpec("name", False)


def test_cycle_walks_shown_columns_with_a_default_stop():
    shown = ("name", "state", "cpu")
    s = ds.DEFAULT_SORT
    seen = []
    for _ in range(4):
        s = ds.cycle_sort(s, shown, 1, now=NOW)
        seen.append(s.field)
    assert seen == ["name", "state", "cpu", None]
    assert ds.cycle_sort(ds.DEFAULT_SORT, shown, -1, now=NOW).field == "cpu"
    assert ds.cycle_sort(ds.SortSpec("cpu"), shown, 1, now=NOW) == ds.DEFAULT_SORT
    assert ds.cycle_sort(ds.DEFAULT_SORT, shown, 1, now=NOW) == ds.SortSpec("name", False)
    assert ds.cycle_sort(ds.SortSpec("name"), shown, 2, now=NOW) == ds.SortSpec("cpu", True)


def test_cycle_from_a_column_no_longer_shown_starts_over():
    assert ds.cycle_sort(ds.SortSpec("ip"), ("name", "state"), 1, now=NOW).field == "name"


def test_invert_flips_only_a_real_sort():
    assert ds.invert_sort(ds.SortSpec("cpu", True)) == ds.SortSpec("cpu", False)
    assert ds.invert_sort(ds.DEFAULT_SORT) == ds.DEFAULT_SORT


def test_mark_and_notice():
    assert ds.sort_mark(ds.SortSpec("cpu", True)) == "▼"
    assert ds.sort_mark(ds.SortSpec("cpu", False)) == "▲"
    assert ds.sort_notice(ds.DEFAULT_SORT) == "Sorted: newest first"
    assert ds.sort_notice(ds.SortSpec("cpu", True)) == "Sorted by cpu ▼"
