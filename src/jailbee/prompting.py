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
ask_confirm    bool                                        typer.Abort (never "no")
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

The one exception to `Cancelled` is the yes/no question: it has always been
`typer.confirm`, whose Ctrl-C is typer's `Abort` ("Aborted!", exit 1), and a
cancelled confirmation presents that way on every backend, a prompt channel's
cancel included, so cancelling is exactly Ctrl-C.

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

import typer
from rich.text import Text
from typer._click.exceptions import ClickException

from jailbee import prompt_channel as pc

T = TypeVar("T")

# questionary answers a Choice whose value is None with its *title*; a unique
# object can never be mistaken for a real answer.
_CANCEL = object()
# Pass as `choose_one(default=CANCEL_ROW)` to start the terminal cursor on the
# cancel row, so a bare Enter backs out (an optional, consequential choice).
# Over the channel it names no option: the prompt has no default.
CANCEL_ROW: Any = _CANCEL


def displayed_question(noun: str, question: str | None) -> str:
    """The question as the user reads it: `question`, else "Select <noun>:".

    The one place this text is built: the terminal's default pickers and the
    prompt channel both call it, so a dashboard asks what the terminal asks.
    """
    return f"Select {noun}:" if question is None else question


def _capitalized(noun: str) -> str:
    # Not str.capitalize(): it lowercases the rest ("GitHub" -> "Github").
    return noun[:1].upper() + noun[1:]


def _explicit_hint(alternative: str | None) -> str:
    """"pass it explicitly" plus the flag that also answers, when there is one."""
    return f"pass it explicitly{f' or use {alternative}' if alternative else ''}"


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
            verb = "type it" if free_text else "choose"
            message = f"missing {noun}; {_explicit_hint(alternative)}, or run in a terminal to {verb}"
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
        detail: str | None = None,
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
        default: Any | None = None,
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
        default: Sequence[Any] | None = None,
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
        error: str | None = None,
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
        detail: str | None = None,
    ) -> bool | None:
        if detail:
            _show_detail(detail)
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
        default: Any | None = None,
    ) -> Any | None:
        if picker is not None:
            return picker(options)
        # Keep the documented two-argument seam when no question is given.
        kwargs: dict[str, Any] = {}
        if question is not None:
            kwargs["question"] = question
        if default is not None:
            kwargs["default"] = default
        return _select(noun, options, **kwargs)

    def choose_many(
        self,
        noun: str,
        options: Sequence[Option[Any]],
        *,
        context: PromptContext | None,
        alternative: str | None,
        picker: Callable[[Sequence[Option[Any]]], list[Any] | None] | None,
        question: str | None,
        default: Sequence[Any] | None = None,
    ) -> list[Any] | None:
        if picker is not None:
            return picker(options)
        kwargs: dict[str, Any] = {}
        if question is not None:
            kwargs["question"] = question
        if default is not None:
            kwargs["default"] = default
        return _select_many(noun, options, **kwargs)

    def text(
        self,
        noun: str,
        *,
        default: str | None,
        completions: Sequence[str] | None,
        clear_shortcut: bool,
        context: PromptContext | None,
        alternative: str | None,
        error: str | None = None,
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
        detail: str | None = None,
    ) -> bool | None:
        raise MissingValue(
            "confirmation",
            reason=(
                f"confirmation needed ({question}); {_explicit_hint(alternative)}, "
                "or run in a terminal to answer"
            ),
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
        default: Any | None = None,
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
        default: Sequence[Any] | None = None,
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
        error: str | None = None,
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


# The one process-wide global in jailbee's prompting: a process has exactly one
# prompt channel, adopted once from `JAILBEE_PROMPT_FD` and then reused by every
# question. It cannot be an argument: the questions are asked from hundreds of
# call sites that have no handle to pass it through.
_CHANNELS: list[pc.CommandEnd] = []


def drop_channel() -> None:
    """Close and forget the adopted channel (tests; a process normally keeps it)."""
    for end in _CHANNELS:
        end.close()
    _CHANNELS.clear()


def adopt_channel() -> None:
    """Consume `JAILBEE_PROMPT_FD`: dup the socket, cache the channel, unset the variable.

    The variable is removed so no grandchild (a detached worker, a subprocess)
    inherits a number that, in its table, may name some other descriptor. The
    inherited descriptor is made non-inheritable and left open; the duplicate
    is the channel. A value that is not a descriptor of an open socket (not a
    number, 0-2, closed, not a socket) is dropped as if absent. The CLI entry
    calls this before it spawns anything.
    """
    raw = os.environ.pop(pc.PROMPT_FD_ENV, None)
    if not raw:
        return
    try:
        fd = int(raw)
        if fd <= 2 or not stat.S_ISSOCK(os.fstat(fd).st_mode):
            return
        os.set_inheritable(fd, False)
        sock = socket.socket(fileno=os.dup(fd))
    except (ValueError, OSError):
        return
    drop_channel()
    _CHANNELS.append(pc.CommandEnd(sock))


def _channel_end() -> pc.CommandEnd | None:
    """The prompt channel this process was started under, if any.

    A newly set `JAILBEE_PROMPT_FD` is adopted first (see `adopt_channel`);
    afterwards the cached channel serves every call.
    """
    if os.environ.get(pc.PROMPT_FD_ENV):
        adopt_channel()
    return _CHANNELS[0] if _CHANNELS else None


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
        detail: str | None = None,
    ) -> bool | None:
        reply = self._ask(
            lambda i: pc.Prompt(
                i,
                "confirm",
                question,
                default=False if destructive else default,
                destructive=destructive,
                context=_wire_context(context),
                detail=detail,
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
        default: Any | None = None,
    ) -> Any | None:
        wire = _wire_options(options)
        text = displayed_question(noun, question)
        default_id = next(
            (
                w.id
                for w, o in zip(wire, options, strict=True)
                if default is not None and o.value == default
            ),
            None,
        )
        reply = self._ask(
            lambda i: pc.Prompt(
                i,
                "choose_one",
                text,
                options=wire,
                default=default_id,
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
        default: Sequence[Any] | None = None,
    ) -> list[Any] | None:
        # `picker` is a terminal renderer; the channel sends the question instead.
        wire = _wire_options(options)
        text = displayed_question(noun, question)
        ticked = (
            None
            if default is None
            else tuple(w.id for w, o in zip(wire, options, strict=True) if o.value in default)
        )
        reply = self._ask(
            lambda i: pc.Prompt(
                i,
                "choose_many",
                text,
                options=wire,
                default=ticked,
                context=_wire_context(context),
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
        error: str | None = None,
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
                error=error,
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


def select_backend(interactive: Callable[[], bool] | None = None) -> PromptBackend:
    """The backend for this call. The one place a backend is chosen.

    `JAILBEE_NONINTERACTIVE` wins over everything; then a prompt channel
    (`JAILBEE_PROMPT_FD` adopted at start-up, see `adopt_channel`); then the
    terminal if `is_interactive()` (looked up at call time, or the legacy
    `interactive` override a caller passed); else non-interactive.
    """
    if os.environ.get("JAILBEE_NONINTERACTIVE"):
        return NonInteractiveBackend()
    end = _channel_end()
    if end is not None:
        return ChannelBackend(end)
    test = interactive if interactive is not None else is_interactive
    return TerminalBackend() if test() else NonInteractiveBackend()


def _show_detail(detail: str) -> None:
    """Print a confirmation's detail lines before the question, as the CLI always did.

    A leading "⚠ " keeps its yellow marker; nothing is read as Rich markup."""
    from jailbee.tui import console

    for line in detail.splitlines():
        if line.startswith("⚠ "):
            console.print("[yellow]⚠[/yellow] ", Text(line[2:]), sep="", highlight=False)
        else:
            console.print(Text(line), highlight=False)


def _note(message: str) -> None:
    from jailbee.tui import hint_console

    hint_console.print(Text(message), highlight=False)


def _select(  # noqa: UP047
    noun: str,
    options: Sequence[Option[T]],
    *,
    question: str | None = None,
    default: T | None = None,
) -> T | None:
    """The default picker: an arrow-key list over the options' titles.

    The header is `question` when given, else "Select <noun>:". `default` is the
    value of the row the cursor starts on."""
    import questionary

    choices = [questionary.Choice(title=o.title, value=o.value) for o in options]
    choices.append(questionary.Choice(title="cancel", value=_CANCEL))
    # questionary has 36 shortcut keys (0-9, a-z); more rows than that raise.
    header = displayed_question(noun, question)
    preselect = next((c for c in choices if default is not None and c.value == default), None)
    extra: dict[str, Any] = {} if preselect is None else {"default": preselect}
    from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent, merge_key_bindings

    prompt = questionary.select(header, choices=choices, use_shortcuts=len(choices) <= 36, **extra)
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


def _ask(
    noun: str,
    default: str | None,
    completions: Sequence[str] | None = None,
    *,
    clear_shortcut: bool = False,
) -> str | None:
    """The default text prompt; Tab completes from `completions`. None on Ctrl-C / Esc."""
    import questionary

    label = _capitalized(noun)
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
    noun: str,
    options: Sequence[Option[T]],
    *,
    question: str | None = None,
    default: Sequence[T] | None = None,
) -> list[T] | None:
    """The default multi-picker (`tui.checkbox`); None on Ctrl-C / Esc, [] if none ticked."""
    import questionary

    from jailbee.tui import checkbox

    choices = [
        questionary.Choice(
            title=o.title,
            value=o.value,
            description=o.description or None,
            checked=default is not None and o.value in default,
        )
        for o in options
    ]
    result = checkbox(displayed_question(noun, question), choices=choices)
    return None if result is None else list(result)


def _ask_secret(noun: str) -> str | None:
    """The default secret prompt (input hidden). None on Ctrl-C / Esc."""
    import questionary

    result = questionary.password(f"{_capitalized(noun)}:").ask()
    return None if result is None else str(result)


def _confirm(question: str, default: bool) -> bool | None:
    """The default yes/no prompt: `typer.confirm`, as every command always asked.

    Ctrl-C / EOF is typer's `Abort` ("Aborted!", exit 1), not a return value."""
    return typer.confirm(question, default=default)


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
    default: Any = None,
    always_ask: bool = False,
) -> T:
    """Resolve a missing value from its candidates.

    No candidates -> `MissingValue(empty_reason)`. One, not `destructive` ->
    taken, with a line on stderr saying so. Otherwise the backend asks:
    the picker on a terminal, `MissingValue` naming the candidates off one.
    `picker` lets a caller keep a richer renderer (the container table); it
    gets the options and returns a value, or None for cancel.
    `question` is the question as the user reads it (the picker header, the
    channel's question); without it the header is "Select <noun>:".
    `default` is the value to preselect (terminal: the cursor row; channel: the
    prompt's `default` option id); the non-interactive backend ignores it.
    `always_ask` asks even a lone candidate (as `destructive` does) without the
    destructive meaning: a picker that shows every row, such as the submodule list.

    Cancel (Ctrl-C / Esc / the picker's cancel row) -> `Cancelled`.
    """
    if not options:
        raise MissingValue(noun, reason=empty_reason or f"no {noun} to choose from")
    if len(options) == 1 and not (destructive or always_ask):
        _note(f"Using {noun} {options[0].label}")
        return options[0].value
    chosen = select_backend(is_interactive).choose_one(
        noun,
        options,
        destructive=destructive,
        context=context,
        alternative=alternative,
        picker=picker,
        question=question,
        default=default,
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
    default: Sequence[T] | None = None,
) -> list[T]:
    """Resolve a missing set of values from its candidates.

    No candidates -> `MissingValue(empty_reason)`. A single candidate is still
    asked (an empty selection is a legitimate answer; callers decide whether
    it is acceptable). Off a terminal, `MissingValue` naming the candidates.
    `picker` lets a caller keep a richer renderer (its own question, cursor
    row or pre-ticked rows); it gets the options and returns the chosen values,
    or None for cancel.
    `question` is the question shown, as for `choose_one`. `default` lists the
    values pre-ticked (terminal checkbox and channel alike; a `picker` ticks
    its own rows).

    Cancel (Ctrl-C / Esc) -> `Cancelled`. Submitting with nothing ticked is
    not a cancel: it returns `[]` (the terminal picker's bare Enter selects
    the highlighted row, as `tui.checkbox` documents).
    """
    if not options:
        raise MissingValue(noun, reason=empty_reason or f"no {noun} to choose from")
    chosen = select_backend().choose_many(
        noun,
        options,
        context=context,
        alternative=alternative,
        picker=picker,
        question=question,
        default=default,
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
    backend = select_backend(is_interactive)
    error: str | None = None
    while True:
        answer = backend.text(
            noun,
            default=default,
            completions=completions,
            clear_shortcut=clear_shortcut,
            context=context,
            alternative=alternative,
            error=error,
        )
        if answer is None:
            raise Cancelled()
        if clear_shortcut and answer == "":
            return answer
        problem = validate(answer)
        if problem is None:
            return answer
        _note(problem)
        error = problem


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
    detail: str | None = None,
) -> bool:
    """Resolve a missing yes/no decision.

    `destructive` forces the default to "no" whatever `default` says, so a bare
    Enter never consents to damage. Off a terminal this is `MissingValue`
    (exit 2, naming `alternative` such as `--yes`), never a silent default.

    `detail` is what the user should know before answering (a risk summary):
    the terminal prints it above the question, a channel carries it to the
    modal. "⚠ " opens a line the terminal marks as a warning.

    Cancel (Ctrl-C / Esc / a channel's cancel) -> `typer.Abort`, as
    `typer.confirm` always did; it is not the answer "no".
    """
    answer = select_backend().confirm(
        question,
        default=default,
        destructive=destructive,
        context=context,
        alternative=alternative,
        detail=detail,
    )
    if answer is None:
        raise typer.Abort()
    return answer
