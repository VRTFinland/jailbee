"""The dashboard's scrollbar look: a thin line, like the details card's own.

Textual draws a vertical scrollbar as a block of reversed cells. Swapping its
renderer for this one gives the table, the menus and the pickers the same
``┃`` thumb on a ``│`` track the details card draws for itself
(`jailbee.dashboard.details`), so the two never look like different widgets.
"""

from __future__ import annotations

from rich.color import Color
from rich.segment import Segment, Segments
from rich.style import Style
from textual.scrollbar import ScrollBar, ScrollBarRender

from jailbee.dashboard.details import (
    SCROLL_THUMB_GLYPH,
    SCROLL_THUMB_STYLE,
    SCROLL_TRACK_GLYPH,
    SCROLL_TRACK_STYLE,
)

_BACK, _BAR = Color.parse("#555555"), Color.parse("bright_magenta")  # Textual's defaults


class ThinScrollBarRender(ScrollBarRender):
    """A vertical scrollbar as a thin line; a horizontal one stays Textual's."""

    @classmethod
    def render_bar(
        cls,
        size: int = 25,
        virtual_size: float = 50,
        window_size: float = 20,
        position: float = 0,
        thickness: int = 1,
        vertical: bool = True,
        back_color: Color = _BACK,
        bar_color: Color = _BAR,
    ) -> Segments:
        if not vertical:
            return super().render_bar(
                size,
                virtual_size,
                window_size,
                position,
                thickness,
                vertical,
                back_color,
                bar_color,
            )
        # Textual's own `@mouse` metadata: grab the thumb, page above or below it.
        grab, up, down = (
            {"@mouse.down": "grab"},
            {"@mouse.down": "scroll_up"},
            {"@mouse.down": "scroll_down"},
        )
        track = SCROLL_TRACK_GLYPH * thickness
        if not (window_size and size and virtual_size and size != virtual_size):
            return Segments([Segment(track, SCROLL_TRACK_STYLE)] * int(size), new_lines=True)
        thumb = max(1, round(window_size * size / virtual_size))
        furthest = virtual_size - window_size
        start = round((size - thumb) * (position / furthest)) if furthest > 0 else 0
        segments = [
            Segment(
                SCROLL_THUMB_GLYPH * thickness,
                SCROLL_THUMB_STYLE + Style(meta=grab),
            )
            if start <= row < start + thumb
            else Segment(track, SCROLL_TRACK_STYLE + Style(meta=up if row < start else down))
            for row in range(int(size))
        ]
        return Segments(segments, new_lines=True)


def install() -> None:
    """Make every Textual scrollbar of this process use the thin look."""
    ScrollBar.renderer = ThinScrollBarRender
