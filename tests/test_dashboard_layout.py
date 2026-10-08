"""The frame's height budget, top to bottom, and its Rich integration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from rich.console import Console
from rich.text import Text

from jailbee.dashboard.overlays import MIN_LIST_ROWS
from jailbee.dashboard.tui import frame as tframe


def _bottom(*, details: bool = False, overlay_lines: int = 0) -> Callable[[int, int | None], int]:
    def lines(list_rows: int, details_rows: int | None) -> int:
        shown_details = details_rows + 2 if details and details_rows is not None else 0
        shown_overlay = min(overlay_lines, list_rows + 2) if overlay_lines else 0
        return max(shown_details, shown_overlay)

    return lines


def _layout(
    *,
    height: int = 23,
    table_lines: int = 10,
    notice_lines: int = 0,
    hint_lines: int = 0,
    has_bottom: bool = False,
    details_cap: int = 8,
    details_fit: bool = True,
    bottom_lines: Callable[[int, int | None], int] | None = None,
):
    from jailbee.dashboard.tui.layout import frame_layout

    return frame_layout(
        height=height,
        table_lines=table_lines,
        notice_lines=notice_lines,
        hint_lines=hint_lines,
        has_bottom=has_bottom,
        details_cap=details_cap,
        details_fit=details_fit,
        bottom_lines=_bottom() if bottom_lines is None else bottom_lines,
    )


def test_no_bottom_gives_the_table_everything_it_needs():
    from jailbee.dashboard.tui.layout import FrameLayout

    assert _layout() == FrameLayout(10, 0, False, None, MIN_LIST_ROWS, 0, 0)
    assert _layout(table_lines=40).table_rows == 23


def test_details_take_their_cap_under_a_short_table():
    layout = _layout(has_bottom=True, bottom_lines=_bottom(details=True))
    assert layout.details_rows == 8
    assert layout.gap and layout.bottom_rows == 10
    assert layout.table_rows == 10


def test_a_long_table_keeps_six_lines_above_the_details():
    layout = _layout(height=22, table_lines=40, has_bottom=True, bottom_lines=_bottom(details=True))
    assert layout.table_rows == 11
    assert layout.table_rows + int(layout.gap) + layout.bottom_rows == 22


def test_a_panel_with_fewer_than_two_rows_is_dropped():
    layout = _layout(height=9, table_lines=40, has_bottom=True, bottom_lines=_bottom(details=True))
    assert layout.details_rows is None
    assert layout.bottom_rows == 0 and not layout.gap
    assert layout.table_rows == 9


def test_details_that_do_not_fit_beside_a_menu_are_left_out():
    layout = _layout(
        has_bottom=True, details_fit=False, bottom_lines=_bottom(details=True, overlay_lines=6)
    )
    assert layout.details_rows is None and layout.bottom_rows == 6


def test_a_long_menu_is_windowed_but_never_below_min_list_rows():
    layout = _layout(height=20, table_lines=40, has_bottom=True, bottom_lines=_bottom(overlay_lines=60))
    assert layout.list_rows == 11
    tiny = _layout(height=8, table_lines=40, has_bottom=True, bottom_lines=_bottom(overlay_lines=60))
    assert tiny.list_rows == MIN_LIST_ROWS


def test_notice_and_hint_lines_come_off_the_top_budget():
    layout = _layout(table_lines=40, notice_lines=2, hint_lines=1)
    assert layout.table_rows == 20 and layout.notice_rows == 2


@pytest.mark.parametrize("height", [4, 6, 8])
def test_even_the_minimum_is_cropped_from_the_top_to_fit(height):
    layout = _layout(
        height=height, table_lines=40, hint_lines=1, has_bottom=True,
        bottom_lines=_bottom(overlay_lines=60),
    )
    total = layout.table_rows + layout.notice_rows + int(layout.gap) + layout.bottom_rows - layout.crop_top + 1
    assert total == height
    assert layout.table_rows == max(0, height - (MIN_LIST_ROWS + 2 + 2))
    assert layout.gap == (height >= MIN_LIST_ROWS + 2 + 2)
    assert layout.crop_top == max(0, MIN_LIST_ROWS + 2 + 1 - height)


def test_notice_loses_rows_before_the_bottom_is_cropped():
    layout = _layout(height=2, notice_lines=5)
    assert layout.table_rows == 0 and layout.notice_rows == 2
    assert layout.crop_top == 0


@pytest.mark.parametrize("height, expected", [(2, ["middle", "remedy"]), (0, [])])
def test_rich_frame_preserves_the_notice_suffix_and_hides_a_zero_row_notice(height, expected):
    console = Console(width=80)
    sections = tframe._RepoSections(
        [], datetime(2026, 10, 8, tzinfo=UTC), None, frozenset(), None, None, 0, False, None
    )
    body = tframe._FrameBody(sections, Text("verdict\nmiddle\nremedy"), None, max_height=height)
    lines = console.render_lines(body, console.options, pad=False)
    assert ["".join(segment.text for segment in line) for line in lines] == expected


def test_layout_constants_preserve_the_frame_export():
    from jailbee.dashboard.tui.layout import MIN_DETAILS_ROWS, MIN_TABLE_ROWS, OVERLAY_BORDER_ROWS

    assert (MIN_TABLE_ROWS, OVERLAY_BORDER_ROWS, MIN_DETAILS_ROWS) == (5, 2, 2)
    assert tframe.MIN_TABLE_ROWS == MIN_TABLE_ROWS
