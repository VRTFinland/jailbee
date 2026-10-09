"""The terminal table's header: the sort mark and the clickable column titles."""

from __future__ import annotations

from rich.style import Style

from jailbee.dashboard import hit as dhit
from jailbee.dashboard.columns import optimize_column_widths
from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.sorting import DEFAULT_SORT, SortSpec
from jailbee.dashboard.tui import fleet
from tests.dashboard_fixtures import NOW, ci

ENABLED = ("name", "state", "created")


def _group() -> RepoGroup:
    return RepoGroup(
        "alpha", "/repos/alpha", None, [ci("alpha-one", "alpha"), ci("alpha-two", "alpha")]
    )


def _model(sort=DEFAULT_SORT, column_widths=None, width=120):  # type: ignore[no-untyped-def]
    return fleet.table_model(
        [_group()],
        now=NOW,
        enabled=ENABLED,
        folded=frozenset(),
        column_widths=column_widths,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=width,
        sort=sort,
    )


def _header(sort=DEFAULT_SORT, column_widths=None) -> str:  # type: ignore[no-untyped-def]
    return fleet.header_line(_model(sort, column_widths).geometry).plain


def test_the_sort_column_carries_a_mark():
    assert "ST ▲" in _header(SortSpec("state", False))
    assert "ST ▼" in _header(SortSpec("state", True))
    assert "▲" not in _header() and "▼" not in _header()


def test_no_mark_when_the_sort_column_is_not_shown():
    assert "▲" not in _header(SortSpec("ip", False))


def test_the_mark_survives_optimized_widths():
    widths = optimize_column_widths([_group()], now=NOW, enabled=ENABLED)
    assert "ST ▼" in _header(SortSpec("state", True), column_widths=widths)


def test_each_header_cell_is_a_sort_target():
    line = fleet.header_line(_model().geometry)
    targets = set()
    for span in line.spans:
        style = span.style if isinstance(span.style, Style) else None
        if style is not None and style.meta:
            hit = dhit.Hit.of(style.meta)
            if hit is not None:
                targets.add((hit.kind, hit.args))
    assert {("sort", ("name",)), ("sort", ("state",))} <= targets
