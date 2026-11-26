"""Modal screens for the questions a running dashboard action asks.

One screen per prompt kind of the prompt channel (`jailbee.prompt_channel`):
confirm, choose one, choose many, text and masked secret. Every screen
dismisses with the *wire value* of its kind (bool, option id, list of option
ids, str) or ``None``. Entry questions also support Later, a distinct sentinel
that leaves the command waiting; Ctrl-C cancels. Without an entry, Esc still
cancels. The output screen shows an action's captured output.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Button, Input, OptionList, SelectionList, Static
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from jailbee.dashboard.notifications import QueueEntry
from jailbee.prompt_channel import Prompt, WireOption

_CSS = """
PromptModal { align: center middle; background: ansi_default 40%; }
PromptModal > .dialog {
    width: 72; max-width: 95%; height: auto; max-height: 90%;
    border: round ansi_blue; background: ansi_default; padding: 0 1;
}
PromptModal.destructive > .dialog { border: heavy ansi_red; }
PromptModal .title { text-style: bold; width: 100%; }
PromptModal.destructive .title { color: ansi_red; }
PromptModal .context { color: ansi_bright_black; }
PromptModal .hint { color: ansi_bright_black; }
PromptModal .error { color: ansi_red; }
PromptModal .detail { margin: 1 0; }
PromptModal OptionList, PromptModal SelectionList { height: auto; max-height: 16; }
PromptModal Input { margin: 1 0 0 0; }
PromptModal Horizontal { height: auto; }
PromptModal Button { min-width: 0; }
OutputScreen { align: center middle; background: ansi_default 40%; }
OutputScreen > .dialog {
    width: 95%; height: 90%; border: round ansi_blue; background: ansi_default; padding: 0 1;
}
OutputScreen .title { text-style: bold; }
OutputScreen.failed .title { color: ansi_red; }
OutputScreen VerticalScroll { height: 1fr; }
"""


def _row_text(option: WireOption) -> Text:
    """An option's row as the terminal shows it: the title, then its description.

    `title` is the row the terminal prints; `label` is only the short name used
    in errors, and stands in when a peer sent no title."""
    row = option.title or option.label
    return Text(row + (f"  {option.description}" if option.description else ""))


LATER = object()


class PromptModal(ModalScreen[Any]):
    """Base: title, optional repo/container context, cancel on Esc / Ctrl-C."""

    DEFAULT_CSS = _CSS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "later", "Later", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", priority=True),
    ]
    hint = "Enter confirms, Esc cancels"

    def __init__(self, prompt: Prompt) -> None:
        super().__init__()
        self.prompt = prompt
        self.entry: QueueEntry | None = None
        if prompt.destructive:
            self.add_class("destructive")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_later(self) -> None:
        self.dismiss(LATER if self.entry is not None else None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "later":
            self.action_later()
        elif event.button.id == "abort-command":
            self.action_cancel()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            if self.entry is not None:
                yield Static(
                    Text(f"{self.entry.command}  |  {self.entry.target}"), classes="context"
                )
                if self.entry.output:
                    yield Static(
                        Text.from_ansi("\n".join(self.entry.output.splitlines()[-3:])),
                        classes="context",
                    )
            yield Static(Text(self.heading()), classes="title", markup=False)
            context = self.prompt.context
            if context is not None and (context.repo or context.container):
                where = " / ".join(part for part in (context.repo, context.container) if part)
                yield Static(Text(where), classes="context", markup=False)
            if self.prompt.detail:
                yield Static(Text(self.prompt.detail), classes="detail", markup=False)
            yield from self.body()
            if self.prompt.error:
                yield Static(Text(self.prompt.error), classes="error", markup=False)
            if self.entry is not None:
                with Horizontal():
                    yield Button("Later", id="later")
                    yield Button("Abort command", id="abort-command", variant="error")
                yield Static("Esc: Later, Ctrl-C: Abort command", classes="hint")
            else:
                yield Static(self.hint, classes="hint", markup=False)

    def heading(self) -> str:
        return self.prompt.question

    def body(self) -> ComposeResult:
        yield from ()


class ConfirmModal(PromptModal):
    """No / Yes rows. Destructive questions start with "No" highlighted."""

    hint = "y / n, Up/Down move, Enter chooses, Esc cancels"

    def heading(self) -> str:
        return ("DESTRUCTIVE: " if self.prompt.destructive else "") + self.prompt.question

    def body(self) -> ComposeResult:
        yield OptionList(Option("No", id="no"), Option("Yes", id="yes"), compact=True)

    def on_mount(self) -> None:
        # Exit may prune the dialog while its screen is still mounting.
        for widget in self.query(OptionList):
            widget.highlighted = int(self.prompt.default is True and not self.prompt.destructive)
            widget.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option_id == "yes")

    def on_key(self, event: events.Key) -> None:
        if event.key in ("y", "n"):
            event.stop()
            self.dismiss(event.key == "y")
        elif event.key in ("left", "right", "tab", "shift+tab"):
            event.stop()
            event.prevent_default()
            choices = self.query_one(OptionList)
            choices.highlighted = 0 if choices.highlighted == 1 else 1


class ChooseOneModal(PromptModal):
    """A list; Enter chooses the highlighted option (its id)."""

    hint = "Up/Down move, Enter chooses, Esc cancels"

    def body(self) -> ComposeResult:
        yield OptionList(*[Option(_row_text(o), id=o.id) for o in self.prompt.options])

    def on_mount(self) -> None:
        for widget in self.query(OptionList):
            default = self.prompt.default
            if isinstance(default, str):
                for index, option in enumerate(self.prompt.options):
                    if option.id == default:
                        widget.highlighted = index
                        break
            if widget.highlighted is None and self.prompt.options:
                widget.highlighted = 0
            widget.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(event.option_id)


class ChooseManyModal(PromptModal):
    """Checkboxes; Space toggles, Enter confirms the ticked ids (none is valid)."""

    hint = "Space toggles, Enter confirms, Esc cancels"

    def body(self) -> ComposeResult:
        ticked = self.prompt.default if isinstance(self.prompt.default, tuple) else ()
        yield SelectionList[str](
            *[Selection(_row_text(o), o.id, o.id in ticked) for o in self.prompt.options]
        )

    def on_mount(self) -> None:
        self.query(SelectionList).focus()

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            chosen = set(self.query_one(SelectionList).selected)
            # Wire order is free; keep the options' own order.
            self.dismiss([o.id for o in self.prompt.options if o.id in chosen])


class TextModal(PromptModal):
    """A line of text, masked for a secret."""

    def __init__(self, prompt: Prompt) -> None:
        super().__init__(prompt)
        self.secret = prompt.kind == "secret"
        if prompt.clear_shortcut:
            self.hint = "Enter confirms, Ctrl-R clears, Esc cancels"

    def body(self) -> ComposeResult:
        default = self.prompt.default
        suggester = (
            SuggestFromList(self.prompt.completions, case_sensitive=True)
            if self.prompt.completions and not self.secret
            else None
        )
        yield Input(
            value=default if isinstance(default, str) and not self.secret else "",
            password=self.secret,
            suggester=suggester,
            id="answer",
        )
        yield Static("", classes="error", id="problem", markup=False)

    def on_mount(self) -> None:
        self.query(Input).focus()

    def on_key(self, event: events.Key) -> None:
        # The terminal's Ctrl-R: the only way to answer "" under `clear_shortcut`.
        if event.key == "ctrl+r" and self.prompt.clear_shortcut:
            event.stop()
            self.dismiss("")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        # As at the terminal: an empty answer is an ordinary answer (the command's
        # validator decides), except where Ctrl-R exists to say "clear" and a
        # blank line is blocked.
        if self.prompt.clear_shortcut and not event.value.strip():
            self.query_one("#problem", Static).update("Type an answer, or Ctrl-R to clear")
            return
        self.dismiss(event.value)


class OutputScreen(ModalScreen[None]):
    """The captured output of an action, scrollable; any dismiss key returns."""

    DEFAULT_CSS = _CSS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close", priority=True),
        Binding("enter", "close", "Close", priority=True),
        Binding("q", "close", "Close", priority=True),
        Binding("ctrl+c", "close", "Close", priority=True),
        Binding("b", "background", "Continue in background", priority=True),
    ]

    def __init__(
        self,
        title: str,
        text: str,
        *,
        failed: bool = False,
        background: bool = False,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.text = text
        self.background = background
        if failed:
            self.add_class("failed")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text), classes="title", markup=False)
            with VerticalScroll():
                yield Static(Text.from_ansi(self.text), id="captured", markup=False)
            hint = "Up/Down scroll, Enter or Esc returns"
            if self.background:
                hint = "Up/Down scroll, b, Enter or Esc continues in the background"
            yield Static(hint, classes="hint", markup=False)

    def set_text(self, text: str) -> None:
        """Replace the captured text (a live entry's output); stay at the end if it was there."""
        self.text = text
        if not self.is_mounted:
            return
        scroll = self.query_one(VerticalScroll)
        at_end = scroll.scroll_y >= scroll.max_scroll_y - 1
        self.query_one("#captured", Static).update(Text.from_ansi(text))
        if at_end:
            self.call_after_refresh(scroll.scroll_end, animate=False)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_background(self) -> None:
        """Continue in background: leave; the owner of the screen sees the close."""
        if self.background:
            self.dismiss(None)


def modal_for(prompt: Prompt, *, entry: QueueEntry | None = None) -> PromptModal:
    """The screen for ``prompt``'s kind."""
    kinds: dict[str, type[PromptModal]] = {
        "confirm": ConfirmModal,
        "choose_one": ChooseOneModal,
        "choose_many": ChooseManyModal,
        "text": TextModal,
        "secret": TextModal,
    }
    modal = kinds[prompt.kind](prompt)
    modal.entry = entry
    return modal
