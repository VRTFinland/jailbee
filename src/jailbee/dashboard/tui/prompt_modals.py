"""Modal screens for the questions a running dashboard action asks.

One screen per prompt kind of the prompt channel (`jailbee.prompt_channel`):
confirm, choose one, choose many, text and masked secret. Every screen
dismisses with the *wire value* of its kind (bool, option id, list of option
ids, str) or ``None``: Esc and Ctrl-C cancel the question, which is exactly
what Ctrl-C does at the CLI prompt. The output screen shows an action's
captured output.
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

from jailbee.prompt_channel import Prompt

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
PromptModal .buttons { height: auto; align-horizontal: right; }
PromptModal OptionList, PromptModal SelectionList { height: auto; max-height: 16; }
PromptModal Input { margin: 1 0 0 0; }
OutputScreen { align: center middle; background: ansi_default 40%; }
OutputScreen > .dialog {
    width: 95%; height: 90%; border: round ansi_blue; background: ansi_default; padding: 0 1;
}
OutputScreen .title { text-style: bold; }
OutputScreen.failed .title { color: ansi_red; }
OutputScreen VerticalScroll { height: 1fr; }
"""


class PromptModal(ModalScreen[Any]):
    """Base: title, optional repo/container context, cancel on Esc / Ctrl-C."""

    DEFAULT_CSS = _CSS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+c", "cancel", "Cancel", priority=True),
    ]
    hint = "Enter confirms, Esc cancels"

    def __init__(self, prompt: Prompt) -> None:
        super().__init__()
        self.prompt = prompt
        if prompt.destructive:
            self.add_class("destructive")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.heading()), classes="title", markup=False)
            context = self.prompt.context
            if context is not None and (context.repo or context.container):
                where = " / ".join(part for part in (context.repo, context.container) if part)
                yield Static(Text(where), classes="context", markup=False)
            yield from self.body()
            yield Static(self.hint, classes="hint", markup=False)

    def heading(self) -> str:
        return self.prompt.question

    def body(self) -> ComposeResult:
        yield from ()


class ConfirmModal(PromptModal):
    """Yes / No. Destructive: error-styled, and "No" holds the focus."""

    hint = "y / n, Enter picks the focused button, Esc cancels"

    def heading(self) -> str:
        return ("DESTRUCTIVE: " if self.prompt.destructive else "") + self.prompt.question

    def body(self) -> ComposeResult:
        with Horizontal(classes="buttons"):
            yield Button("Yes", id="yes", variant="error" if self.prompt.destructive else "primary")
            yield Button("No", id="no")

    def on_mount(self) -> None:
        # A destructive question defaults to No (the channel already says so).
        yes = self.prompt.default is True and not self.prompt.destructive
        self.query_one("#yes" if yes else "#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "yes")

    def on_key(self, event: events.Key) -> None:
        if event.key in ("y", "n"):
            event.stop()
            self.dismiss(event.key == "y")
        elif event.key in ("left", "right", "tab", "shift+tab"):
            event.stop()
            self.query_one("#no" if self.query_one("#yes").has_focus else "#yes").focus()


class ChooseOneModal(PromptModal):
    """A list; Enter chooses the highlighted option (its id)."""

    hint = "Up/Down move, Enter chooses, Esc cancels"

    def body(self) -> ComposeResult:
        yield OptionList(
            *[
                Option(
                    Text(o.label + (f"  {o.description}" if o.description else "")),
                    id=o.id,
                )
                for o in self.prompt.options
            ]
        )

    def on_mount(self) -> None:
        widget = self.query_one(OptionList)
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
        yield SelectionList[str](
            *[
                Selection(Text(o.label + (f"  {o.description}" if o.description else "")), o.id)
                for o in self.prompt.options
            ]
        )

    def on_mount(self) -> None:
        self.query_one(SelectionList).focus()

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
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if not event.value and not self.prompt.clear_shortcut:
            self.query_one("#problem", Static).update("An answer is required")
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
    ]

    def __init__(self, title: str, text: str, *, failed: bool = False) -> None:
        super().__init__()
        self.title_text = title
        self.text = text
        if failed:
            self.add_class("failed")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text), classes="title", markup=False)
            with VerticalScroll():
                yield Static(Text.from_ansi(self.text), id="captured", markup=False)
            yield Static("Up/Down scroll, Enter or Esc returns", classes="hint", markup=False)

    def action_close(self) -> None:
        self.dismiss(None)


def modal_for(prompt: Prompt) -> PromptModal:
    """The screen for ``prompt``'s kind."""
    kinds: dict[str, type[PromptModal]] = {
        "confirm": ConfirmModal,
        "choose_one": ChooseOneModal,
        "choose_many": ChooseManyModal,
        "text": TextModal,
        "secret": TextModal,
    }
    return kinds[prompt.kind](prompt)
