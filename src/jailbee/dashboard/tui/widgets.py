"""Native dashboard frame and fleet widgets over the pure layout and renderers."""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property

from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.geometry import Region, Size, Spacing
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.scrollbar import ScrollDown, ScrollLeft, ScrollRight, ScrollTo, ScrollUp
from textual.strip import Strip
from textual.widgets import Static

from jailbee.dashboard import hit as dhit
from jailbee.dashboard.details import (
    DETAILS_MAX_ROWS,
    DETAILS_PAIR_WIDTH,
    DetailsView,
    details_for,
    render_details,
)
from jailbee.dashboard.hit import TABLE_HIT_KINDS
from jailbee.dashboard.model import Row
from jailbee.dashboard.tui import fleet
from jailbee.dashboard.tui.fleet import TableModel, entry_cells, entry_line, header_line
from jailbee.dashboard.tui.frame import (
    DashboardView,
    HoverHighlight,
    _hint_line,
    _render_overlay,
    frame_title,
    notice_parts,
)
from jailbee.dashboard.tui.layout import FRAME_INSET_COLS, FrameLayout, frame_layout
from jailbee.dashboard.tui.menu_state import MenuState, RepoMenuState
from jailbee.dashboard.tui.native import OverlayBox, build_box
from jailbee.dashboard.tui.overlay import NativeState, Overlay, is_native, overlay_key

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
        scrollbar-background: ansi_default;
        scrollbar-background-hover: ansi_default;
        scrollbar-background-active: ansi_default;
        scrollbar-color: ansi_default;
        scrollbar-color-hover: ansi_default;
        scrollbar-color-active: ansi_default;
    }
    """

    class GeometryChanged(Message):
        """The actual content width has settled after layout."""

    class WheelScrolled(Message):
        """The wheel moved the table vertically; the row under the pointer changed."""

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
        self._placeholder_width = 0
        self._height = -1
        self._drawn: tuple[tuple[object, ...], ...] = ()
        self._strips: dict[tuple[object, ...], Strip] = {}

    @property
    def content_width(self) -> int:
        return self.scrollable_content_region.width

    def show(
        self,
        model: TableModel,
        selected: Row | None,
        hover: dhit.Hit | None,
        *,
        width: int | None = None,
    ) -> None:
        previous = self._model
        old_selected = self._selected
        self._model, self._selected, self._hover = model, selected, hover
        if model.empty_text is not None:
            self._placeholder_width = max(
                1, width if width is not None else self.content_width or self.app.size.width
            )
        drawn = self._signatures(model)
        structural = (
            previous is None
            or (model.empty_text is not None and drawn != self._drawn)
            or previous.geometry != model.geometry
            or previous.rows != model.rows
            or previous.has_header != model.has_header
            or previous.empty_text != model.empty_text
        )
        if structural:
            self.virtual_size = Size(
                self._placeholder_width if model.empty_text is not None else model.geometry.width,
                len(drawn),
            )
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
        self.post_message(self.GeometryChanged())
        if self._model is not None:
            if self._model.empty_text is not None:
                self.show(self._model, self._selected, self._hover, width=self.content_width)
                self.call_after_refresh(self._rewrap_placeholder)
            self.refresh()
            if self._selected is not None and self.size.height != self._height:
                self.scroll_to_row(self._selected)
            self._height = self.size.height

    def _rewrap_placeholder(self) -> None:
        if self._model is not None and self._model.empty_text is not None:
            self.show(self._model, self._selected, self._hover, width=self.content_width)

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
            return tuple(
                ("empty", line.plain)
                for line in Text(model.empty_text).wrap(self.app.console, self._placeholder_width)
            )
        hover = self._hover
        lines: list[tuple[object, ...]] = []
        if model.has_header:
            lines.append(
                ("header", model.geometry, hover if hover and hover.kind == "scroll" else None)
            )
        for entry in model.entries:
            target = None
            if hover and hover.args and hover.args[0] == entry.row.key:
                if (entry.row.kind == "container" and hover.kind == "row") or (
                    entry.row.kind == "repo" and hover.kind in ("repo", "fold")
                ):
                    target = hover
            lines.append(
                (
                    "entry",
                    entry.row,
                    entry.heading,
                    entry_cells(entry, model.geometry),
                    model.geometry,
                    entry.row.key in model.folded if entry.heading is not None else False,
                    entry.row == self._selected,
                    target,
                )
            )
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
                text = Text(str(lines[virtual_y][1]), no_wrap=True, end="")
            elif model.has_header and virtual_y == 0:
                text = header_line(model.geometry)
            else:
                entry = model.entries[virtual_y - int(model.has_header)]
                text = entry_line(
                    entry,
                    model.geometry,
                    model.folded,
                    selected=entry.row == self._selected,
                    width=width,
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
            # Textual sends no MouseMove after a wheel notch: the app re-resolves hover.
            self.post_message(self.WheelScrolled())

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


FRAME_BORDER_ROWS = 2


class DetailsPanel(Static):
    DEFAULT_CSS = (
        "DetailsPanel { width: 1fr; height: auto; background: ansi_default; color: ansi_default; }"
    )

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._shown: tuple[DetailsView, int] | None = None

    def show(self, view: DetailsView | None, rows: int | None) -> None:
        shown = None if view is None or rows is None else (view, rows)
        self.display = shown is not None
        if shown is not None and shown != self._shown:
            self.update(render_details(shown[0], shown[1], fixed=True))
        self._shown = shown


class _CropTop:
    """``renderable`` without its first ``lines`` lines."""

    def __init__(self, renderable: RenderableType, lines: int) -> None:
        self.renderable, self.lines = renderable, lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        kept = console.render_lines(self.renderable, options.update(height=None), pad=False)[
            self.lines :
        ]
        for line in kept:
            yield from line
            # RichVisual counts terminated lines; keep the suffix's final border too.
            yield Segment.line()


class OverlayPanel(Static):
    DEFAULT_CSS = (
        "OverlayPanel { width: auto; height: auto; background: ansi_default; color: ansi_default; }"
    )

    class Wheel(Message):
        def __init__(self, step: int) -> None:
            super().__init__()
            self.step = step

    def __init__(
        self, *, id: str | None = None, mouse_enabled: Callable[[], bool] | None = None
    ) -> None:
        super().__init__(id=id)
        self.mouse_enabled: Callable[[], bool] = mouse_enabled or (lambda: True)
        self.auto_links = False  # the panel tags its own targets (see `jailbee.dashboard.hit`)

    def show(self, renderable: RenderableType | None, crop_top: int) -> None:
        self.display = renderable is not None
        if renderable is not None:
            self.update(_CropTop(renderable, crop_top) if crop_top else renderable)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._wheel(event, 1)

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._wheel(event, -1)

    def _wheel(self, event: events.MouseEvent, step: int) -> None:
        event.stop()
        event.prevent_default()
        if self.mouse_enabled() and not event.shift:
            self.post_message(self.Wheel(step))

    def _on_mouse_scroll_left(self, event: events.MouseScrollLeft) -> None:
        event.stop()
        event.prevent_default()

    def _on_mouse_scroll_right(self, event: events.MouseScrollRight) -> None:
        event.stop()
        event.prevent_default()


class DashboardFrame(Vertical):
    """The whole dashboard inside one rounded border.

    Title: the summary and clock; subtitle: a short notice. Inside, top to
    bottom: the table, a long notice, the bottom area (details and/or an
    overlay) and the hint — sized by :func:`jailbee.dashboard.tui.layout.frame_layout`.
    Each part repaints only when its own input changed; the clock touches
    only the border.
    """

    DEFAULT_CSS = """
    DashboardFrame {
        width: 100%;
        height: auto;
        border: round ansi_default;
        border-title-align: left;
        border-subtitle-align: left;
        padding: 0 1;
        background: ansi_default;
        color: ansi_default;
    }
    DashboardFrame > #notice { height: auto; background: ansi_default; }
    DashboardFrame > #bottom { height: auto; }
    DashboardFrame > #hint { height: auto; background: ansi_default; color: ansi_default; }
    """

    def __init__(
        self, *, id: str | None = None, mouse_enabled: Callable[[], bool] | None = None
    ) -> None:
        super().__init__(id=id)
        self.mouse_enabled = mouse_enabled or (lambda: True)
        self._overlay_input: object = None
        self._notice_input: object = None
        self._hint_input: object = None
        self.native_box: OverlayBox | None = None
        self._native_overlay_key: tuple[object, ...] | None = None

    def compose(self) -> ComposeResult:
        yield FleetTable(id="fleet", mouse_enabled=self.mouse_enabled)
        yield Static(id="notice")
        with Horizontal(id="bottom"):
            yield DetailsPanel(id="details")
            yield OverlayPanel(id="overlay", mouse_enabled=self.mouse_enabled)
        yield Static(id="hint")

    @cached_property
    def table(self) -> FleetTable:
        return self.query_one(FleetTable)

    def _sync_native(self, overlay: Overlay | None) -> OverlayBox | None:
        """Keep, refresh, replace or drop the native box for ``overlay``."""
        key = overlay_key(overlay) if is_native(overlay) else None
        box = self.native_box
        if box is not None and key == self._native_overlay_key:
            assert overlay is not None
            box.show(overlay)
            return box
        old = box
        self._native_overlay_key = key
        self.native_box = None
        if key is None or overlay is None:
            if old is not None and old.parent is not None:
                old.remove()
            return None
        box = build_box(overlay, mouse_enabled=self.mouse_enabled)
        self.native_box = box
        if old is None:
            self._mount_native(box)
        else:
            # Both boxes hold a child with the fixed id `native-list`; Textual
            # refuses the duplicate until the old box has left the DOM.
            self.app.call_later(self._swap_native, old, box)
        return box

    def _mount_native(self, box: OverlayBox) -> None:
        self.query_one("#bottom", Horizontal).mount(box, before=self.query_one(OverlayPanel))

    async def _swap_native(self, old: OverlayBox, box: OverlayBox) -> None:
        """Replace ``old`` by ``box`` once ``old`` is gone (unless superseded)."""
        if old.parent is not None:
            await old.remove()
        if self.native_box is box:
            await self.query_one("#bottom", Horizontal).mount(
                box, before=self.query_one(OverlayPanel)
            )

    def native_state(self) -> NativeState | None:
        box = self.native_box
        return box.state() if box is not None and box.is_mounted else None

    def show(self, view: DashboardView) -> None:
        width = max(0, self.app.size.width - FRAME_INSET_COLS)
        height = max(0, self.app.size.height - FRAME_BORDER_ROWS)
        console = self.app.console

        def lines(renderable: RenderableType | None, at: int) -> int:
            if renderable is None:
                return 0
            return len(
                console.render_lines(
                    renderable, console.options.update(width=at, height=None), pad=False
                )
            )

        self.border_title = frame_title(
            view.groups, view.folded, git_enabled=view.git_enabled, now=view.now
        )
        subtitle, inline = notice_parts(view.notice)
        self.border_subtitle = subtitle if subtitle is not None else ""
        overlay = view.overlay
        box = self._sync_native(overlay)
        legacy = overlay if box is None else None
        menu = isinstance(overlay, (MenuState, RepoMenuState))
        details = (
            details_for(view.groups, view.selected, view.now)
            if view.show_details and view.groups
            else None
        )
        hint = _hint_line(overlay) if overlay is not None else None
        menu_width = (box.natural_width() or 0) if box is not None and menu else 0
        details_fit = not menu or width - menu_width >= DETAILS_PAIR_WIDTH
        overlay_hover = (
            view.hover
            if view.hover is not None and view.hover.kind not in TABLE_HIT_KINDS
            else None
        )

        def overlay_renderable(list_rows: int) -> RenderableType | None:
            if legacy is None:
                return None
            return HoverHighlight(_render_overlay(legacy, list_rows), overlay_hover)

        def bottom_lines(list_rows: int, details_rows: int | None) -> int:
            beside = details is not None and details_rows is not None and (overlay is None or menu)
            shown_details = details_rows + 2 if beside and details_rows is not None else 0
            if box is not None:
                return max(shown_details, min(box.content_rows(), list_rows) + box.chrome_rows())
            overlay_at = menu_width if beside else width
            return max(shown_details, lines(overlay_renderable(list_rows), overlay_at))

        table_lines = fleet.line_count(view.groups, view.folded)
        if not view.groups:
            # The model counts one logical placeholder; its instruction wraps on screen.
            placeholder = fleet.HIDDEN_TEXT if view.hidden_by_preferences else fleet.EMPTY_TEXT
            table_lines = len(Text(placeholder).wrap(console, max(1, width)))

        def fit(table_lines: int) -> FrameLayout:
            return frame_layout(
                height=height,
                table_lines=table_lines,
                notice_lines=lines(inline, width),
                hint_lines=lines(hint, width),
                has_bottom=overlay is not None or details is not None,
                details_cap=DETAILS_MAX_ROWS if details is None else details.max_rows,
                details_fit=details_fit,
                bottom_lines=bottom_lines,
            )

        layout = fit(table_lines)
        scrollbar = 1 if layout.table_rows < table_lines else 0
        if not view.groups and scrollbar:
            table_lines = len(Text(placeholder).wrap(console, max(1, width - scrollbar)))
            layout = fit(table_lines)
        model = fleet.table_model(
            view.groups,
            now=view.now,
            enabled=view.enabled,
            folded=view.folded,
            column_widths=view.column_widths,
            shown_columns=view.shown_columns,
            column_offset=view.column_offset,
            hidden_by_preferences=view.hidden_by_preferences,
            width=max(0, width - scrollbar),
        )
        table = self.table
        table.display = layout.table_rows > 0
        table.styles.height = layout.table_rows
        table_hover = (
            view.hover if view.hover is not None and view.hover.kind in TABLE_HIT_KINDS else None
        )
        table.show(model, view.selected, table_hover, width=max(1, width - scrollbar))
        notice = self.query_one("#notice", Static)
        notice.display = inline is not None and layout.notice_rows > 0
        if inline is not None:
            notice_input = (view.notice, width, layout.notice_rows)
            if notice_input != self._notice_input:
                notice.update(_CropTop(inline, max(0, lines(inline, width) - layout.notice_rows)))
                self._notice_input = notice_input
            notice.styles.height = layout.notice_rows
        bottom = self.query_one("#bottom", Horizontal)
        bottom.display = layout.bottom_rows > layout.crop_top
        bottom.styles.margin = (1 if layout.gap else 0, 0, 0, 0)
        beside = (
            details is not None and layout.details_rows is not None and (overlay is None or menu)
        )
        self.query_one(DetailsPanel).show(details if beside else None, layout.details_rows)
        if box is not None:
            rows = min(box.content_rows(), layout.list_rows) + box.chrome_rows() - layout.crop_top
            box.display = rows > box.chrome_rows()
            box.styles.height = max(0, rows)
            natural = box.natural_width()
            box.styles.width = menu_width if beside else natural if natural is not None else "1fr"
            box.styles.max_width = "100%"
        panel = self.query_one(OverlayPanel)
        panel.styles.width = menu_width if beside and legacy is not None else "1fr"
        overlay_input = (legacy, layout.list_rows, overlay_hover, layout.crop_top)
        if overlay_input != self._overlay_input:
            panel.show(overlay_renderable(layout.list_rows), layout.crop_top)
            self._overlay_input = overlay_input
        hint_widget = self.query_one("#hint", Static)
        hint_widget.display = hint is not None
        if hint is not None and hint != self._hint_input:
            hint_widget.update(hint)
        self._hint_input = hint
