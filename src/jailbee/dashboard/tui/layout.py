"""The terminal dashboard's height budget, Textual- and Rich-free.

The table keeps five rows plus its header before the bottom area may grow.
Lists retain their minimum window; details with fewer than two content rows
are omitted. Overflow loses the table, notice and gap first, then the top of
the bottom area, preserving the hint and the frame's bottom border.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from jailbee.dashboard.overlays import MIN_LIST_ROWS

MIN_TABLE_ROWS = 5
FRAME_INSET_COLS = 4  # the dashboard frame's border and padding, either side
BOX_INSET_COLS = 4  # an overlay box's border and padding, either side
OVERLAY_BORDER_ROWS = 2
MIN_DETAILS_ROWS = 2


@dataclass(frozen=True)
class FrameLayout:
    table_rows: int
    """Table lines, including the header; zero hides it."""
    notice_rows: int
    gap: bool
    details_rows: int | None
    """Details content rows; None omits the panel."""
    list_rows: int
    """Maximum content rows for a menu or picker."""
    bottom_rows: int
    """Bottom area lines before cropping."""
    crop_top: int
    """Lines removed from the bottom area's top."""


def frame_layout(
    *,
    height: int,
    table_lines: int,
    notice_lines: int,
    hint_lines: int,
    has_bottom: bool,
    details_cap: int,
    details_fit: bool,
    bottom_lines: Callable[[int, int | None], int],
) -> FrameLayout:
    """Budget the frame's inner height using a bottom-area line counter."""
    rest = height - notice_lines - hint_lines
    if not has_bottom:
        table_rows = min(table_lines, max(0, rest))
        return _fit(height, table_rows, notice_lines, False, None, MIN_LIST_ROWS, 0, hint_lines)
    floor = min(table_lines, MIN_TABLE_ROWS + 1)
    room = max(0, rest - 1 - floor) - OVERLAY_BORDER_ROWS
    details_rows = min(details_cap, room)
    shown_details = details_rows if details_fit and details_rows >= MIN_DETAILS_ROWS else None
    list_rows = max(room, MIN_LIST_ROWS)
    bottom = bottom_lines(list_rows, shown_details)
    gap = bottom > 0
    table_rows = min(table_lines, max(0, rest - int(gap) - bottom))
    return _fit(height, table_rows, notice_lines, gap, shown_details, list_rows, bottom, hint_lines)


def _fit(
    height: int,
    table_rows: int,
    notice_rows: int,
    gap: bool,
    details_rows: int | None,
    list_rows: int,
    bottom: int,
    hint_lines: int,
) -> FrameLayout:
    """Lose lines from the top until everything fits."""
    over = table_rows + notice_rows + int(gap) + bottom + hint_lines - height
    cut = min(max(0, over), table_rows)
    table_rows -= cut
    over -= cut
    cut = min(max(0, over), notice_rows)
    notice_rows -= cut
    over -= cut
    if over > 0 and gap:
        gap = False
        over -= 1
    crop_top = min(max(0, over), bottom)
    return FrameLayout(table_rows, notice_rows, gap, details_rows, list_rows, bottom, crop_top)
