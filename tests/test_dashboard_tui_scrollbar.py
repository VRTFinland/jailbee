"""The dashboard's scrollbars share one thin-line look."""

from __future__ import annotations

from rich.console import Console

from jailbee.dashboard.details import SCROLL_THUMB_GLYPH, SCROLL_TRACK_GLYPH
from jailbee.dashboard.tui.scrollbar import ThinScrollBarRender


def _column(**kw: float) -> list[str]:
    segments = ThinScrollBarRender.render_bar(size=10, **kw)  # type: ignore[arg-type]  # test call
    return [s.text for s in Console().render(segments) if s.text != "\n"]


def test_a_vertical_bar_is_a_thin_thumb_on_a_thin_track() -> None:
    top = _column(virtual_size=50, window_size=10, position=0)
    assert top == [SCROLL_THUMB_GLYPH] * 2 + [SCROLL_TRACK_GLYPH] * 8
    end = _column(virtual_size=50, window_size=10, position=40)
    assert end == [SCROLL_TRACK_GLYPH] * 8 + [SCROLL_THUMB_GLYPH] * 2


def test_a_bar_with_nothing_to_scroll_is_all_track() -> None:
    assert _column(virtual_size=10, window_size=10, position=0) == [SCROLL_TRACK_GLYPH] * 10


def test_the_thumb_and_the_track_keep_textuals_mouse_actions() -> None:
    segments = ThinScrollBarRender.render_bar(size=6, virtual_size=60, window_size=10).segments
    actions = [s.style.meta.get("@mouse.down") for s in segments if s.style and s.text != "\n"]
    assert actions == [
        "grab",
        "scroll_down",
        "scroll_down",
        "scroll_down",
        "scroll_down",
        "scroll_down",
    ]
