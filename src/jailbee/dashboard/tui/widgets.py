"""Textual fleet widgets; the remaining table and frame parts are pure."""

from __future__ import annotations

from collections.abc import Callable

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.geometry import Region, Size, Spacing
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.scrollbar import ScrollDown, ScrollLeft, ScrollRight, ScrollTo, ScrollUp
from textual.strip import Strip

from jailbee.dashboard import hit as dhit
from jailbee.dashboard.model import Row
from jailbee.dashboard.tui.fleet import TableModel, entry_cells, entry_line, header_line

_CACHE_MAX = 4096


class FleetTable(ScrollView, can_focus=False):
    """Frozen header and virtual rows, with selection independent of wheel scroll.

    ``mouse_enabled`` is a live policy supplied by the enclosing frame. It
    defaults to enabled for standalone use and gates delivered input only.
    """

    DEFAULT_CSS = """
    FleetTable {
        height: auto;
        background: ansi_default;
        color: ansi_default;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 0;
    }
    """

    class ColumnScroll(Message):
        def __init__(self, step: int) -> None:
            super().__init__()
            self.step = step

    def __init__(
        self, *, id: str | None = None, mouse_enabled: Callable[[], bool] | None = None
    ) -> None:
        super().__init__(id=id)
        self.mouse_enabled = mouse_enabled or (lambda: True)
        self._model: TableModel | None = None
        self._selected: Row | None = None
        self._hover: dhit.Hit | None = None
        self._height = -1
        self._drawn: tuple[tuple[object, ...], ...] = ()
        self._strips: dict[tuple[object, ...], Strip] = {}

    @property
    def content_width(self) -> int:
        return self.scrollable_content_region.width

    def show(self, model: TableModel, selected: Row | None, hover: dhit.Hit | None) -> None:
        previous = self._model
        old_selected = self._selected
        self._model, self._selected, self._hover = model, selected, hover
        drawn = self._signatures(model)
        structural = (
            previous is None
            or previous.geometry != model.geometry
            or previous.rows != model.rows
            or previous.has_header != model.has_header
            or previous.empty_text != model.empty_text
        )
        if structural:
            self.virtual_size = Size(model.geometry.width, model.line_count)
            self.refresh()
        else:
            for virtual_y, (old, new) in enumerate(zip(self._drawn, drawn, strict=True)):
                if old != new:
                    # refresh_line maps virtual rows, but the frozen header is screen y=0.
                    if model.has_header and virtual_y == 0:
                        self.refresh(Region(0, 0, self.size.width, 1))
                    elif (
                        int(model.has_header) <= virtual_y - self.scroll_offset.y < self.size.height
                    ):
                        self.refresh_line(virtual_y)
        self._drawn = drawn
        height = self.size.height
        if selected is not None and (selected != old_selected or height != self._height):
            if structural:
                # Scroll bounds/allow_vertical_scroll settle during the next layout pass.
                self.call_after_refresh(self._reveal_selection, selected)
            else:
                self.scroll_to_row(selected)
        self._height = height

    def on_resize(self, event: events.Resize) -> None:
        if self._model is not None:
            self.refresh()
            if self._selected is not None and self.size.height != self._height:
                self.scroll_to_row(self._selected)
            self._height = self.size.height

    def _reveal_selection(self, row: Row) -> None:
        if self._selected == row:
            self.scroll_to_row(row)

    def scroll_to_row(self, row: Row) -> None:
        model = self._model
        if model is None or row not in model.rows:
            return
        y = model.rows.index(row) + int(model.has_header)
        self.scroll_to_region(
            Region(0, y, 1, 1),
            spacing=Spacing(top=int(model.has_header)),
            animate=False,
            immediate=True,
        )

    def _paint(self, text: Text) -> tuple[Segment, ...]:
        # Text.render drops a base style when there are no spans; Console.render preserves it.
        return tuple(dhit.hover_segments(list(self.app.console.render(text)), self._hover))

    def _signatures(self, model: TableModel) -> tuple[tuple[object, ...], ...]:
        if model.empty_text is not None:
            return (("empty", model.empty_text),)
        hover = self._hover
        lines: list[tuple[object, ...]] = []
        if model.has_header:
            lines.append(("header", model.geometry, hover if hover and hover.kind == "scroll" else None))
        for entry in model.entries:
            target = None
            if hover and hover.args and hover.args[0] == entry.row.key:
                if (entry.row.kind == "container" and hover.kind == "row") or (
                    entry.row.kind == "repo" and hover.kind in ("repo", "fold")
                ):
                    target = hover
            lines.append((
                "entry", entry.row, entry.heading, entry_cells(entry, model.geometry),
                model.geometry, entry.row.key in model.folded if entry.heading is not None else False,
                entry.row == self._selected, target,
            ))
        # Lightweight cell values capture fresh AGE closures without building Rich lines.
        return tuple(lines)

    def render_line(self, y: int) -> Strip:
        width = self.content_width
        model = self._model
        if model is None:
            return Strip.blank(width, Style(color="default"))
        # Width changes can occur after show when Textual lays out a scrollbar.
        lines = self._drawn
        virtual_y = y if (model.has_header and y == 0) else int(self.scroll_offset.y) + y
        if not 0 <= virtual_y < len(lines):
            return Strip.blank(width, Style(color="default"))
        key = (*lines[virtual_y], width)
        strip = self._strips.get(key)
        if strip is None:
            if model.empty_text is not None:
                text = Text(model.empty_text, no_wrap=True, end="")
            elif model.has_header and virtual_y == 0:
                text = header_line(model.geometry)
            else:
                entry = model.entries[virtual_y - int(model.has_header)]
                text = entry_line(
                    entry, model.geometry, model.folded,
                    selected=entry.row == self._selected, width=width,
                )
            segments = self._paint(text)
            if len(self._strips) >= _CACHE_MAX:
                self._strips.clear()
            strip = (
                Strip(segments)
                .apply_style(Style(color="default"))
                .extend_cell_length(width, Style(color="default"))
                .crop(0, width)
            )
            self._strips[key] = strip
        return strip

    def _wheel(self, event: events.MouseEvent, step: int, sideways: bool) -> None:
        event.stop()
        event.prevent_default()
        if not self.mouse_enabled():
            return
        if sideways:
            self.post_message(self.ColumnScroll(step))
        else:
            self.scroll_relative(y=step, animate=False)

    # Override private handlers: Textual runs every _on_ base handler, otherwise scrolling twice.
    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(event, 1, event.shift)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(event, -1, event.shift)

    def _on_mouse_scroll_right(self, event: events.MouseScrollRight) -> None:
        self._wheel(event, 1, True)

    def _on_mouse_scroll_left(self, event: events.MouseScrollLeft) -> None:
        self._wheel(event, -1, True)

    # Scrollbar messages bypass wheel handlers; gate drag and page clicks as well.
    def _on_scroll_to(self, message: ScrollTo) -> None:
        message.stop()
        message.prevent_default()
        if self.mouse_enabled():
            self.scroll_to(message.x, message.y, animate=message.animate, duration=0.1)

    def _on_scroll_up(self, message: ScrollUp) -> None:
        message.stop()
        message.prevent_default()
        if self.mouse_enabled():
            self.scroll_page_up()

    def _on_scroll_down(self, message: ScrollDown) -> None:
        message.stop()
        message.prevent_default()
        if self.mouse_enabled():
            self.scroll_page_down()

    def _on_scroll_left(self, message: ScrollLeft) -> None:
        message.stop()
        message.prevent_default()
        if self.mouse_enabled():
            self.scroll_page_left()

    def _on_scroll_right(self, message: ScrollRight) -> None:
        message.stop()
        message.prevent_default()
        if self.mouse_enabled():
            self.scroll_page_right()
