"""The Outbox manager's native box: the proposal tree beside a preview.

A box shows the `OutboxState` it is given and posts what the user did as
`OutboxBox.Intent` messages (the vocabulary of `jailbee.dashboard.outbox_flow`);
it never changes the state itself. Keys reach it only through `handle_key`.

Layout: the row list and the preview sit side by side from
`SIDE_BY_SIDE_MIN_COLS` columns of content; below that the preview replaces the
list while it is on (`v`), or while a question is open (the question's lines
live in the preview and must be readable). A question's choices are a second
list under the preview and take the keys.
"""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from jailbee.dashboard.outbox import (
    OutboxState,
    Row,
    RowKey,
    outbox_hint,
    preview_lines,
    visible_rows,
)
from jailbee.dashboard.outbox_flow import Intent as IntentName
from jailbee.dashboard.tui.layout import BOX_INSET_COLS, FRAME_INSET_COLS
from jailbee.dashboard.tui.native import (
    _SCROLLBAR_CSS,
    NATIVE_LIST_ID,
    KeyOutcome,
    OverlayBox,
    OverlayList,
    _wheel,
)
from jailbee.dashboard.tui.overlay import NativeState, Overlay

SIDE_BY_SIDE_MIN_COLS = 110
MIN_OUTBOX_ROWS = 10

QUESTION_ID = "outbox-question"
PREVIEW_ID = "outbox-preview"
RIGHT_ID = "outbox-right"

_LIST_SHARE = 3  # the list's share of a side-by-side body, out of _BODY_SHARES
_BODY_SHARES = 5

# Row-list keys -> the intent they post, and the ability each needs (None: always).
_INTENT_KEYS: dict[str, IntentName] = {
    "enter": "expand",
    "right": "expand",
    "left": "collapse",
    "space": "mark",
    "d": "delete",
    "p": "publish",
    "c": "create-pr",
    "X": "clear",
    "v": "preview",
    "m": "raw",
    "r": "refresh",
    "a": "scope",
}
_PREVIEW_SCROLL = {
    "up": "scroll_up",
    "k": "scroll_up",
    "down": "scroll_down",
    "j": "scroll_down",
    "pageup": "page_up",
    "pagedown": "page_down",
    "home": "scroll_home",
    "end": "scroll_end",
}
_QUESTION_MOVES = {
    "up": "cursor_up",
    "k": "cursor_up",
    "down": "cursor_down",
    "j": "cursor_down",
}


def _title(text: str) -> Text:
    return Text(text, style="bold", no_wrap=True, overflow="ellipsis")


def _line(text: str, style: str = "") -> Text:
    return Text(text, style=style, no_wrap=True, overflow="ellipsis")


def _safe(text: str) -> str:
    """``text`` without control or zero-width characters (it may come from the container)."""
    from jailbee.outbox.inspect import safe_text

    return safe_text(text)


class PreviewScroll(VerticalScroll):
    DEFAULT_CSS = f"""
    PreviewScroll {{
        height: 1fr;
        background: ansi_default;
        color: ansi_default;
        padding: 0 1;
        min-height: 3;
        {_SCROLLBAR_CSS}
    }}
    PreviewScroll > Static {{ background: ansi_default; color: ansi_default; }}
    """

    def __init__(self, *children: Widget, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(*children, id=PREVIEW_ID)
        self.mouse_enabled = mouse_enabled

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        _wheel(self, event, 1, self.mouse_enabled())

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        _wheel(self, event, -1, self.mouse_enabled())


class OutboxBox(OverlayBox):
    """The proposals of one container (or every repo's), with preview and questions."""

    DEFAULT_CSS = """
    OutboxBox.-standalone { height: 1fr; }
    OutboxBox > #outbox-body { height: 1fr; }
    OutboxBox > .outbox-line {
        height: 1; width: 1fr; background: ansi_default; color: ansi_default;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    OutboxBox #outbox-right { height: 1fr; background: ansi_default; }
    OutboxBox #outbox-question { height: auto; max-height: 8; }
    """

    class Intent(Message):
        """The user asked for ``name`` on ``row`` (the cursor row; None for a question)."""

        def __init__(
            self,
            key: tuple[object, ...] | None,
            name: IntentName,
            row: RowKey | None,
            value: str | None = None,
        ) -> None:
            super().__init__()
            self.key = key
            self.name = name
            self.row = row
            self.value = value

    def __init__(
        self, spec: OutboxState, *, mouse_enabled: Callable[[], bool], standalone: bool = False
    ) -> None:
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self.panel = spec
        self.standalone = standalone
        self._width = 0
        self._rows: tuple[Row, ...] = ()
        # The row key of the last intent this box posted. Limit: while it equals the
        # state's cursor, every show() treats that cursor as a stale echo, so a
        # controller that really re-selects that same row cannot move us off where
        # the user has since scrolled to.
        self._last_sent: RowKey | None = None
        self._asked: object = None
        self.set_class(standalone, "-standalone")
        self.border_title = _title(f"Outbox · {spec.title}")

    # -- geometry -----------------------------------------------------------

    def _estimate_width(self) -> int:
        inset = BOX_INSET_COLS + (0 if self.standalone else FRAME_INSET_COLS)
        try:
            return max(1, self.app.size.width - inset)
        except Exception:  # no active app yet (a box measured before mount)
            return 80

    def _side(self) -> bool:
        return self._width >= SIDE_BY_SIDE_MIN_COLS and self.panel.preview != "off"

    def _preview_shown(self) -> bool:
        if self.panel.question is not None:
            return True
        if self.panel.preview == "off":
            return False
        return self._width >= SIDE_BY_SIDE_MIN_COLS or self.panel.preview == "on"

    def _list_shown(self) -> bool:
        return not (self._preview_shown() and self._width < SIDE_BY_SIDE_MIN_COLS)

    def _list_width(self) -> int:
        both = self._preview_shown() and self._list_shown()
        share = self._width * _LIST_SHARE // _BODY_SHARES if both else self._width
        return max(1, share - 1)  # the scrollbar's column

    # -- widgets ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        self._width = self._width or self._estimate_width()
        self._rows = visible_rows(self.panel, self._list_width())
        with Horizontal(id="outbox-body"):
            yield OverlayList(
                *self._options(), mouse_enabled=self.mouse_enabled, double_click_chooses=True
            )
            with Vertical(id=RIGHT_ID):
                yield PreviewScroll(
                    Static(Text(), id="outbox-preview-text", markup=False),
                    mouse_enabled=self.mouse_enabled,
                )
                yield OverlayList(mouse_enabled=self.mouse_enabled, list_id=QUESTION_ID)
        yield Static(Text(), id="outbox-status", classes="outbox-line", markup=False)
        if self.standalone:
            yield Static(Text(), id="outbox-hint", classes="outbox-line", markup=False)

    def _options(self) -> list[Option]:
        options = [Option(row.text, disabled=row.key is None) for row in self._rows]
        return options or [Option(_line("(the outbox is empty)", "dim"), disabled=True)]

    @property
    def _list(self) -> OverlayList:
        return self.query_one(f"#{NATIVE_LIST_ID}", OverlayList)

    @property
    def _questions(self) -> OverlayList:
        return self.query_one(f"#{QUESTION_ID}", OverlayList)

    def _cursor_key(self) -> RowKey | None:
        index = self._list.highlighted if self.is_mounted else None
        if index is None or not 0 <= index < len(self._rows):
            return None
        return self._rows[index].key

    def _index_of(self, key: RowKey | None) -> int | None:
        if key is None:
            return None
        return next((i for i, row in enumerate(self._rows) if row.key == key), None)

    def _first_selectable(self) -> int | None:
        return next((i for i, row in enumerate(self._rows) if row.key is not None), None)

    def _nearest(self, index: int | None) -> int | None:
        picks = [i for i, row in enumerate(self._rows) if row.key is not None]
        if not picks:
            return None
        return min(picks, key=lambda i: abs(i - (index or 0)))

    def _ready(self) -> None:
        self._apply(cursor=self._index_of(self.panel.cursor), rebuild=True)

    def _apply(self, *, cursor: int | None, rebuild: bool) -> None:
        """Lay the panel out for the current width; ``rebuild`` repopulates the row list."""
        panel = self.panel
        lst = self._list
        if rebuild:
            self._rows = visible_rows(panel, self._list_width())
            with lst.prevent(OptionList.OptionHighlighted):
                lst.clear_options()
                lst.add_options(self._options())
                target = cursor if cursor is not None else self._nearest(0)
                lst.highlighted = self._nearest(target) if target is not None else None
        right = self.query_one(f"#{RIGHT_ID}")
        lst_shown, preview_shown = self._list_shown(), self._preview_shown()
        lst.display = lst_shown
        right.display = preview_shown
        both = lst_shown and preview_shown
        lst.styles.width = f"{_LIST_SHARE}fr" if both else "1fr"
        right.styles.width = f"{_BODY_SHARES - _LIST_SHARE}fr" if both else "1fr"
        self._sync_question()
        self._update_preview()
        status = panel.busy or (panel.status[-1] if panel.status else "")
        self.query_one("#outbox-status", Static).update(_line(_safe(status), "dim"))
        if self.standalone:
            self.query_one("#outbox-hint", Static).update(
                _line(outbox_hint(panel, standalone=True), "dim")
            )
        self.focus_target().focus()

    def _sync_question(self) -> None:
        question = self.panel.question
        lst = self._questions
        if question != self._asked:
            self._asked = question
            lst.clear_options()
            if question is not None:
                lst.add_options(
                    [Option(_line(label), id=value) for label, value in question.choices]
                )
                values = [value for _, value in question.choices]
                default = question.default
                lst.highlighted = values.index(default) if default in values else 0
        lst.display = question is not None

    def _update_preview(self) -> None:
        if not self._preview_shown():
            return
        both = self._list_shown()
        room = self._width - self._width * _LIST_SHARE // _BODY_SHARES if both else self._width
        width = max(20, room - 3)
        lines = list(preview_lines(self.panel, self._cursor_key(), width))
        if self.panel.question is not None:
            heading = Text(_safe(self.panel.question.title), style="bold")
            lines = [heading, *lines]
        self.query_one("#outbox-preview-text", Static).update(Text("\n").join(lines))

    # -- overlay protocol ---------------------------------------------------

    def focus_target(self) -> Widget:
        if self.panel.question is not None and self.is_mounted:
            return self._questions
        return self._list

    def hint(self) -> str:
        """The key hint for the current state (the dashboard frame shows it)."""
        return outbox_hint(self.panel, standalone=self.standalone)

    def content_rows(self) -> int:
        rows = self._rows or visible_rows(self.panel, self._list_width() if self._width else 80)
        question = self.panel.question
        return max(MIN_OUTBOX_ROWS, len(rows) + 1) + (len(question.choices) if question else 0)

    def state(self) -> NativeState:
        return NativeState("outbox", self._list.highlighted, text=self.panel.preview)

    def show(self, spec: Overlay) -> None:
        """New data: the cursor stays on its row key, unless the controller moved it."""
        super().show(spec)
        assert isinstance(spec, OutboxState)
        before = self.content_rows()
        old_key = self._cursor_key()
        old_index = self._list.highlighted if self.is_mounted else None
        old_cursor = self.panel.cursor
        self.panel = spec
        if not self.is_mounted:
            self._rows = visible_rows(spec, self._list_width() if self._width else 80)
            return
        # The state's cursor is the controller's. Arrow keys never reach it, so an
        # unchanged one (a repaint) is stale, and one that is just what this box last
        # sent is an echo (the user may have moved since); any other is a move.
        self._rows = visible_rows(spec, self._list_width())
        fresh = spec.cursor not in (old_cursor, self._last_sent)
        moved = spec.cursor if fresh else None
        cursor = next(
            (
                i
                for i in (
                    self._index_of(moved),
                    self._index_of(old_key),
                    self._index_of(spec.cursor),
                )
                if i is not None
            ),
            self._nearest(old_index),
        )
        self._apply(cursor=cursor, rebuild=True)
        if self.content_rows() != before:
            self.post_message(self.Changed())

    # -- events -------------------------------------------------------------

    def on_resize(self, event: events.Resize) -> None:
        width = self.content_size.width
        if width > 0 and width != self._width:
            self._width = width
            self._apply(cursor=self._list.highlighted, rebuild=True)
            self.post_message(self.Changed())

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        if event.option_list is self._list:
            self._update_preview()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()  # a double click (Enter never gets here: keys are routed)
        if event.option_list is self._questions:
            self.post_message(self.Intent(self.key, "answer", None, event.option.id))
            return
        if 0 <= event.option_index < len(self._rows):
            row = self._rows[event.option_index].key
            if row is not None:
                self._last_sent = row
                self.post_message(self.Intent(self.key, "toggle", row))

    # -- keys ---------------------------------------------------------------

    def _intent(self, name: IntentName) -> Intent:
        row = self._cursor_key()
        if row is not None:
            self._last_sent = row
        return self.Intent(self.key, name, row)

    def _allowed(self, name: IntentName) -> bool:
        ab = self.panel.abilities
        return {
            "delete": ab.delete,
            "publish": ab.publish or ab.publish_hand_off,
            "create-pr": ab.create_pr,
            "clear": ab.clear,
            "scope": ab.scope and self.standalone,
        }.get(name, True)

    def _question_key(self, event: events.Key) -> KeyOutcome:
        key, lst = event.key, self._questions
        if key in ("escape", "ctrl+c"):
            return self.Intent(self.key, "cancel", None)
        if key == "enter":
            index = lst.highlighted
            value = lst.get_option_at_index(index).id if index is not None else None
            return self.Intent(self.key, "answer", None, value)
        move = _QUESTION_MOVES.get(key)
        if move is not None:
            getattr(lst, f"action_{move}")()
        return True

    async def handle_key(self, event: events.Key) -> KeyOutcome:  # type: ignore[override]  # see OverlayBox.handle_key
        if self.panel.question is not None:
            return self._question_key(event)
        key = event.key
        if key == "escape":
            return self.Cancelled(self.key)
        if key == "q":
            return self.Cancelled(self.key) if self.standalone else False
        name = _INTENT_KEYS.get(key)
        if name is not None:
            return self._intent(name) if self._allowed(name) else True
        if not self._list.display:
            scroll = _PREVIEW_SCROLL.get(key)
            if scroll is None:
                return False
            getattr(self.query_one(f"#{PREVIEW_ID}", PreviewScroll), f"action_{scroll}")()
            return True
        return await super().handle_key(event)
