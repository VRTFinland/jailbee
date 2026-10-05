"""Asking for values the user left out.

The one policy every command follows: a value a command needs but was not
given is picked or typed on an interactive terminal, and is an exit-2 error
naming the candidates anywhere else. Rendering stays in `tui`; this module
decides *whether* to ask and what the refusal says.

`MissingValue` and `Cancelled` are Typer's `ClickException`s, so Typer prints
them (its `Error` panel, stderr) and sets the exit code — under `CliRunner`
exactly as in production. Typer vendors Click (`typer._click`); a top-level
`click.ClickException` would escape it as a traceback.
They are deliberately not `ValueError`s: many callers turn a `ValueError`
into exit 1, and would swallow these into the wrong code.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from rich.text import Text
from typer._click.exceptions import ClickException

T = TypeVar("T")

# questionary answers a Choice whose value is None with its *title*; a unique
# object can never be mistaken for a real answer.
_CANCEL = object()


def is_interactive() -> bool:
    """Whether jailbee may stop and ask: stdin is a terminal, and the
    `JAILBEE_NONINTERACTIVE` override is unset."""
    return sys.stdin.isatty() and not os.environ.get("JAILBEE_NONINTERACTIVE")


def stdout_is_terminal() -> bool:
    """Whether stdout is a terminal; false in a pipe or a redirect, where a
    full-screen UI would have nowhere to draw."""
    return sys.stdout.isatty()


@dataclass(frozen=True)
class Option(Generic[T]):  # noqa: UP046
    """One candidate: what is returned, its picker row, its name in errors."""

    value: T
    title: str
    label: str


class MissingValue(ClickException):
    """A required value was not given and cannot be asked for."""

    exit_code = 2

    def __init__(
        self,
        noun: str,
        *,
        candidates: Sequence[str] = (),
        reason: str | None = None,
        alternative: str | None = None,
        free_text: bool = False,
    ) -> None:
        self.noun = noun
        self.candidates = tuple(candidates)
        if reason is not None:
            message = reason
        else:
            how = f"pass it explicitly{f' or use {alternative}' if alternative else ''}"
            verb = "type it" if free_text else "choose"
            message = f"missing {noun}; {how}, or run in a terminal to {verb}"
            if self.candidates:
                message += ". Candidates: " + ", ".join(self.candidates)
        super().__init__(message)


class Cancelled(ClickException):
    """The user backed out of a prompt."""

    exit_code = 1

    def __init__(self) -> None:
        super().__init__("cancelled")


def _note(message: str) -> None:
    from jailbee.tui import hint_console

    hint_console.print(Text(message), highlight=False)


def _select(noun: str, options: Sequence[Option[T]]) -> T | None:  # noqa: UP047
    """The default picker: an arrow-key list over the options' titles."""
    import questionary

    choices = [questionary.Choice(title=o.title, value=o.value) for o in options]
    choices.append(questionary.Choice(title="cancel", value=_CANCEL))
    # questionary has 36 shortcut keys (0-9, a-z); more rows than that raise.
    result = questionary.select(
        f"Select {noun}:", choices=choices, use_shortcuts=len(choices) <= 36
    ).ask()
    if result is None or result is _CANCEL:
        return None
    return result  # type: ignore[no-any-return]  # questionary is untyped; values are ours


def _ask(noun: str, default: str | None) -> str | None:
    """The default text prompt. None on Ctrl-C / Esc."""
    import questionary

    # Not str.capitalize(): it lowercases the rest ("GitHub" -> "Github").
    label = noun[:1].upper() + noun[1:]
    result = questionary.text(f"{label}:", default=default or "").ask()
    return None if result is None else str(result)


def choose_one(  # noqa: UP047
    noun: str,
    options: Sequence[Option[T]],
    *,
    destructive: bool = False,
    empty_reason: str | None = None,
    alternative: str | None = None,
    picker: Callable[[Sequence[Option[T]]], T | None] | None = None,
    is_interactive: Callable[[], bool] | None = None,
) -> T:
    """Resolve a missing value from its candidates.

    No candidates → `MissingValue(empty_reason)`. One, not `destructive` →
    taken, with a line on stderr saying so. Otherwise the picker on a
    terminal, `MissingValue` naming the candidates off one. `picker` lets a
    caller keep a richer renderer (the container table); it gets the options
    and returns a value, or None for cancel.
    """
    interactive = is_interactive if is_interactive is not None else globals()["is_interactive"]
    if not options:
        raise MissingValue(noun, reason=empty_reason or f"no {noun} to choose from")
    if len(options) == 1 and not destructive:
        _note(f"Using {noun} {options[0].label}")
        return options[0].value
    if not interactive():
        raise MissingValue(noun, candidates=[o.label for o in options], alternative=alternative)
    chosen = (picker or (lambda opts: _select(noun, opts)))(options)
    if chosen is None:
        raise Cancelled()
    return chosen


def ask_text(
    noun: str,
    *,
    validate: Callable[[str], str | None],
    default: str | None = None,
    alternative: str | None = None,
    is_interactive: Callable[[], bool] | None = None,
) -> str:
    """Resolve a missing free-text value; re-ask while `validate` objects.

    `validate` returns an error message, or None when the text is acceptable.
    """
    interactive = is_interactive if is_interactive is not None else globals()["is_interactive"]
    if not interactive():
        raise MissingValue(noun, alternative=alternative, free_text=True)
    while True:
        answer = _ask(noun, default)
        if answer is None:
            raise Cancelled()
        problem = validate(answer)
        if problem is None:
            return answer
        _note(problem)
