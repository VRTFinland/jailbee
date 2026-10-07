"""Which dashboard columns are drawn, and how wide, at a horizontal scroll offset.

The first column is frozen. The rest scroll a whole column per step, so the
scrolled area always starts on a column boundary. Every column keeps its
budgeted width. The one that meets the right edge is truncated to whatever is
left, or left out when that is too little to read. Pure geometry: no Rich, so
the terminal dashboard's render and its key loop share one source of truth.

Widths follow the dashboard tables' layout (``padding=(0, 1)``,
``pad_edge=False``). The first column costs its width, and each later column,
including a 1-cell ``<``/``>`` mark column, costs its width plus 2.
"""

from __future__ import annotations

from dataclasses import dataclass

MARK_COST = 3
"""A 1-cell mark column plus the 2 cells of padding in front of it."""

MIN_PARTIAL = 3
"""The narrowest truncated column still drawn ("x…" needs room to mean anything)."""


@dataclass(frozen=True)
class Viewport:
    indices: tuple[int, ...]
    """Indices into the caller's columns, frozen column first."""
    widths: tuple[int, ...]
    """Rendered width per entry of ``indices``; the last may be truncated."""
    offset: int
    """The offset actually applied, clamped to the scrollable range."""
    hidden_left: bool
    """Draw the left mark: scrolled columns are out of view on the left."""
    hidden_right: bool
    """Draw the right mark: a column is out of view or truncated on the right."""


def _layout(widths: tuple[int, ...], available: int, offset: int) -> Viewport:
    if available <= 0:
        return Viewport((0,), (0,), 0, False, False)
    first = max(1, min(widths[0], available))
    if len(widths) == 1 or first == available:
        return Viewport((0,), (first,), 0, False, False)
    left = offset > 0 and first + MARK_COST <= available
    used = first + (MARK_COST if left else 0)
    indices, drawn = [0], [first]
    last = len(widths) - 1
    for i in range(1 + offset, len(widths)):
        whole = used + 2 + widths[i] + (0 if i == last else MARK_COST)
        if whole <= available:
            indices.append(i)
            drawn.append(widths[i])
            used += 2 + widths[i]
            continue
        room = available - used - 2 - MARK_COST
        if room >= MIN_PARTIAL:
            indices.append(i)
            drawn.append(room)
        return Viewport(tuple(indices), tuple(drawn), offset, left, used + MARK_COST <= available)
    return Viewport(tuple(indices), tuple(drawn), offset, left, False)


def _complete(view: Viewport, widths: tuple[int, ...]) -> bool:
    """Whether the last column is drawn at its full width."""
    last = len(widths) - 1
    return bool(view.indices) and view.indices[-1] == last and view.widths[-1] == widths[last]


def column_viewport(widths: tuple[int, ...], available: int, offset: int) -> Viewport:
    """The columns drawn at ``offset``, which is clamped first.

    The clamp's upper end is the smallest offset that draws the last column
    whole. Past it, scrolling right would only uncover blank space, and an
    unclamped overshoot would make the next left-arrow look dead.
    """
    if not widths:
        return Viewport((), (), 0, False, False)
    if available <= 0:
        return Viewport((0,), (0,), 0, False, False)
    highest = max(0, len(widths) - 2)
    drawable = False
    for candidate in range(highest + 1):
        view = _layout(widths, available, candidate)
        drawable |= len(view.indices) > 1
        if _complete(view, widths):
            highest = candidate
            break
    if not drawable:
        view = _layout(widths, available, 0)
        return Viewport(view.indices, view.widths, 0, False, False)
    # A mark-only intermediate step must remain traversable to later columns.
    clamped = max(0, min(offset, highest))
    return _layout(widths, available, clamped)
