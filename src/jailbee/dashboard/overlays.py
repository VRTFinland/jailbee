"""Pure data and checks for the dashboard's inline questions.

The text prompt, the picker, and the suggestion filter. Typing, the cursor and
the shown error belong to the native boxes in `jailbee.dashboard.tui.native`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jailbee.dashboard.accounts import AccountsState
    from jailbee.dashboard.egress import EgressState

PROMPT_HINT = "[bold]Enter[/bold] confirm  ·  [bold]Esc[/bold] cancel"
SUGGEST_HINT = (
    "[bold]Tab[/bold] complete  ·  [bold]↑/↓[/bold] choose  ·  "
    "[bold]Enter[/bold] confirm  ·  [bold]Esc[/bold] cancel"
)
# Rows of suggestions shown under a prompt; longer lists scroll with the highlight.
SUGGESTION_ROWS = 8
PICKER_HINT = "[bold]↑/↓[/bold] move  ·  [bold]Enter[/bold] choose  ·  [bold]Esc[/bold] cancel"


@dataclass(frozen=True)
class TextPrompt:
    """One free-text question, asked inside the dashboard frame.

    ``purpose`` names what the answer is for (the session's submit handler
    switches on it); ``target`` is the repo prefix or container it applies to,
    re-resolved at submit time because the row can vanish while the prompt is
    open; ``carry`` holds the answers of earlier steps of a multi-step flow.
    ``back`` is the overlay Esc (or a finished submit) returns to. ``initial``
    is the answer the box starts with. ``suggestions`` turns the prompt into a
    typed choice (the matches are listed under the input), and
    ``require_suggestion`` refuses a name not in the list.
    """

    purpose: str
    title: str
    label: str
    initial: str = ""
    target: str = ""
    carry: tuple[str, ...] = ()
    back: EgressState | AccountsState | None = None
    suggestions: tuple[str, ...] = ()
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


def validate_answer(prompt: TextPrompt, text: str) -> str | None:
    """Why ``text`` cannot answer ``prompt``, or None when it can."""
    answer = text.strip()
    if not answer and prompt.purpose != "container-rename":  # empty clears the alias
        return f"{prompt.label} cannot be empty"
    if prompt.purpose == "new-pr" and parse_pr_number(text) is None:
        return "PR number must be a positive whole number"
    if prompt.require_suggestion and prompt.suggestions and answer not in prompt.suggestions:
        return f"'{answer}' is not one of the listed branches"
    return None


@dataclass(frozen=True)
class PickerEntry:
    label: str
    value: str


@dataclass(frozen=True)
class Picker:
    """A short list to choose from.

    ``purpose``/``target``/``carry``/``back`` are as in :class:`TextPrompt`.
    ``detail`` is shown above the entries, one line each (a destroy's risk summary).
    """

    purpose: str
    title: str
    entries: tuple[PickerEntry, ...]
    target: str = ""
    carry: tuple[str, ...] = ()
    back: EgressState | AccountsState | None = None
    detail: tuple[str, ...] = ()


# A list never shrinks below this many rows.
MIN_LIST_ROWS = 3
