"""Native overlay boxes: one bordered box per overlay kind.

The session decides what is open and what a choice does (it never sees a
cursor); a box shows its overlay's data, owns cursor, hover, scroll and focus,
and posts what the user did. `DashboardFrame` mounts the box for the session's
overlay, keeps it while `overlay_key` is unchanged, and sizes it from
`content_rows`/`natural_width` through `frame_layout`.

Keys: `DashboardApp` routes every key to the open box's `handle_key`, which
answers with an outcome message (applied before the next key is read),
`True` for a key it used, or `False` for one the dashboard keeps (`q`, `h`,
`S`, Ctrl-C in a list). Keys never travel Textual's focus chain, so a key
typed ahead lands in whatever the previous key opened. The mouse still
reaches boxes through Textual's events; those outcomes are posted.
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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.strip import Strip
from textual.suggester import Suggester
from textual.widget import Widget
from textual.widgets import Checkbox, Input, OptionList, SelectionList, Static, Tab, Tabs
from textual.widgets._tabs import Underline
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from jailbee.dashboard.accounts import AccountRow, AccountsState, account_lines
from jailbee.dashboard.commands import apply_completion
from jailbee.dashboard.egress import EgressState, egress_label
from jailbee.dashboard.hit import HOVER_STYLE
from jailbee.dashboard.menus import MenuGroup, MenuItem
from jailbee.dashboard.overlays import (
    SUGGESTION_ROWS,
    Picker,
    PickerEntry,
    TextPrompt,
    filter_suggestions,
)
from jailbee.dashboard.settings import CURSOR_STYLE, TABS, SettingsState, next_tab, setting_rows
from jailbee.dashboard.settings import Tab as SettingsTab
from jailbee.dashboard.tui.frame import help_lines
from jailbee.dashboard.tui.layout import BOX_INSET_COLS, FRAME_INSET_COLS
from jailbee.dashboard.tui.menu_state import (
    MenuState,
    RepoMenuState,
    menu_entries,
    menu_hotkeys,
    menu_option_text,
    menu_title,
    menu_width,
)
from jailbee.dashboard.tui.overlay import CommandState, NativeState, Overlay, overlay_key
from jailbee.egress_scope import EntryRow

NATIVE_LIST_ID = "native-list"

KeyOutcome = Message | bool
"""What a box made of a routed key: an outcome to apply, or whether the key was its own."""

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
    keeps its own style, as Textual's precedence does. Read with ``getattr``: a
    renamed attribute (this pins Textual 8.2.8) degrades to no hover paint.
    """
    hovered = getattr(lst, "_mouse_hovering_over", None)
    if hovered is None or hovered == lst.highlighted:
        return strip
    line = lst.scroll_offset.y + y
    lines = getattr(lst, "_lines", None) or []
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
        # Only the first click (or, for the double-click lists, the second) acts: a
        # repeat would land on whatever the first one opened under the pointer.
        if event.chain > (2 if self.double_click_chooses else 1):
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
            if event.chain == 2:
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

    # Widget.handle_key (key_* method dispatch, -> bool) is never reached: the app routes keys.
    # Every override below repeats the `type: ignore[override]` for the same reason.
    async def handle_key(self, event: events.Key) -> KeyOutcome:  # type: ignore[override]
        """A routed key: by default the binding the box's list has for it (↑/↓, j/k, PgUp…)."""
        return await _run_binding(self, event)

    def _ready(self) -> None:
        """After mount, before focus: subclasses place their initial cursor here."""

    def on_mount(self) -> None:
        self._ready()
        self.focus_target().focus()


async def _run_binding(box: OverlayBox, event: events.Key) -> bool:
    """Run the binding a widget inside ``box`` has for ``event``, as Textual would.

    Looked up through ``screen.active_bindings`` (the focus chain, nearest
    first), keeping only nodes inside the box: the app's and screen's own
    bindings (focus cycling, copy) never apply to a box key.
    """
    active = box.screen.active_bindings
    for key in event.aliases:
        found = active.get(key)
        if found is not None and box in found.node.ancestors_with_self:
            await box.app.run_action(found.binding.action, found.node)
            return True
    return False


INPUT_ID = "native-input"
_CANCEL_KEYS = frozenset({"escape", "ctrl+c"})
# Enter's control aliases: the old cbreak loop took a bare LF (Ctrl-J) as Enter.
_SUBMIT_KEYS = frozenset({"ctrl+j", "ctrl+m"})


class OverlayInput(Input):
    """One line of typed text: no select-all on focus, no blink, the dashboard's colours."""

    DEFAULT_CSS = """
    OverlayInput, OverlayInput:focus {
        width: 1fr;
        background: ansi_default;
        color: ansi_default;
        background-tint: initial;
    }
    OverlayInput > .input--cursor, OverlayInput:ansi > .input--cursor,
    OverlayInput > .input--selection, OverlayInput:ansi > .input--selection {
        background: ansi_default;
        color: ansi_default;
        text-style: reverse;
    }
    OverlayInput > .input--suggestion, OverlayInput:ansi > .input--suggestion {
        background: ansi_default;
        color: ansi_default;
        text-style: dim;
    }
    """

    def __init__(self, value: str = "", *, suggester: Suggester | None = None) -> None:
        super().__init__(
            value, suggester=suggester, select_on_focus=False, compact=True, id=INPUT_ID
        )
        self.cursor_blink = False


class TextBox(OverlayBox):
    """A box answered by typing.

    Every key reaches `handle_key` from the app, in order: printable keys are
    text, Esc and Ctrl-C cancel just this input, Enter (and Ctrl-J/Ctrl-M)
    answers, Tab is `complete`, and every other key runs the input's own
    binding (←/→, Home/End, Ctrl-A/E/W/U/K, selection).
    """

    DEFAULT_CSS = """
    TextBox > .line { height: 1; background: ansi_default; color: ansi_default; }
    TextBox .input-mark { width: 2; background: ansi_default; color: ansi_default; }
    """

    def __init__(self, spec: Overlay, *, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self._seen: str | None = None  # the text the box's lists and error answer

    @property
    def input(self) -> OverlayInput:
        return self.query_one(OverlayInput)

    def focus_target(self) -> Widget:
        return self.input

    async def handle_key(self, event: events.Key) -> KeyOutcome:  # type: ignore[override]  # see OverlayBox.handle_key
        key = event.key
        if key in _CANCEL_KEYS:
            return self.Cancelled(self.key)
        if key == "enter" or key in _SUBMIT_KEYS:
            return self.answer()
        if key in ("tab", "shift+tab"):
            self.complete()
        elif key == "ctrl+h":
            self.input.action_delete_left()
        elif self._own_key(key):
            return True  # a move, not an edit: the text is as `_seen` has it
        else:
            if event.is_printable:
                assert event.character is not None
                self.type_text(event.character)
            else:
                await _run_binding(self, event)
        self.text_changed()
        return True

    def type_text(self, text: str) -> None:
        """Type ``text`` at the cursor, over the selection if there is one (as `Input` does)."""
        selection = self.input.selection
        if selection.is_empty:
            self.input.insert_text_at_cursor(text)
        else:
            self.input.replace(text, *selection)

    def paste(self, text: str) -> None:
        """Every line of a paste, joined; a chunk with a control character is dropped whole."""
        joined = "".join(text.strip("\r\n").splitlines())
        if joined and joined.isprintable():
            self.type_text(joined)
            self.text_changed()

    def text_changed(self) -> None:
        """Bring the box's lists and error up to the input's text, once per text."""
        value = self.input.value
        if value != self._seen:
            self._seen = value
            self._edited(value)

    def on_input_changed(self, event: Input.Changed) -> None:
        # Every edit already went through `text_changed`; this only catches up on one
        # that did not (none today) and is a no-op otherwise.
        event.stop()
        self.text_changed()

    def _edited(self, value: str) -> None:
        """The text became ``value``: refresh what depends on it."""

    def _own_key(self, key: str) -> bool:
        """A key this kind of box gives its own meaning (a choice prompt's ↑/↓)."""
        return False

    def answer(self) -> Message:
        """Enter: the outcome that answers this box."""
        raise NotImplementedError

    def complete(self) -> None:
        """Tab: complete the answer if the box can."""


def _no_candidates(_text: str) -> tuple[str, ...]:
    return ()


class _CommandSuggester(Suggester):
    """The ghost completion of the `!` line: only when exactly one candidate fits."""

    def __init__(self, box: CommandBox) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.box = box

    async def get_suggestion(self, value: str) -> str | None:
        candidates = self.box.candidates_for(value)
        if len(candidates) != 1:
            return None
        done = apply_completion(value, candidates[0])
        return done if done != value and done.startswith(value) else None


class CommandBox(TextBox):
    """The `!` line: the completions are listed under it, Tab cycles them, Enter runs it."""

    class Submitted(Message):
        def __init__(self, key: tuple[object, ...] | None, text: str) -> None:
            super().__init__()
            self.key = key
            self.text = text

    def __init__(
        self,
        spec: CommandState,
        *,
        mouse_enabled: Callable[[], bool],
        candidates: Callable[[str], tuple[str, ...]],
    ) -> None:
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self._candidates_of = candidates
        self._memo: tuple[str, tuple[str, ...]] | None = None
        self._base = ""  # the text the listed candidates complete
        self._shown: tuple[str, ...] = ()
        self._index = -1  # the Tab-cycled candidate; -1 while none is
        self.border_title = "command"

    def compose(self) -> ComposeResult:
        with Horizontal(classes="line"):
            yield Static("> ", classes="input-mark")
            yield OverlayInput(suggester=_CommandSuggester(self))
        yield Static(id="command-candidates", classes="line")

    def _ready(self) -> None:
        self.query_one("#command-candidates", Static).display = False

    def candidates_for(self, text: str) -> tuple[str, ...]:
        """The session's completions for ``text``.

        The one-entry cache keyed on the text only dedups the suggester's call for the
        same edit (it and `_edited` both ask); it must not be widened, or a
        session whose candidates changed under the same text would answer stale.
        """
        if self._memo is None or self._memo[0] != text:
            self._memo = (text, self._candidates_of(text))
        return self._memo[1]

    def _list(self, shown: tuple[str, ...]) -> None:
        before = self.content_rows()
        self._shown = shown
        line = Text("  ", no_wrap=True, overflow="ellipsis")
        for i, candidate in enumerate(shown):
            if i:
                line.append("   ")
            line.append(candidate, style=CURSOR_STYLE if i == self._index else "")
        widget = self.query_one("#command-candidates", Static)
        widget.update(line)
        widget.display = bool(shown)
        if self.content_rows() != before:
            self.post_message(self.Changed())

    def _edited(self, value: str) -> None:
        if 0 <= self._index < len(self._shown) and value == apply_completion(
            self._base, self._shown[self._index]
        ):
            return  # the box's own Tab completion, not an edit
        self._base, self._index = value, -1
        self._list(self.candidates_for(value))

    def complete(self) -> None:
        if not self._shown:
            return
        self._index = (self._index + 1) % len(self._shown)
        completed = apply_completion(self._base, self._shown[self._index])
        self.input.value = completed
        self.input.cursor_position = len(completed)
        self._list(self._shown)  # repaint the marked candidate

    def answer(self) -> Message:
        return self.Submitted(self.key, self.input.value)

    def content_rows(self) -> int:
        return 1 + int(bool(self._shown))

    def state(self) -> NativeState:
        return NativeState(
            "command",
            self._index if self._index >= 0 else None,
            text=self.input.value,
            matches=self._shown,
        )


class SuggestionList(OverlayList, can_focus=False):
    """A choice prompt's matches: the input keeps the focus and moves this highlight."""

    DEFAULT_CSS = list_css("SuggestionList") + "SuggestionList { height: 1fr; }"


class _PrefixSuggester(Suggester):
    """The ghost answer: the first listed suggestion that starts with the text."""

    def __init__(self, suggestions: tuple[str, ...]) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self.suggestions = suggestions

    async def get_suggestion(self, value: str) -> str | None:
        return next((s for s in self.suggestions if s != value and s.startswith(value)), None)


def _one_line(label: str, style: str = "") -> Text:
    return Text(label, style=style, no_wrap=True, overflow="ellipsis")


class PromptBox(TextBox):
    """A question: label, input, for a typed choice the matches, and the refusal."""

    class Submitted(Message):
        def __init__(self, key: tuple[object, ...] | None, text: str) -> None:
            super().__init__()
            self.key = key
            self.text = text

    def __init__(self, spec: TextPrompt, *, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self.prompt = spec
        self._matches = filter_suggestions(spec.suggestions, spec.initial)
        self._error: str | None = None
        self._error_for: str | None = None  # the text the error answers
        self.border_title = _one_line(spec.title, "bold")

    def _options(self) -> list[Option]:
        return [Option(_one_line(m)) for m in self._matches] or [
            Option(_one_line("(no matching branch)", "dim"), disabled=True)
        ]

    def compose(self) -> ComposeResult:
        yield Static(_one_line(self.prompt.label), classes="line")
        with Horizontal(classes="line"):
            yield Static("> ", classes="input-mark")
            yield OverlayInput(
                self.prompt.initial,
                suggester=_PrefixSuggester(self.prompt.suggestions)
                if self.prompt.suggestions
                else None,
            )
        if self.prompt.suggestions:
            yield SuggestionList(*self._options(), mouse_enabled=self.mouse_enabled)
        yield Static(id="prompt-error", classes="line")

    def _list(self) -> SuggestionList | None:
        return self.query_one(SuggestionList) if self.prompt.suggestions else None

    def _ready(self) -> None:
        self.query_one("#prompt-error", Static).display = False
        lst = self._list()
        if lst is not None:
            lst.highlighted = None  # OptionList highlights its first option on its own

    def _highlight(self) -> int | None:
        lst = self._list()
        return lst.highlighted if lst is not None and self._matches else None

    def _edited(self, value: str) -> None:
        before = self.content_rows()
        if self._error is not None and value != self._error_for:
            self._error = None
            self.query_one("#prompt-error", Static).display = False
        lst = self._list()
        if lst is not None:
            matches = filter_suggestions(self.prompt.suggestions, value)
            if matches != self._matches:
                self._matches = matches
                lst.clear_options()
                lst.add_options(self._options())
            lst.highlighted = None  # any edit drops the highlight
        if self.content_rows() != before:
            self.post_message(self.Changed())

    def _own_key(self, key: str) -> bool:
        lst = self._list()
        if lst is None or key not in ("up", "down"):
            return False
        if self._matches:
            current = lst.highlighted
            if key == "down":
                lst.highlighted = 0 if current is None else min(current + 1, len(self._matches) - 1)
            else:
                lst.highlighted = None if current is None or current == 0 else current - 1
        return True

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()  # a click on a match: choose it, as Enter on it would
        lst = self._list()
        if lst is not None and 0 <= event.option_index < len(self._matches):
            lst.highlighted = event.option_index
            self.post_message(self.answer())

    def complete(self) -> None:
        if not self._matches:
            return
        current = self._highlight()
        chosen = self._matches[current if current is not None else 0]
        self.input.value = chosen
        self.input.cursor_position = len(chosen)

    def answer(self) -> Message:
        current = self._highlight()
        text = self._matches[current] if current is not None else self.input.value
        if text != self.input.value:
            self.input.value = text
            self.input.cursor_position = len(text)
            self.text_changed()
        return self.Submitted(self.key, text)

    def show_error(self, error: str) -> None:
        """The session refused the answer: say why until the text changes."""
        before = self.content_rows()
        self._error, self._error_for = error, self.input.value
        line = self.query_one("#prompt-error", Static)
        line.update(Text(error, style="red", no_wrap=True, overflow="ellipsis"))
        line.display = True
        if self.content_rows() != before:
            self.post_message(self.Changed())

    def content_rows(self) -> int:
        rows = 2 + int(self._error is not None)
        if self.prompt.suggestions:
            rows += min(max(1, len(self._matches)), SUGGESTION_ROWS)
        return rows

    def state(self) -> NativeState:
        return NativeState(
            "prompt",
            self._highlight(),
            text=self.input.value,
            error=self._error,
            matches=tuple(self._matches),
        )


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

    async def handle_key(self, event: events.Key) -> KeyOutcome:  # type: ignore[override]
        if event.key == "escape":
            return self.Cancelled(self.key)
        return await super().handle_key(event)


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

    def _chosen(self, index: int | None) -> PickerBox.Chosen | None:
        if index is not None and 0 <= index < len(self.picker.entries):
            return self.Chosen(self.key, self.picker.entries[index])
        return None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        chosen = self._chosen(event.option_index)
        if chosen is not None:
            self.post_message(chosen)

    async def handle_key(self, event: events.Key) -> KeyOutcome:  # type: ignore[override]
        if event.key in ("escape", "q", "ctrl+c"):
            return self.Cancelled(self.key)
        if event.key == "enter":
            return self._chosen(self.query_one(OverlayList).highlighted) or True
        return await super().handle_key(event)


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

    def _choose(self, index: int | None) -> MenuBox.Chosen | None:
        entries = self._entries()
        if index is None or not 0 <= index < len(entries):
            return None
        item = entries[index]
        if isinstance(item, MenuGroup):
            self._parent_index = index
            self._load(item.label, 0)
            return None
        return self.Chosen(self.key, item[1], self.group, index)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        chosen = self._choose(event.option_index)
        if chosen is not None:
            self.post_message(chosen)

    async def handle_key(self, event: events.Key) -> KeyOutcome:  # type: ignore[override]
        lst = self.query_one(OverlayList)
        if event.key == "escape":
            if self.group is None:
                return self.Cancelled(self.key)
            self._load(None, self._parent_index)
            return True
        if event.key == "enter":
            return self._choose(lst.highlighted) or True
        keys = menu_hotkeys(self._entries())
        if event.character is not None and event.character in keys:
            # An entry's own key is Enter on that entry.
            index = keys.index(event.character)
            lst.highlighted = index
            return self._choose(index) or True
        return await super().handle_key(event)

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
    /* Textual's own `Tab:ansi.-active` sets `not dim bold`; this outranks it */
    SettingsTabs Tab:ansi.-active { text-style: not dim bold reverse; }
    SettingsTabs Underline > .underline--bar { background: ansi_default; color: ansi_default; }
    """

    def __init__(self, *tabs: Tab, active: str, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(*tabs, active=active)
        self.mouse_enabled = mouse_enabled

    # Tab.Clicked and Underline.Clicked bubble here; with the mouse off neither switches a tab.
    async def _on_tab_clicked(self, event: Tab.Clicked) -> None:
        if not self.mouse_enabled():
            event.stop()
            event.prevent_default()

    def _on_underline_clicked(self, event: Underline.Clicked) -> None:
        if not self.mouse_enabled():
            event.stop()
            event.prevent_default()


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
        if not self.mouse_enabled() or event.chain > 1:  # a double click must not toggle twice
            event.stop()
            event.prevent_default()

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        _wheel(self, event, 1, self.mouse_enabled())

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        _wheel(self, event, -1, self.mouse_enabled())

    def render_line(self, y: int) -> Strip:
        # Textual 8.2.8 internals: `_selected` (selected values) and the button drawn as
        # segments [left, inner, right, ...] with `Checkbox.BUTTON_INNER` at index 1.
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
        yield SettingsTabs(
            *(Tab(label, id=tab) for tab, label in TABS),
            active=self.tab,
            mouse_enabled=self.mouse_enabled,
        )
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

    async def handle_key(self, event: events.Key) -> KeyOutcome:  # type: ignore[override]
        key = event.key
        if key == "escape":
            return self.Cancelled(self.key)
        if key == "tab":
            self._switch(next_tab(self.tab))
            return True
        if key == "enter":
            return True  # SelectionList would toggle on Enter too; only Space does here
        if key == "space":
            lst = self._list()
            index = lst.highlighted
            if index is None:
                return True
            return self.Toggled(self.key, self.tab, lst.get_option_at_index(index).value)
        return await super().handle_key(event)

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


class EgressBox(OverlayBox):
    """One scope's egress overrides: `a` adds, `r` removes the highlighted one."""

    class Add(Message):
        def __init__(self, key: tuple[object, ...] | None, index: int) -> None:
            super().__init__()
            self.key = key
            self.index = index

    class Remove(Message):
        def __init__(self, key: tuple[object, ...] | None, row: EntryRow) -> None:
            super().__init__()
            self.key = key
            self.row = row

    def __init__(self, spec: EgressState, *, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self.panel = spec
        scope = "repo" if spec.container is None else f"container {spec.container}"
        self.border_title = _one_line(f"Egress · {scope}")

    def _options(self) -> list[Option]:
        return [Option(egress_label(self.panel, row)) for row in self.panel.rows] or [
            Option(_one_line("No egress entries in this scope."), disabled=True)
        ]

    def compose(self) -> ComposeResult:
        yield OverlayList(
            *self._options(), mouse_enabled=self.mouse_enabled, double_click_chooses=True
        )

    def _ready(self) -> None:
        if self.panel.rows:
            self.query_one(OverlayList).highlighted = min(
                self.panel.start_index, len(self.panel.rows) - 1
            )

    def _cursor(self) -> int | None:
        return self.query_one(OverlayList).highlighted if self.panel.rows else None

    def show(self, spec: Overlay) -> None:
        """Reloaded rows keep the cursor on the same entry and source when it is still listed."""
        super().show(spec)
        assert isinstance(spec, EgressState)
        old, self.panel = self.panel, spec
        if not self.is_mounted or spec.rows == old.rows:
            return
        lst = self.query_one(OverlayList)
        cursor = lst.highlighted if old.rows else None  # still the old options' cursor
        keep = old.rows[cursor] if cursor is not None and cursor < len(old.rows) else None
        lst.clear_options()
        lst.add_options(self._options())
        if spec.rows:
            lst.highlighted = next(
                (
                    i
                    for i, row in enumerate(spec.rows)
                    if keep is not None and (row.entry, row.source) == (keep.entry, keep.source)
                ),
                min(cursor or 0, len(spec.rows) - 1),
            )
        self.post_message(self.Changed())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()  # Enter on a row does nothing, as before

    async def handle_key(self, event: events.Key) -> KeyOutcome:  # type: ignore[override]
        key = event.key
        if key == "escape":
            return self.Cancelled(self.key)
        if key == "a":
            return self.Add(self.key, self._cursor() or 0)
        if key == "r":
            cursor = self._cursor()
            return self.Remove(self.key, self.panel.rows[cursor]) if cursor is not None else True
        if key == "enter":
            return True  # Enter on a row does nothing, as before
        return await super().handle_key(event)

    def content_rows(self) -> int:
        return max(1, len(self.panel.rows))

    def state(self) -> NativeState:
        return NativeState("egress", self._cursor())


class AccountsBox(OverlayBox):
    """Credential groups and stored logins: Enter acts on a row, `n` creates a group."""

    DEFAULT_CSS = """
    AccountsBox > #accounts-header { height: auto; background: ansi_default; color: ansi_default; }
    AccountsBox > OverlayList { height: 1fr; }  /* the room the header leaves, not its own height */
    """

    class Chosen(Message):
        def __init__(self, key: tuple[object, ...] | None, row: AccountRow, index: int) -> None:
            super().__init__()
            self.key = key
            self.row = row
            self.index = index

    class NewGroup(Message):
        def __init__(self, key: tuple[object, ...] | None, index: int) -> None:
            super().__init__()
            self.key = key
            self.index = index

    def __init__(self, spec: AccountsState, *, mouse_enabled: Callable[[], bool]) -> None:
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self.panel = spec
        self._width = 0
        self.border_title = "credential groups and logins"

    def _lay_out(self, width: int) -> tuple[Text, list[Option]]:
        """The header text and the options for the panel's rows at content ``width``."""
        if not self.panel.rows:
            placeholder = _one_line("(no logins or groups on this host)", "dim")
            return Text(), [Option(placeholder, disabled=True)]
        head, lines = account_lines(self.panel.rows, width - 1)  # the scrollbar's column
        return Text("\n").join(head), [Option(line) for line in lines]

    def compose(self) -> ComposeResult:
        # The frame gives the box the whole slot, so its content width is known now;
        # on_resize corrects it when it is not (a box shown on its own, a tiny screen).
        self._width = max(1, self.app.size.width - FRAME_INSET_COLS - BOX_INSET_COLS)
        header, options = self._lay_out(self._width)
        yield Static(header, id="accounts-header")
        yield OverlayList(*options, mouse_enabled=self.mouse_enabled, double_click_chooses=True)

    def chrome_rows(self) -> int:
        return 2 + (2 if self.panel.rows else 0)

    def _rebuild(self, width: int, cursor: int) -> None:
        """Lay the rows out again at ``width`` with the cursor on row ``cursor``."""
        self._width = width
        header, options = self._lay_out(width)
        self.query_one("#accounts-header", Static).update(header)
        self.query_one("#accounts-header", Static).display = bool(self.panel.rows)
        lst = self.query_one(OverlayList)
        lst.clear_options()
        lst.add_options(options)
        if self.panel.rows:
            lst.highlighted = min(cursor, len(self.panel.rows) - 1)

    def _cursor(self) -> int | None:
        return self.query_one(OverlayList).highlighted if self.panel.rows else None

    def show(self, spec: Overlay) -> None:
        """Reloaded rows keep the cursor on the same login when it is still listed."""
        super().show(spec)
        assert isinstance(spec, AccountsState)
        old, self.panel = self.panel, spec
        if not self.is_mounted or spec.rows == old.rows:
            return
        cursor = self.query_one(OverlayList).highlighted if old.rows else None
        keep = old.rows[cursor] if cursor is not None and cursor < len(old.rows) else None
        self._rebuild(
            self._width,
            next(
                (i for i, row in enumerate(spec.rows) if row == keep),
                min(cursor or 0, max(0, len(spec.rows) - 1)),
            ),
        )
        self.post_message(self.Changed())

    def _ready(self) -> None:
        self.query_one("#accounts-header", Static).display = bool(self.panel.rows)
        if self.panel.rows:
            self.query_one(OverlayList).highlighted = min(
                self.panel.start_index, len(self.panel.rows) - 1
            )

    def on_resize(self, event: events.Resize) -> None:
        width = self.content_size.width
        if width > 0 and width != self._width:
            self._rebuild(width, self._cursor() or 0)

    def _chosen(self, index: int | None) -> AccountsBox.Chosen | None:
        if index is not None and 0 <= index < len(self.panel.rows):
            return self.Chosen(self.key, self.panel.rows[index], index)
        return None

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        chosen = self._chosen(event.option_index)
        if chosen is not None:
            self.post_message(chosen)

    async def handle_key(self, event: events.Key) -> KeyOutcome:  # type: ignore[override]
        if event.key == "escape":
            return self.Cancelled(self.key)
        if event.key == "n":
            return self.NewGroup(self.key, self._cursor() or 0)
        if event.key == "enter":
            return self._chosen(self._cursor()) or True
        return await super().handle_key(event)

    def content_rows(self) -> int:
        return max(1, len(self.panel.rows))

    def state(self) -> NativeState:
        return NativeState("accounts", self._cursor())


def build_box(
    spec: Overlay,
    *,
    mouse_enabled: Callable[[], bool],
    candidates: Callable[[str], tuple[str, ...]] = _no_candidates,
) -> OverlayBox:
    """The box for ``spec``: every overlay is drawn by a native box."""
    if spec == "help":
        return HelpBox(spec, mouse_enabled=mouse_enabled)
    if isinstance(spec, Picker):
        return PickerBox(spec, mouse_enabled=mouse_enabled)
    if isinstance(spec, (MenuState, RepoMenuState)):
        return MenuBox(spec, mouse_enabled=mouse_enabled)
    if isinstance(spec, SettingsState):
        return SettingsBox(spec, mouse_enabled=mouse_enabled)
    if isinstance(spec, EgressState):
        return EgressBox(spec, mouse_enabled=mouse_enabled)
    if isinstance(spec, AccountsState):
        return AccountsBox(spec, mouse_enabled=mouse_enabled)
    if isinstance(spec, TextPrompt):
        return PromptBox(spec, mouse_enabled=mouse_enabled)
    if isinstance(spec, CommandState):
        return CommandBox(spec, mouse_enabled=mouse_enabled, candidates=candidates)
    raise ValueError(f"no native box for {spec!r}")
