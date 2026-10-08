"""Native overlay boxes: one bordered box per overlay kind.

The session decides what is open and what a choice does (it never sees a
cursor); a box shows its overlay's data, owns cursor, hover, scroll and focus,
and posts what the user did. `DashboardFrame` mounts the box for the session's
overlay, keeps it while `overlay_key` is unchanged, and sizes it from
`content_rows`/`natural_width` through `frame_layout`.

Keys: the focused list sees a key first (its ancestors' `on_key` next, then
`DashboardApp.on_key`, then the list's bindings), so a box consumes its own
letters in `on_key` with `stop()` + `prevent_default()`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import ClassVar

from rich.cells import cell_len
from rich.console import Console
from rich.segment import Segment
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Checkbox, OptionList, SelectionList, Static, Tab, Tabs
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from jailbee.dashboard.hit import HOVER_STYLE
from jailbee.dashboard.menus import MenuGroup, MenuItem
from jailbee.dashboard.overlays import Picker, PickerEntry
from jailbee.dashboard.settings import TABS, SettingsState, next_tab, setting_rows
from jailbee.dashboard.settings import Tab as SettingsTab
from jailbee.dashboard.tui.frame import help_lines
from jailbee.dashboard.tui.menu_state import (
    MenuState,
    RepoMenuState,
    menu_entries,
    menu_hotkeys,
    menu_option_text,
    menu_title,
    menu_width,
)
from jailbee.dashboard.tui.overlay import NativeState, Overlay, overlay_key

NATIVE_LIST_ID = "native-list"

# Every overlay list: no background but the hover, the cursor in CURSOR_STYLE
# (bold magenta), and the V2 scrollbar colours (else Textual's theme paints RGB).
_SCROLLBAR_CSS = """
    scrollbar-size-vertical: 1;
    scrollbar-size-horizontal: 0;
    scrollbar-background: ansi_default;
    scrollbar-background-hover: ansi_default;
    scrollbar-background-active: ansi_default;
    scrollbar-color: ansi_default;
    scrollbar-color-hover: ansi_default;
    scrollbar-color-active: ansi_default;
"""


def list_css(name: str) -> str:
    """The shared look of one OptionList subclass ``name``."""
    return f"""
    {name} {{
        width: 100%;
        height: auto;
        border: none;
        padding: 0;
        background: ansi_default;
        color: ansi_default;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        {_SCROLLBAR_CSS}
    }}
    {name}:focus {{ border: none; background-tint: initial; }}
    {name} > .option-list--option,
    {name} > .option-list--option-hover {{ background: ansi_default; color: ansi_default; }}
    {name} > .option-list--option-highlighted,
    {name}:focus > .option-list--option-highlighted {{
        background: ansi_default;
        color: ansi_magenta;
        text-style: bold;
    }}
    {name} > .option-list--option-disabled {{ background: ansi_default; text-style: dim; }}
    """


def _wheel(widget: Widget, event: events.MouseEvent, step: int, enabled: bool) -> None:
    """One line per notch (Textual's default is two); never the cursor."""
    event.stop()
    event.prevent_default()  # also skips the base classes' private scroll handlers
    if enabled and not event.shift:
        widget.scroll_relative(y=step, animate=False)


def _hovered(lst: OptionList, y: int, strip: Strip) -> Strip:
    """``strip`` painted with HOVER_STYLE when it is a line of the hovered option.

    Textual CSS cannot name the 256-colour grey the table hovers with, so the
    hover is painted here. Reads two private attributes of Textual 8.2.8's
    OptionList: ``_mouse_hovering_over`` (option index) and ``_lines``
    ((option index, line offset) per virtual line). The highlighted option
    keeps its own style, as Textual's precedence does.
    """
    hovered = lst._mouse_hovering_over
    if hovered is None or hovered == lst.highlighted:
        return strip
    line = lst.scroll_offset.y + y
    lines = lst._lines
    if 0 <= line < len(lines) and lines[line][0] == hovered:
        # post_style: the option's own (default) background would win over a plain apply_style
        return Strip(Segment.apply_style(strip, post_style=HOVER_STYLE), strip.cell_length)
    return strip


class OverlayList(OptionList, can_focus=True):
    """An overlay's list: j/k, one-line wheel, gated clicks, the dashboard's hover."""

    DEFAULT_CSS = list_css("OverlayList")
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(
        self,
        *options: Option,
        mouse_enabled: Callable[[], bool],
        double_click_chooses: bool = False,
    ) -> None:
        super().__init__(*options, id=NATIVE_LIST_ID)
        self.mouse_enabled = mouse_enabled
        self.double_click_chooses = double_click_chooses

    async def _on_click(self, event: events.Click) -> None:
        if not self.mouse_enabled():
            event.stop()
            event.prevent_default()
            return
        if not self.double_click_chooses:
            return  # OptionList's own handler highlights and chooses
        # Egress and accounts rows: one click highlights, the second chooses.
        event.prevent_default()
        index = event.style.meta.get("option")
        if isinstance(index, int) and not self.get_option_at_index(index).disabled:
            self.highlighted = index
            if event.chain >= 2:
                self.action_select()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        _wheel(self, event, 1, self.mouse_enabled())

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        _wheel(self, event, -1, self.mouse_enabled())

    def render_line(self, y: int) -> Strip:
        return _hovered(self, y, super().render_line(y))


class OverlayBox(Vertical):
    """One overlay in the bottom slot: a round border titled like the old panel."""

    DEFAULT_CSS = """
    OverlayBox {
        width: 1fr;
        height: auto;
        border: round ansi_default;
        border-title-align: left;
        border-title-style: bold;
        padding: 0 1;
        background: ansi_default;
        color: ansi_default;
    }
    """
    HEADER_ROWS: ClassVar[int] = 0

    class Cancelled(Message):
        """Esc at the box's root; the session decides where that lands."""

        def __init__(self, key: tuple[object, ...] | None) -> None:
            super().__init__()
            self.key = key

    class Changed(Message):
        """The box's content height or width changed (a menu level, a tab): lay out again."""

    def __init__(self, spec: Overlay, *, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__()
        self.spec: Overlay = spec
        self.mouse_enabled = mouse_enabled

    @property
    def key(self) -> tuple[object, ...] | None:
        return overlay_key(self.spec)

    def show(self, spec: Overlay) -> None:
        """New data for the same overlay (`overlay_key` unchanged)."""
        self.spec = spec

    def content_rows(self) -> int:
        """Lines the content wants, border and header excluded."""
        raise NotImplementedError

    def natural_width(self) -> int | None:
        """Cells including border and padding, or None to fill the slot."""
        return None

    def chrome_rows(self) -> int:
        return 2 + self.HEADER_ROWS

    def focus_target(self) -> Widget:
        return self.query_one(f"#{NATIVE_LIST_ID}")

    def state(self) -> NativeState:
        raise NotImplementedError

    def cancel(self) -> None:
        self.post_message(self.Cancelled(self.key))

    def _ready(self) -> None:
        """After mount, before focus: subclasses place their initial cursor here."""

    def on_mount(self) -> None:
        self._ready()
        self.focus_target().focus()


class HelpScroll(VerticalScroll, can_focus=True):
    DEFAULT_CSS = f"""
    HelpScroll {{
        height: auto;
        max-height: 100%;
        background: ansi_default;
        color: ansi_default;
        {_SCROLLBAR_CSS}
    }}
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up", show=False),
    ]

    def __init__(self, *children: Widget, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(*children, id=NATIVE_LIST_ID)
        self.mouse_enabled = mouse_enabled

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        _wheel(self, event, 1, self.mouse_enabled())

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        _wheel(self, event, -1, self.mouse_enabled())


class HelpBox(OverlayBox):
    """The key help; scrolls when the terminal is short."""

    HELP_WIDTH = 72
    # Border and padding take 4 cells, the scrollbar one more (it can appear).
    WRAP_WIDTH = HELP_WIDTH - 5

    def __init__(self, spec: Overlay, *, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self._lines = help_lines()
        self._rows = self._wrapped_rows()
        self.border_title = "keys"

    def compose(self) -> ComposeResult:
        yield HelpScroll(
            Static(Text.from_markup("\n".join(self._lines))), mouse_enabled=self.mouse_enabled
        )

    def _wrapped_rows(self) -> int:
        console = Console()
        return sum(
            max(1, len(Text.from_markup(line).wrap(console, self.WRAP_WIDTH)))
            for line in self._lines
        )

    def content_rows(self) -> int:
        """Screen rows of the help once its long lines wrap inside the box."""
        return self._rows

    def natural_width(self) -> int | None:
        return self.HELP_WIDTH

    def state(self) -> NativeState:
        return NativeState("help", None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            self.cancel()


def _one_line(label: str, style: str = "") -> Text:
    return Text(label, style=style, no_wrap=True, overflow="ellipsis")


class PickerBox(OverlayBox):
    """A short list to choose from; Esc, `q` and Ctrl-C cancel just this step."""

    class Chosen(Message):
        def __init__(self, key: tuple[object, ...] | None, entry: PickerEntry) -> None:
            super().__init__()
            self.key = key
            self.entry = entry

    def __init__(self, spec: Picker, *, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self.picker = spec
        self.border_title = _one_line(spec.title, "bold")

    def compose(self) -> ComposeResult:
        options = [Option(_one_line(entry.label)) for entry in self.picker.entries] or [
            Option(_one_line("(nothing to choose)", "dim"), disabled=True)
        ]
        yield OverlayList(*options, mouse_enabled=self.mouse_enabled)

    def content_rows(self) -> int:
        return max(1, len(self.picker.entries))

    def natural_width(self) -> int | None:
        widest = max((cell_len(e.label) for e in self.picker.entries), default=20)
        # border 2 + padding 2 + scrollbar 1; the title needs its own room in the border
        return max(widest + 5, cell_len(self.picker.title) + 6)

    def state(self) -> NativeState:
        return NativeState("picker", self.query_one(OverlayList).highlighted)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if 0 <= event.option_index < len(self.picker.entries):
            self.post_message(self.Chosen(self.key, self.picker.entries[event.option_index]))

    def on_key(self, event: events.Key) -> None:
        if event.key in ("escape", "q", "ctrl+c"):
            event.stop()
            event.prevent_default()
            self.cancel()


class MenuBox(OverlayBox):
    """An action menu: a `▸` group opens in place, Esc goes back a level, a key picks."""

    class Chosen(Message):
        """A leaf ``verb`` at ``group``/``index`` (the session keeps them for a way back)."""

        def __init__(
            self, key: tuple[object, ...] | None, verb: str, group: str | None, index: int
        ) -> None:
            super().__init__()
            self.key = key
            self.verb = verb
            self.group = group
            self.index = index

    def __init__(
        self, spec: MenuState | RepoMenuState, *, mouse_enabled: Callable[[], bool]
    ) -> None:
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self.menu = spec
        levels = {item.label for item in menu_entries(spec) if isinstance(item, MenuGroup)}
        # A start level the menu does not have opens at the root, cursor at the top.
        known = spec.start_group in levels
        self.group = spec.start_group if known else None
        self._start_index = spec.start_index if known or spec.start_group is None else 0
        # Esc from a level returns to that group's own row at the root.
        self._parent_index = next(
            (
                i
                for i, item in enumerate(menu_entries(spec))
                if isinstance(item, MenuGroup) and item.label == spec.start_group
            ),
            0,
        )
        self.border_title = _one_line(menu_title(spec, self.group), "bold")

    def _entries(self) -> Sequence[MenuItem]:
        return menu_entries(self.menu, self.group)

    def _options(self) -> list[Option]:
        entries = self._entries()
        return [
            Option(menu_option_text(item, key))
            for item, key in zip(entries, menu_hotkeys(entries), strict=True)
        ]

    def compose(self) -> ComposeResult:
        yield OverlayList(*self._options(), mouse_enabled=self.mouse_enabled)

    def _ready(self) -> None:
        last = max(0, len(self._entries()) - 1)
        self.query_one(OverlayList).highlighted = min(self._start_index, last)

    def _load(self, group: str | None, cursor: int) -> None:
        self.group = group
        lst = self.query_one(OverlayList)
        lst.clear_options()
        lst.add_options(self._options())
        lst.highlighted = cursor
        self.border_title = _one_line(menu_title(self.menu, group), "bold")
        self.post_message(self.Changed())

    def _choose(self, index: int) -> None:
        entries = self._entries()
        if not 0 <= index < len(entries):
            return
        item = entries[index]
        if isinstance(item, MenuGroup):
            self._parent_index = index
            self._load(item.label, 0)
        else:
            self.post_message(self.Chosen(self.key, item[1], self.group, index))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._choose(event.option_index)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            if self.group is None:
                self.cancel()
            else:
                self._load(None, self._parent_index)
            return
        keys = menu_hotkeys(self._entries())
        if event.character is not None and event.character in keys:
            # An entry's own key is Enter on that entry.
            event.stop()
            event.prevent_default()
            index = keys.index(event.character)
            self.query_one(OverlayList).highlighted = index
            self._choose(index)

    def content_rows(self) -> int:
        return max(1, len(self._entries()))

    def natural_width(self) -> int | None:
        return menu_width(self.menu) + 5  # border 2 + padding 2 + scrollbar 1

    def state(self) -> NativeState:
        return NativeState("menu", self.query_one(OverlayList).highlighted, level=self.group)


class SettingsTabs(Tabs, can_focus=False):
    """The tab row; clicked, never focused (the list keeps the keys)."""

    DEFAULT_CSS = """
    SettingsTabs { background: ansi_default; color: ansi_default; }
    SettingsTabs Tab { background: ansi_default; color: ansi_default; }
    SettingsTabs Tab.-active { text-style: bold reverse; }
    SettingsTabs Underline > .underline--bar { background: ansi_default; color: ansi_default; }
    """


class SettingsList(SelectionList[str], can_focus=True):
    """The checkbox rows: j/k, one-line wheel, gated clicks, the dashboard's hover."""

    DEFAULT_CSS = (
        list_css("SettingsList")
        + """
    SettingsList { height: 1fr; }  /* the room the tab row leaves; `auto` would overflow it */
    SettingsList > .selection-list--button,
    SettingsList > .selection-list--button-highlighted {
        background: ansi_default;
        color: ansi_default;
    }
    SettingsList > .selection-list--button-selected,
    SettingsList > .selection-list--button-selected-highlighted {
        background: ansi_default;
        color: ansi_green;
        text-style: bold;
    }
    """
    )
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(self, *selections: Selection[str], mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(*selections, id=NATIVE_LIST_ID)
        self.mouse_enabled = mouse_enabled

    async def _on_click(self, event: events.Click) -> None:
        if not self.mouse_enabled():
            event.stop()
            event.prevent_default()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        _wheel(self, event, 1, self.mouse_enabled())

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        _wheel(self, event, -1, self.mouse_enabled())

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        index = self.scroll_offset.y + y
        if 0 <= index < self.option_count and (
            self.get_option_at_index(index).value not in self._selected
        ):
            # The button always draws an "X" and shows its state by colour alone,
            # which NO_COLOR and a 16-colour terminal lose: an unchecked box is empty.
            segments = list(strip)
            if len(segments) > 1 and segments[1].text == Checkbox.BUTTON_INNER:
                segments[1] = Segment(" ", segments[1].style)
                strip = Strip(segments, strip.cell_length)
        return _hovered(self, y, strip)


class SettingsBox(OverlayBox):
    """Columns, folding, visibility: Space or a click toggles; Tab or a tab click switches."""

    HEADER_ROWS = 2  # the tab row and its underline
    WIDTH = 72

    class Toggled(Message):
        def __init__(self, key: tuple[object, ...] | None, tab: SettingsTab, row_key: str) -> None:
            super().__init__()
            self.key = key
            self.tab = tab
            self.row_key = row_key

    def __init__(self, spec: SettingsState, *, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self.settings = spec
        self.tab: SettingsTab = "fields"
        self.border_title = "settings"

    def _selections(self) -> list[Selection[str]]:
        return [
            Selection(_one_line(row.label), row.key, row.checked)
            for row in setting_rows(self.settings, self.tab)
        ]

    def compose(self) -> ComposeResult:
        yield SettingsTabs(*(Tab(label, id=tab) for tab, label in TABS), active=self.tab)
        yield SettingsList(*self._selections(), mouse_enabled=self.mouse_enabled)

    def _list(self) -> SettingsList:
        return self.query_one(SettingsList)

    def _switch(self, tab: SettingsTab) -> None:
        self.tab = tab
        lst = self._list()
        with lst.prevent(SelectionList.SelectedChanged, SelectionList.SelectionToggled):
            lst.clear_options()
            lst.add_options(self._selections())
        lst.highlighted = 0
        tabs = self.query_one(SettingsTabs)
        with tabs.prevent(Tabs.TabActivated):
            tabs.active = tab
        self.post_message(self.Changed())

    def show(self, spec: Overlay) -> None:
        """Re-sync every checkbox from the session (a refused toggle flips back)."""
        super().show(spec)
        assert isinstance(spec, SettingsState)
        self.settings = spec
        if not self.is_mounted:
            return
        lst = self._list()
        selected = set(lst.selected)
        with lst.prevent(SelectionList.SelectedChanged, SelectionList.SelectionToggled):
            for row in setting_rows(spec, self.tab):
                if row.checked and row.key not in selected:
                    lst.select(row.key)
                elif not row.checked and row.key in selected:
                    lst.deselect(row.key)

    def on_key(self, event: events.Key) -> None:
        if event.key in ("escape", "tab", "enter"):
            event.stop()
            event.prevent_default()
            if event.key == "escape":
                self.cancel()
            elif event.key == "tab":
                self._switch(next_tab(self.tab))

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        event.stop()
        tab = event.tab.id
        if tab in ("fields", "repos", "visibility") and tab != self.tab:
            self._switch(tab)  # type: ignore[arg-type]  # narrowed by the membership test above

    def on_selection_list_selection_toggled(
        self, event: SelectionList.SelectionToggled[str]
    ) -> None:
        event.stop()
        self.post_message(self.Toggled(self.key, self.tab, event.selection.value))

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged[str]) -> None:
        event.stop()

    def content_rows(self) -> int:
        return max(1, len(setting_rows(self.settings, self.tab)))

    def natural_width(self) -> int | None:
        return self.WIDTH

    def state(self) -> NativeState:
        return NativeState("settings", self._list().highlighted, tab=self.tab)


def build_box(spec: Overlay, *, mouse_enabled: Callable[[], bool]) -> OverlayBox:
    """The box for a native overlay (see `is_native`)."""
    if spec == "help":
        return HelpBox(spec, mouse_enabled=mouse_enabled)
    if isinstance(spec, Picker):
        return PickerBox(spec, mouse_enabled=mouse_enabled)
    if isinstance(spec, (MenuState, RepoMenuState)):
        return MenuBox(spec, mouse_enabled=mouse_enabled)
    if isinstance(spec, SettingsState):
        return SettingsBox(spec, mouse_enabled=mouse_enabled)
    raise ValueError(f"no native box for {spec!r}")
