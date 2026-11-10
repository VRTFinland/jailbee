"""Asking for values the user left out.

The one policy every command follows: a value a command needs but was not
given is picked or typed on an interactive terminal, and is an exit-2 error
naming the candidates anywhere else. Rendering stays in `tui`; this module
decides *whether* to ask and what the refusal says.

Every question kind goes through one public function and one `PromptBackend`
chosen per call by `select_backend()`:

=============  ==========================================  =================
function       answer                                      cancel (Ctrl-C/Esc)
=============  ==========================================  =================
ask_confirm    bool                                        Cancelled (never "no")
choose_one     the chosen option's value                   Cancelled
choose_many    list of values; [] is a valid answer        Cancelled
ask_text       validated str; "" only via clear_shortcut   Cancelled
ask_secret     str, never echoed or logged                 Cancelled
=============  ==========================================  =================

A backend returns None for cancel; the public function raises `Cancelled`
(exit 1), so every backend reproduces the same meaning. Cancel is never an
answer: it must not read as "no", "nothing selected" or an empty string. A
backend that cannot ask raises `MissingValue` (exit 2) naming the value.
`JAILBEE_NONINTERACTIVE` forces the non-interactive backend over everything.

`MissingValue` and `Cancelled` are Typer's `ClickException`s, so Typer prints
them (its `Error` panel, stderr) and sets the exit code — under `CliRunner`
exactly as in production. Typer vendors Click (`typer._click`); a top-level
`click.ClickException` would escape it as a traceback.
They are deliberately not `ValueError`s: many callers turn a `ValueError`
into exit 1, and would swallow these into the wrong code.
"""

from __future__ import annotations

import os
import socket
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from rich.text import Text
from typer._click.exceptions import ClickException

from jailbee import prompt_channel as pc

T = TypeVar("T")

# questionary answers a Choice whose value is None with its *title*; a unique
# object can never be mistaken for a real answer.
_CANCEL = object()


def is_interactive() -> bool:
    """Whether jailbee may stop and ask: stdin is a terminal or a prompt
    channel is present, and the `JAILBEE_NONINTERACTIVE` override is unset."""
    if os.environ.get("JAILBEE_NONINTERACTIVE"):
        return False
    return _channel_end() is not None or sys.stdin.isatty()


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
    description: str = ""


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


@dataclass(frozen=True)
class PromptContext:
    """Where a question comes from; a backend may show it (the terminal does not)."""

    repo: str | None = None
    container: str | None = None


class PromptBackend(Protocol):
    """How questions are put to a user. Each method returns None for cancel.

    Backends do not apply policy (single-candidate shortcuts, validation
    loops, `Cancelled`); the public functions do. A backend that cannot ask
    raises `MissingValue`.
    """

    def confirm(
        self,
        question: str,
        *,
        default: bool,
        destructive: bool,
        context: PromptContext | None,
        alternative: str | None,
    ) -> bool | None: ...

    def choose_one(
        self,
        noun: str,
        options: Sequence[Option[Any]],
        *,
        destructive: bool,
        context: PromptContext | None,
        alternative: str | None,
        picker: Callable[[Sequence[Option[Any]]], Any | None] | None,
        question: str | None,
    ) -> Any | None: ...

    def choose_many(
        self,
        noun: str,
        options: Sequence[Option[Any]],
        *,
        context: PromptContext | None,
        alternative: str | None,
        picker: Callable[[Sequence[Option[Any]]], list[Any] | None] | None,
        question: str | None,
    ) -> list[Any] | None: ...

    def text(
        self,
        noun: str,
        *,
        default: str | None,
        completions: Sequence[str] | None,
        clear_shortcut: bool,
        context: PromptContext | None,
        alternative: str | None,
    ) -> str | None: ...

    def secret(
        self,
        noun: str,
        *,
        context: PromptContext | None,
        alternative: str | None,
    ) -> str | None: ...


class TerminalBackend:
    """Today's rendering: questionary and the `tui` pickers on the terminal."""

    def confirm(
        self,
        question: str,
        *,
        default: bool,
        destructive: bool,
        context: PromptContext | None,
        alternative: str | None,
    ) -> bool | None:
        # A destructive question never defaults to consent.
        return _confirm(question, False if destructive else default)

    def choose_one(
        self,
        noun: str,
        options: Sequence[Option[Any]],
        *,
        destructive: bool,
        context: PromptContext | None,
        alternative: str | None,
        picker: Callable[[Sequence[Option[Any]]], Any | None] | None,
        question: str | None,
    ) -> Any | None:
        return (picker or (lambda opts: _select(noun, opts, question=question)))(options)

    def choose_many(
        self,
        noun: str,
        options: Sequence[Option[Any]],
        *,
        context: PromptContext | None,
        alternative: str | None,
        picker: Callable[[Sequence[Option[Any]]], list[Any] | None] | None,
        question: str | None,
    ) -> list[Any] | None:
        return (picker or (lambda opts: _select_many(noun, opts, question=question)))(options)

    def text(
        self,
        noun: str,
        *,
        default: str | None,
        completions: Sequence[str] | None,
        clear_shortcut: bool,
        context: PromptContext | None,
        alternative: str | None,
    ) -> str | None:
        if clear_shortcut:
            return _ask(noun, default, completions, clear_shortcut=True)
        return _ask(noun, default, completions)

    def secret(
        self,
        noun: str,
        *,
        context: PromptContext | None,
        alternative: str | None,
    ) -> str | None:
        return _ask_secret(noun)


class NonInteractiveBackend:
    """No one to ask: every question is `MissingValue` (exit 2) naming the value."""

    def confirm(
        self,
        question: str,
        *,
        default: bool,
        destructive: bool,
        context: PromptContext | None,
        alternative: str | None,
    ) -> bool | None:
        how = f"pass it explicitly{f' or use {alternative}' if alternative else ''}"
        raise MissingValue(
            "confirmation",
            reason=f"confirmation needed ({question}); {how}, or run in a terminal to answer",
        )

    def choose_one(
        self,
        noun: str,
        options: Sequence[Option[Any]],
        *,
        destructive: bool,
        context: PromptContext | None,
        alternative: str | None,
        picker: Callable[[Sequence[Option[Any]]], Any | None] | None,
        question: str | None,
    ) -> Any | None:
        raise MissingValue(noun, candidates=[o.label for o in options], alternative=alternative)

    def choose_many(
        self,
        noun: str,
        options: Sequence[Option[Any]],
        *,
        context: PromptContext | None,
        alternative: str | None,
        picker: Callable[[Sequence[Option[Any]]], list[Any] | None] | None,
        question: str | None,
    ) -> list[Any] | None:
        raise MissingValue(noun, candidates=[o.label for o in options], alternative=alternative)

    def text(
        self,
        noun: str,
        *,
        default: str | None,
        completions: Sequence[str] | None,
        clear_shortcut: bool,
        context: PromptContext | None,
        alternative: str | None,
    ) -> str | None:
        raise MissingValue(noun, alternative=alternative, free_text=True)

    def secret(
        self,
        noun: str,
        *,
        context: PromptContext | None,
        alternative: str | None,
    ) -> str | None:
        raise MissingValue(noun, alternative=alternative, free_text=True)


_CHANNELS: dict[tuple[int, int, int], pc.CommandEnd] = {}


def _channel_end() -> pc.CommandEnd | None:
    """The command end of the prompt channel named by `JAILBEE_PROMPT_FD`.

    None when unset or invalid (not a number, 0-2, not an open socket): an
    invalid fd behaves as if absent. The descriptor is duplicated, so the
    inherited one is never closed; the end is cached per socket (a reused fd
    number is a different socket) so the hello is exchanged once.
    """
    raw = os.environ.get("JAILBEE_PROMPT_FD")
    if not raw:
        return None
    try:
        fd = int(raw)
        if fd <= 2:
            return None
        st = os.fstat(fd)
    except (ValueError, OSError):
        return None
    if not stat.S_ISSOCK(st.st_mode):
        return None
    key = (fd, st.st_dev, st.st_ino)
    end = _CHANNELS.get(key)
    if end is None:
        try:
            sock = socket.socket(fileno=os.dup(fd))
        except OSError:
            return None
        end = _CHANNELS[key] = pc.CommandEnd(sock)
    return end


def _wire_context(context: PromptContext | None) -> pc.WireContext | None:
    return None if context is None else pc.WireContext(context.repo, context.container)


def _wire_options(options: Sequence[Option[Any]]) -> tuple[pc.WireOption, ...]:
    return tuple(
        pc.WireOption(str(i), o.title, o.label, o.description) for i, o in enumerate(options)
    )


class ChannelBackend:
    """Questions travel to the UI that spawned this command (`prompt_channel`).

    Cancel, and the UI going away, are None. A peer that speaks another
    protocol version, or answers nonsense, is `MissingValue` (exit 2): the
    command cannot be asked, and says why.
    """

    def __init__(self, end: pc.CommandEnd) -> None:
        self._end = end

    def _ask(self, make: Callable[[int], pc.Prompt]) -> Any | None:
        try:
            reply = self._end.ask(make)
        except pc.ProtocolError as exc:
            raise MissingValue("answer", reason=f"prompt channel failed: {exc}") from exc
        return None if isinstance(reply, pc.Cancel) else _Reply(reply.value)

    def _bad(self, what: str) -> MissingValue:
        return MissingValue("answer", reason=f"prompt channel failed: {what}")

    def confirm(
        self,
        question: str,
        *,
        default: bool,
        destructive: bool,
        context: PromptContext | None,
        alternative: str | None,
    ) -> bool | None:
        reply = self._ask(
            lambda i: pc.Prompt(
                i,
                "confirm",
                question,
                default=False if destructive else default,
                destructive=destructive,
                context=_wire_context(context),
            )
        )
        if reply is None:
            return None
        if not isinstance(reply.value, bool):
            raise self._bad("confirm answer is not a boolean")
        return reply.value

    def choose_one(
        self,
        noun: str,
        options: Sequence[Option[Any]],
        *,
        destructive: bool,
        context: PromptContext | None,
        alternative: str | None,
        picker: Callable[[Sequence[Option[Any]]], Any | None] | None,
        question: str | None,
    ) -> Any | None:
        wire = _wire_options(options)
        text = noun if question is None else question
        reply = self._ask(
            lambda i: pc.Prompt(
                i,
                "choose_one",
                text,
                options=wire,
                destructive=destructive,
                context=_wire_context(context),
            )
        )
        if reply is None:
            return None
        for w, o in zip(wire, options, strict=True):
            if reply.value == w.id:
                return o.value
        raise self._bad(f"unknown option {reply.value!r}")

    def choose_many(
        self,
        noun: str,
        options: Sequence[Option[Any]],
        *,
        context: PromptContext | None,
        alternative: str | None,
        picker: Callable[[Sequence[Option[Any]]], list[Any] | None] | None,
        question: str | None,
    ) -> list[Any] | None:
        # `picker` is a terminal renderer; the channel sends the question instead.
        wire = _wire_options(options)
        text = noun if question is None else question
        reply = self._ask(
            lambda i: pc.Prompt(
                i, "choose_many", text, options=wire, context=_wire_context(context)
            )
        )
        if reply is None:
            return None
        ids = reply.value
        if not isinstance(ids, list):
            raise self._bad("choose_many answer is not a list")
        by_id = {w.id: o.value for w, o in zip(wire, options, strict=True)}
        try:
            return [by_id[x] for x in ids]
        except (KeyError, TypeError) as exc:
            raise self._bad(f"unknown option in {ids!r}") from exc

    def text(
        self,
        noun: str,
        *,
        default: str | None,
        completions: Sequence[str] | None,
        clear_shortcut: bool,
        context: PromptContext | None,
        alternative: str | None,
    ) -> str | None:
        reply = self._ask(
            lambda i: pc.Prompt(
                i,
                "text",
                noun,
                default=default,
                completions=None if completions is None else tuple(completions),
                clear_shortcut=clear_shortcut,
                context=_wire_context(context),
            )
        )
        if reply is None:
            return None
        if not isinstance(reply.value, str):
            raise self._bad("text answer is not a string")
        return reply.value

    def secret(
        self,
        noun: str,
        *,
        context: PromptContext | None,
        alternative: str | None,
    ) -> str | None:
        reply = self._ask(lambda i: pc.Prompt(i, "secret", noun, context=_wire_context(context)))
        if reply is None:
            return None
        if not isinstance(reply.value, str):
            raise self._bad("secret answer is not a string")
        return reply.value


@dataclass(frozen=True)
class _Reply:
    value: Any


def select_backend() -> PromptBackend:
    """The backend for this call. The one place a backend is chosen.

    `JAILBEE_NONINTERACTIVE` wins over everything; then a prompt channel
    (`JAILBEE_PROMPT_FD` naming a valid inherited socket); then the terminal
    if `is_interactive()` (looked up at call time); else non-interactive.
    """
    return _backend_for(None)


def _backend_for(interactive: Callable[[], bool] | None) -> PromptBackend:
    if os.environ.get("JAILBEE_NONINTERACTIVE"):
        return NonInteractiveBackend()
    end = _channel_end()
    if end is not None:
        return ChannelBackend(end)
    test = interactive if interactive is not None else globals()["is_interactive"]
    return TerminalBackend() if test() else NonInteractiveBackend()


def _note(message: str) -> None:
    from jailbee.tui import hint_console

    hint_console.print(Text(message), highlight=False)


def _select(noun: str, options: Sequence[Option[T]], *, question: str | None = None) -> T | None:  # noqa: UP047
    """The default picker: an arrow-key list over the options' titles.

    The header is `question` when given, else "Select <noun>:"."""
    import questionary

    choices = [questionary.Choice(title=o.title, value=o.value) for o in options]
    choices.append(questionary.Choice(title="cancel", value=_CANCEL))
    # questionary has 36 shortcut keys (0-9, a-z); more rows than that raise.
    header = f"Select {noun}:" if question is None else question
    result = questionary.select(header, choices=choices, use_shortcuts=len(choices) <= 36).ask()
    if result is None or result is _CANCEL:
        return None
    return result  # type: ignore[no-any-return]  # questionary is untyped; values are ours


def _select_question(question: str, options: Sequence[Option[T]]) -> T | None:  # noqa: UP047
    """An optional choice, initially pointing at Cancel rather than consent."""
    import questionary
    from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent, merge_key_bindings

    choices = [
        questionary.Choice(title=o.title, value=_CANCEL if o.value is None else o.value)
        for o in options
    ]
    if not any(o.value is None for o in options):
        choices.append(questionary.Choice(title="Cancel", value=_CANCEL))
    cancel_choice = next(choice for choice in choices if choice.value is _CANCEL)
    prompt = questionary.select(question, choices=choices, default=cancel_choice)
    bindings = KeyBindings()

    @bindings.add("escape")
    def cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=_CANCEL)

    existing = prompt.application.key_bindings
    prompt.application.key_bindings = merge_key_bindings(
        [existing, bindings] if existing is not None else [bindings]
    )
    result = prompt.ask()
    if result is None or result is _CANCEL:
        return None
    return result  # type: ignore[no-any-return]  # questionary is untyped; values are ours


def choose(question: str, options: Sequence[Option[T]]) -> T | None:  # noqa: UP047
    """Ask an optional question; callers own interactive policy. Cancel is the default."""
    return _select_question(question, options)


def _ask(
    noun: str,
    default: str | None,
    completions: Sequence[str] | None = None,
    *,
    clear_shortcut: bool = False,
) -> str | None:
    """The default text prompt; Tab completes from `completions`. None on Ctrl-C / Esc."""
    import questionary

    # Not str.capitalize(): it lowercases the rest ("GitHub" -> "Github").
    label = noun[:1].upper() + noun[1:]
    kwargs: dict[str, Any] = {}
    if clear_shortcut:
        from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent

        bindings = KeyBindings()

        @bindings.add("c-r")
        def clear(event: KeyPressEvent) -> None:
            event.app.exit(result="")

        label += " (Ctrl-R to clear)"
        # Reserve an empty result for Ctrl-R, not deleting the text and pressing Enter.
        kwargs = {"key_bindings": bindings, "validate": lambda text: bool(text.strip())}
    if completions:  # autocomplete refuses an empty choice list
        result = questionary.autocomplete(
            f"{label}:",
            choices=list(completions),
            default=default or "",
            match_middle=True,
            **kwargs,
        ).ask()
    else:
        result = questionary.text(f"{label}:", default=default or "", **kwargs).ask()
    return None if result is None else str(result)


def _select_many(  # noqa: UP047
    noun: str, options: Sequence[Option[T]], *, question: str | None = None
) -> list[T] | None:
    """The default multi-picker (`tui.checkbox`); None on Ctrl-C / Esc, [] if none ticked."""
    import questionary

    from jailbee.tui import checkbox

    choices = [
        questionary.Choice(title=o.title, value=o.value, description=o.description or None)
        for o in options
    ]
    header = f"Select {noun}:" if question is None else question
    result = checkbox(header, choices=choices)
    return None if result is None else list(result)


def _ask_secret(noun: str) -> str | None:
    """The default secret prompt (input hidden). None on Ctrl-C / Esc."""
    import questionary

    label = noun[:1].upper() + noun[1:]
    result = questionary.password(f"{label}:").ask()
    return None if result is None else str(result)


def _confirm(question: str, default: bool) -> bool | None:
    """The default yes/no prompt. None on Ctrl-C / Esc."""
    import questionary

    result = questionary.confirm(question, default=default).ask()
    return None if result is None else bool(result)


def choose_one(  # noqa: UP047
    noun: str,
    options: Sequence[Option[T]],
    *,
    destructive: bool = False,
    empty_reason: str | None = None,
    alternative: str | None = None,
    picker: Callable[[Sequence[Option[T]]], T | None] | None = None,
    is_interactive: Callable[[], bool] | None = None,
    context: PromptContext | None = None,
    question: str | None = None,
) -> T:
    """Resolve a missing value from its candidates.

    No candidates -> `MissingValue(empty_reason)`. One, not `destructive` ->
    taken, with a line on stderr saying so. Otherwise the backend asks:
    the picker on a terminal, `MissingValue` naming the candidates off one.
    `picker` lets a caller keep a richer renderer (the container table); it
    gets the options and returns a value, or None for cancel.
    `question` is the question as the user reads it (the picker header, the
    channel's question); without it the header is "Select <noun>:".

    Cancel (Ctrl-C / Esc / the picker's cancel row) -> `Cancelled`.
    """
    if not options:
        raise MissingValue(noun, reason=empty_reason or f"no {noun} to choose from")
    if len(options) == 1 and not destructive:
        _note(f"Using {noun} {options[0].label}")
        return options[0].value
    chosen = _backend_for(is_interactive).choose_one(
        noun,
        options,
        destructive=destructive,
        context=context,
        alternative=alternative,
        picker=picker,
        question=question,
    )
    if chosen is None:
        raise Cancelled()
    return chosen  # type: ignore[no-any-return]  # Protocol backends return Any; values are the options'


def choose_many(  # noqa: UP047
    noun: str,
    options: Sequence[Option[T]],
    *,
    empty_reason: str | None = None,
    alternative: str | None = None,
    context: PromptContext | None = None,
    picker: Callable[[Sequence[Option[T]]], list[T] | None] | None = None,
    question: str | None = None,
) -> list[T]:
    """Resolve a missing set of values from its candidates.

    No candidates -> `MissingValue(empty_reason)`. A single candidate is still
    asked (an empty selection is a legitimate answer; callers decide whether
    it is acceptable). Off a terminal, `MissingValue` naming the candidates.
    `picker` lets a caller keep a richer renderer (its own question, cursor
    row or pre-ticked rows); it gets the options and returns the chosen values,
    or None for cancel.
    `question` is the question shown, as for `choose_one`.

    Cancel (Ctrl-C / Esc) -> `Cancelled`. Submitting with nothing ticked is
    not a cancel: it returns `[]` (the terminal picker's bare Enter selects
    the highlighted row, as `tui.checkbox` documents).
    """
    if not options:
        raise MissingValue(noun, reason=empty_reason or f"no {noun} to choose from")
    chosen = select_backend().choose_many(
        noun, options, context=context, alternative=alternative, picker=picker, question=question
    )
    if chosen is None:
        raise Cancelled()
    return list(chosen)


def ask_text(
    noun: str,
    *,
    validate: Callable[[str], str | None],
    default: str | None = None,
    alternative: str | None = None,
    is_interactive: Callable[[], bool] | None = None,
    completions: Sequence[str] | None = None,
    clear_shortcut: bool = False,
    context: PromptContext | None = None,
) -> str:
    """Resolve a missing free-text value; re-ask while `validate` objects.

    `validate` returns an error message, or None when the text is acceptable.
    `completions` are offered for Tab completion; the answer is still free
    text, so `validate` decides whether it must be one of them.
    `clear_shortcut` offers Ctrl-R and returns "" without validation when
    pressed; ordinary empty submissions are blocked by the text prompt.

    Cancel (Ctrl-C / Esc) -> `Cancelled`; never an empty string.
    """
    backend = _backend_for(is_interactive)
    while True:
        answer = backend.text(
            noun,
            default=default,
            completions=completions,
            clear_shortcut=clear_shortcut,
            context=context,
            alternative=alternative,
        )
        if answer is None:
            raise Cancelled()
        if clear_shortcut and answer == "":
            return answer
        problem = validate(answer)
        if problem is None:
            return answer
        _note(problem)


def ask_secret(
    noun: str,
    *,
    alternative: str | None = None,
    context: PromptContext | None = None,
) -> str:
    """Resolve a missing secret (token, password); the answer is never echoed.

    Not validated or retried here. Off a terminal, `MissingValue` naming the
    value (never a default, never read from argv by this function).

    Cancel (Ctrl-C / Esc) -> `Cancelled`; never an empty string.
    """
    answer = select_backend().secret(noun, context=context, alternative=alternative)
    if answer is None:
        raise Cancelled()
    return answer


def ask_confirm(
    question: str,
    *,
    default: bool = True,
    destructive: bool = False,
    alternative: str | None = None,
    context: PromptContext | None = None,
) -> bool:
    """Resolve a missing yes/no decision; the missing-value form of `confirm`.

    `destructive` forces the default to "no" whatever `default` says, so a bare
    Enter never consents to damage. Off a terminal this is `MissingValue`
    (exit 2, naming `alternative` such as `--yes`), never a silent default.

    Cancel (Ctrl-C / Esc) -> `Cancelled`; it is not the answer "no".
    """
    answer = select_backend().confirm(
        question,
        default=default,
        destructive=destructive,
        context=context,
        alternative=alternative,
    )
    if answer is None:
        raise Cancelled()
    return answer


def confirm(question: str, *, default: bool = True) -> bool:
    """Ask a yes/no question on the terminal; Ctrl-C / Esc is `Cancelled`.

    Legacy form, kept until call sites move to `ask_confirm`. Unlike it, this
    is not a missing-value resolver: the caller decides what a non-interactive
    run means and checks `is_interactive()` before asking.
    """
    answer = _confirm(question, default)
    if answer is None:
        raise Cancelled()
    return answer
