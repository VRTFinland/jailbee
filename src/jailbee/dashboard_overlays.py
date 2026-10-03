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
    from rich.console import RenderableType

    from jailbee.dashboard_accounts import AccountsState
    from jailbee.dashboard_egress import EgressState

PromptOutcome = Literal["editing", "submit", "cancel"]

PROMPT_HINT = "[bold]Enter[/bold] confirm  ·  [bold]Esc[/bold] cancel"
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


def validate_answer(prompt: TextPrompt) -> str | None:
    """Why the current answer cannot be submitted, or None when it can."""
    if not prompt.text.strip():
        return f"{prompt.label} cannot be empty"
    if prompt.purpose == "new-pr" and parse_pr_number(prompt.text) is None:
        return "PR number must be a positive whole number"
    return None


def handle_prompt_key(prompt: TextPrompt, data: bytes) -> tuple[TextPrompt, PromptOutcome]:
    """Apply one raw terminal read to ``prompt``.

    Esc, Ctrl-C and EOF cancel *the prompt* — the caller decides where that
    lands (never out of the dashboard). Arrow keys arrive as ``ESC [ …`` and
    are non-printable, so they are ignored rather than typed.
    """
    if data in (b"\x1b", b"\x03", b""):
        return prompt, "cancel"
    if data in (b"\r", b"\n"):
        error = validate_answer(prompt)
        if error is not None:
            return replace(prompt, error=error), "editing"
        return prompt, "submit"
    if data in (b"\x7f", b"\x08"):
        if prompt.pending_utf8:
            return replace(prompt, pending_utf8=b"", error=None), "editing"
        return replace(prompt, text=prompt.text[:-1], error=None), "editing"
    appended, pending = decode_input(prompt.pending_utf8, data)
    if not appended:
        return replace(prompt, pending_utf8=pending), "editing"
    return replace(prompt, text=prompt.text + appended, pending_utf8=pending, error=None), "editing"


def render_prompt(prompt: TextPrompt) -> RenderableType:
    lines: list[RenderableType] = [f"{escape(prompt.label)}", f"> {escape(prompt.text)}▏"]
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
