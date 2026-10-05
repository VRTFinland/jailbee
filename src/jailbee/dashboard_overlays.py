"""Pure state, key handling and rendering for the dashboard's inline inputs.

The dashboard used to hand the whole terminal to ``typer.prompt`` for every
question it had, which blanks the screen. A :class:`TextPrompt` or
:class:`Picker` is an overlay instead: drawn under the table like the action
menu, edited a key at a time, cancelled with Esc. Nothing here touches a
terminal, so all of it unit-tests as plain functions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from rich import box
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel

from jailbee.dashboard_settings import CURSOR_STYLE

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rich.console import RenderableType

    from jailbee.dashboard_accounts import AccountsState
    from jailbee.dashboard_egress import EgressState

PromptOutcome = Literal["editing", "submit", "cancel"]

PROMPT_HINT = "[bold]Enter[/bold] confirm  ·  [bold]Esc[/bold] cancel"
SUGGEST_HINT = (
    "[bold]Tab[/bold] complete  ·  [bold]↑/↓[/bold] choose  ·  "
    "[bold]Enter[/bold] confirm  ·  [bold]Esc[/bold] cancel"
)
# Rows of suggestions shown under a prompt; longer lists scroll with the highlight.
SUGGESTION_ROWS = 8
# Both cursor-key encodings: normal (CSI) and application mode (SS3).
_UP_KEYS = (b"\x1b[A", b"\x1bOA")
_DOWN_KEYS = (b"\x1b[B", b"\x1bOB")
PICKER_HINT = "[bold]↑/↓[/bold] move  ·  [bold]Enter[/bold] choose  ·  [bold]Esc[/bold] cancel"


def decode_input(pending: bytes, key: bytes) -> tuple[str, bytes]:
    """Printable text carried by ``key`` and the partial UTF-8 still pending.

    A multi-byte character can arrive split across two terminal reads, so an
    incomplete tail is held back (only the tail; the valid prefix is returned
    at once). Invalid bytes are dropped, never turned into U+FFFD.
    Non-printable input (escape sequences, control bytes) yields no text and
    leaves ``pending`` as it was.
    """
    encoded = pending + key
    tail = _partial_tail(encoded)
    text = encoded[: len(encoded) - len(tail)].decode("utf-8", errors="ignore")
    if text and not text.isprintable():
        return "", pending
    return text, tail


def _partial_tail(data: bytes) -> bytes:
    """The trailing bytes of ``data`` that start a UTF-8 character not yet complete."""
    for n in range(1, min(3, len(data)) + 1):
        byte = data[-n]
        if byte & 0xC0 == 0x80:  # continuation byte: keep looking for its lead
            continue
        need = (
            2
            if 0xC2 <= byte <= 0xDF
            else 3
            if 0xE0 <= byte <= 0xEF
            else 4
            if 0xF0 <= byte <= 0xF4
            else 0
        )
        return data[-n:] if need > n else b""
    return b""


@dataclass(frozen=True)
class TextPrompt:
    """One free-text question, asked inside the dashboard frame.

    ``purpose`` names what the answer is for (the dashboard's submit handler
    switches on it); ``target`` is the repo prefix or container it applies to,
    re-resolved at submit time because the row can vanish while the prompt is
    open; ``carry`` holds the answers of earlier steps of a multi-step flow.
    ``back`` is the overlay Esc (or a finished submit) returns to.
    ``suggestions`` turns the prompt into a typed choice: the matches are listed under the input,
    ``highlight`` indexes the filtered list (None until the user arrows into it), and
    ``require_suggestion`` refuses a name not in the list.
    """

    purpose: str
    title: str
    label: str
    text: str = ""
    target: str = ""
    carry: tuple[str, ...] = ()
    error: str | None = None
    pending_utf8: bytes = b""
    back: EgressState | AccountsState | None = None
    suggestions: tuple[str, ...] = ()
    highlight: int | None = None
    require_suggestion: bool = False


def parse_pr_number(text: str) -> int | None:
    """A positive PR number, or None. ASCII digits only; absurd lengths refused."""
    answer = text.strip()
    if not (answer.isascii() and answer.isdecimal()):
        return None
    try:
        number = int(answer)
    except ValueError:  # Python refuses excessively long integer strings
        return None
    return number if number >= 1 else None


def filter_suggestions(suggestions: Sequence[str], text: str) -> list[str]:
    """The suggestions containing ``text``, case-insensitively; prefix matches first.

    Each group keeps the suggestions' own order. Blank text matches everything.
    """
    needle = text.strip().casefold()
    if not needle:
        return list(suggestions)
    starts = [s for s in suggestions if s.casefold().startswith(needle)]
    inside = [
        s for s in suggestions if needle in s.casefold() and not s.casefold().startswith(needle)
    ]
    return starts + inside


def highlighted(prompt: TextPrompt) -> str | None:
    """The suggestion under the highlight, or None when there is none to take."""
    if prompt.highlight is None:
        return None
    matches = filter_suggestions(prompt.suggestions, prompt.text)
    return matches[prompt.highlight] if 0 <= prompt.highlight < len(matches) else None


def _suggestion_key(prompt: TextPrompt, data: bytes) -> TextPrompt | None:
    """Apply an arrow or Tab to a prompt with suggestions; None for any other key."""
    matches = filter_suggestions(prompt.suggestions, prompt.text)
    if data in _DOWN_KEYS:
        if not matches:
            return prompt
        index = 0 if prompt.highlight is None else min(prompt.highlight + 1, len(matches) - 1)
        return replace(prompt, highlight=index)
    if data in _UP_KEYS:
        if prompt.highlight is None:
            return prompt
        return replace(prompt, highlight=prompt.highlight - 1 if prompt.highlight > 0 else None)
    if data == b"\t":
        if not matches:
            return prompt
        chosen = highlighted(prompt) or matches[0]
        return replace(prompt, text=chosen, highlight=None, error=None)
    return None


def validate_answer(prompt: TextPrompt) -> str | None:
    """Why the current answer cannot be submitted, or None when it can."""
    if not prompt.text.strip():
        return f"{prompt.label} cannot be empty"
    if prompt.purpose == "new-pr" and parse_pr_number(prompt.text) is None:
        return "PR number must be a positive whole number"
    if (
        prompt.require_suggestion
        and prompt.suggestions
        and prompt.text.strip() not in prompt.suggestions
    ):
        return f"'{prompt.text.strip()}' is not one of the listed branches"
    return None


def handle_prompt_key(prompt: TextPrompt, data: bytes) -> tuple[TextPrompt, PromptOutcome]:
    """Apply one raw terminal read to ``prompt``.

    Esc, Ctrl-C and EOF cancel *the prompt* — the caller decides where that
    lands (never out of the dashboard). Arrow keys arrive as ``ESC [ …`` and
    are non-printable, so they are ignored rather than typed.
    With suggestions, ↑/↓ (either cursor-key encoding) move the highlight and Tab completes;
    any edit drops the highlight.
    """
    if data in (b"\x1b", b"\x03", b""):
        return prompt, "cancel"
    if prompt.suggestions:
        moved = _suggestion_key(prompt, data)
        if moved is not None:
            return moved, "editing"
    if data in (b"\r", b"\n"):
        chosen = highlighted(prompt)
        if chosen is not None:
            prompt = replace(prompt, text=chosen, highlight=None)
        error = validate_answer(prompt)
        if error is not None:
            return replace(prompt, error=error), "editing"
        return prompt, "submit"
    if data in (b"\x7f", b"\x08"):
        if prompt.pending_utf8:
            return replace(prompt, pending_utf8=b"", highlight=None, error=None), "editing"
        return replace(prompt, text=prompt.text[:-1], highlight=None, error=None), "editing"
    appended, pending = decode_input(prompt.pending_utf8, data)
    if not appended:
        return replace(prompt, pending_utf8=pending, highlight=None), "editing"
    return (
        replace(
            prompt,
            text=prompt.text + appended,
            pending_utf8=pending,
            highlight=None,
            error=None,
        ),
        "editing",
    )


def render_prompt(prompt: TextPrompt) -> RenderableType:
    lines: list[RenderableType] = [f"{escape(prompt.label)}", f"> {escape(prompt.text)}▏"]
    if prompt.suggestions:
        matches = filter_suggestions(prompt.suggestions, prompt.text)
        rows = [
            f"[bold cyan]▸[/] [{CURSOR_STYLE}]{escape(m)}[/]"
            if i == prompt.highlight
            else f"  {escape(m)}"
            for i, m in enumerate(matches)
        ] or ["[dim](no matching branch)[/dim]"]
        lines.extend(window_lines(rows, prompt.highlight or 0, SUGGESTION_ROWS))
    if prompt.error:
        lines.append(f"[red]{escape(prompt.error)}[/red]")
    return Panel(
        Group(*lines),
        title=f"[bold]{escape(prompt.title)}[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
        expand=False,
    )


@dataclass(frozen=True)
class PickerEntry:
    label: str
    value: str


@dataclass(frozen=True)
class Picker:
    """A short list to choose from.

    ``purpose``/``target``/``carry``/``back`` are as in :class:`TextPrompt`.
    """

    purpose: str
    title: str
    entries: tuple[PickerEntry, ...]
    index: int = 0
    target: str = ""
    carry: tuple[str, ...] = ()
    back: EgressState | AccountsState | None = None


def move_picker(picker: Picker, delta: int) -> Picker:
    last = max(0, len(picker.entries) - 1)
    return replace(picker, index=max(0, min(last, picker.index + delta)))


def picked(picker: Picker) -> PickerEntry | None:
    return picker.entries[picker.index] if 0 <= picker.index < len(picker.entries) else None


# A scrolled list never shrinks below this many rows: the cursor plus a
# marker for each hidden end.
MIN_LIST_ROWS = 3


def window_lines(lines: list[str], index: int, max_rows: int | None) -> list[str]:
    """``lines`` cut to ``max_rows`` rows that keep ``index`` in view.

    Each hidden end is replaced by a dim "↑/↓ N more" row, so a list taller
    than the terminal scrolls with its cursor instead of being clipped below
    the screen. The window is derived from the cursor alone — no scroll
    offset is stored — so it re-centres on every frame. ``None`` keeps all.
    """
    count = len(lines)
    if max_rows is None or count <= max(max_rows, MIN_LIST_ROWS):
        return lines
    budget = max(max_rows, MIN_LIST_ROWS)

    def more(n: int, arrow: str) -> str:
        return f"[dim]  {arrow} {n} more[/dim]"

    # One end hidden: the cursor fits with a single marker.
    size = budget - 1
    if index < size:
        return [*lines[:size], more(count - size, "↓")]
    if index >= count - size:
        return [more(count - size, "↑"), *lines[count - size :]]
    size = budget - 2
    start = max(1, min(index - size // 2, count - size - 1))
    return [more(start, "↑"), *lines[start : start + size], more(count - start - size, "↓")]


def render_picker(picker: Picker, max_rows: int | None = None) -> RenderableType:
    """The picker as a bordered panel, its entries windowed to ``max_rows``."""
    lines = [
        f"[bold cyan]▸[/] [{CURSOR_STYLE}]{escape(entry.label)}[/]"
        if i == picker.index
        else f"  {escape(entry.label)}"
        for i, entry in enumerate(picker.entries)
    ] or ["[dim](nothing to choose)[/dim]"]
    return Panel(
        "\n".join(window_lines(lines, picker.index, max_rows)),
        title=f"[bold]{escape(picker.title)}[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
        expand=False,
    )
