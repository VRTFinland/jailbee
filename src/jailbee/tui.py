"""Rich-based output helpers."""

from __future__ import annotations

import string
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import typer
from rich.console import Console
from rich.text import Text

if TYPE_CHECKING:
    from rich.status import Status

    from jailbee.claude_pool import Slot
    from jailbee.config import Config
    from jailbee.destroy_guard import RiskSummary
    from jailbee.incus import Incus
    from jailbee.lifecycle import ContainerInfo
    from jailbee.sync import BridgePlan, RefSummary

console = Console()
err_console = Console(stderr=True, style="bold red")
hint_console = Console(stderr=True)


def format_elapsed(seconds: float) -> str:
    """`90.4` -> `1m30s`, `9.2` -> `9s`."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


class ElapsedStatus:
    """The live line driven by :func:`status_with_elapsed`.

    ``update`` switches to a new step and restarts the clock, so the number
    on screen always answers "how long has *this* been going", not "how long
    since the command started".
    """

    #: Below this, a counter is flicker rather than information.
    _SHOW_AFTER_SECONDS = 5.0

    def __init__(self, status: Status, message: str) -> None:
        self._status = status
        self._lock = threading.Lock()
        self._message = message
        self._started = time.monotonic()

    def update(self, message: str) -> None:
        with self._lock:
            self._message = message
            self._started = time.monotonic()
        self.refresh()

    def refresh(self) -> None:
        """Re-render with the current elapsed time. Called from the ticker."""
        with self._lock:
            message, started = self._message, self._started
        elapsed = time.monotonic() - started
        suffix = f" — {format_elapsed(elapsed)}" if elapsed >= self._SHOW_AFTER_SECONDS else ""
        self._status.update(f"⏳ {message}…{suffix}")


@contextmanager
def status_with_elapsed(message: str) -> Iterator[ElapsedStatus]:
    """A spinner that also counts, for steps with no progress to report.

    Rich's spinner animates by itself, which answers "is it alive?" but not
    "how long have I been waiting?" — the question that matters when a step
    can legitimately run for ten minutes (a golden-image provision, an apt
    run inside the registry mirror). A daemon thread relabels the line once
    a second; callers only ever touch `update`.
    """
    stop = threading.Event()
    with console.status(f"⏳ {message}…") as status:
        handle = ElapsedStatus(status, message)

        def tick() -> None:
            while not stop.wait(1.0):
                handle.refresh()

        ticker = threading.Thread(target=tick, daemon=True, name="jailbee-status-elapsed")
        ticker.start()
        try:
            yield handle
        finally:
            stop.set()
            ticker.join(timeout=2.0)


def info(msg: str) -> None:
    console.print(msg)


def success(msg: str) -> None:
    console.print(f"[green]✓[/green] {msg}")


def success_plain(msg: str) -> None:
    """Like `success`, but the body is never reinterpreted as Rich markup.

    The `warn` / `warn_plain` hazard, on the success path: a port-forward
    endpoint's bracketed IPv6 display (``[fd00::1]:5037``) is read as a style
    tag and *silently deleted*, so a `jailbee port to-container`/`to-host`
    success line reports connecting to ``:5037`` instead of the real address.
    Highlighting is off too, so Rich doesn't recolour paths or numbers inside
    the body.

    Only the ``✓`` marker is styled; the body is emitted verbatim.
    """
    console.print(Text.assemble(("✓ ", "green"), msg), highlight=False)


def warn(msg: str) -> None:
    console.print(f"[yellow]⚠[/yellow] {msg}")


def warn_plain(msg: str) -> None:
    """Like `warn`, but the body is never reinterpreted as Rich markup.

    For warnings whose text embeds data that legitimately contains square
    brackets: trigger-qualified autostart step names (``on_create[build]``),
    branch names (``feat/[wip]``), pydantic v2 error detail
    (``[type=int_parsing, input_value=...]``). `warn` runs the message through
    Rich's markup parser, which reads those as style tags and *silently
    deletes* them. Same hazard and same remedy as `render_bridge_plan` and its
    ``markup=False`` call site.

    Only the ``⚠`` marker is styled; the body is emitted verbatim, with
    highlighting off so Rich doesn't recolour paths or numbers inside it.
    """
    console.print(Text.assemble(("⚠ ", "yellow"), msg), highlight=False)


def hint(lines: Sequence[str]) -> None:
    """Print an advisory block on stderr, marking only its first line.

    stderr rather than stdout because hints share a terminal with output that
    is parsed by scripts — `jailbee ls`'s table, `--format json`. `err_console`
    is unsuitable: it is styled `bold red`, which is for failures.

    Bodies are emitted as `Text`, never as Rich markup, so a reason
    containing square brackets survives verbatim — the same reason
    `warn_plain` exists alongside `warn`.
    """
    if not lines:
        return
    hint_console.print(Text.assemble(("⚠ ", "yellow"), lines[0]), highlight=False)
    for line in lines[1:]:
        hint_console.print(Text(line), highlight=False)


def error(msg: str) -> None:
    err_console.print(f"✗ {msg}")


def error_plain(msg: str) -> None:
    """Like `error`, but the body is never reinterpreted as Rich markup.

    The `warn` / `warn_plain` hazard, on the error path: a message carrying
    square brackets — a pip extra (``jailbee[gui]``), a bracketed container
    name in a batch summary — has them read as style tags and *silently
    deleted*, so the user is told to install ``'jailbee'`` or sees which
    container failed replaced by nothing. Highlighting is off too, so Rich
    doesn't recolour paths or numbers inside the body.

    The console's own ``bold red`` style still applies to the whole line.
    """
    err_console.print(Text(f"✗ {msg}"), highlight=False)


ConfirmFn = Callable[[str], bool]


def default_confirm(msg: str) -> bool:
    """Ask the user a yes/no question, defaulting to "no".

    The default `ConfirmFn` for modules that need to confirm a destructive or
    privilege-widening action. Callers inject their own in tests and in
    non-interactive contexts (e.g. the detached background worker, which must
    never block on stdin).

    Returns the documented default — `False` — instead of raising when stdin is
    closed or exhausted (`jailbee new < /dev/null`, CI, a script). There is no answer
    to be had there, and "not confirmed" is the safe reading.

    `KeyboardInterrupt` is deliberately *not* caught. Ctrl-C means "abandon this
    command", not "answer no" — and the two differ: `run_apply` treats a `False`
    answer as "skip the restart" and then finishes normally, so swallowing Ctrl-C
    would make `jailbee apply` exit 0 having done half its job. Click converts an
    escaping KeyboardInterrupt into `Abort`, which is the behaviour we want.
    """
    from rich.prompt import Confirm

    try:
        return Confirm.ask(msg, default=False)
    except EOFError:
        return False


CredentialSide = Literal["group", "repo"]
"""Which of two competing Claude logins the credential group keeps."""

ChooseCredentialFn = Callable[[Path, Path, str], "CredentialSide | None"]
"""Resolve the two-credential clash; ``None`` means the user cancelled."""


def choose_shared_credential(
    group_dir: Path,
    repo_cred: Path,
    container_prefix: str,
) -> CredentialSide | None:
    """Ask which login a credential group should keep; ``None`` to cancel.

    Reached only from `init_command._ensure_claude_credentials_dir`, and only
    in the one ambiguous case: the group directory and the joining repo both
    already hold a `.credentials.json`. Exactly one can survive — the loser is
    an independent grant that nothing would ever read again — and this is the
    prompt that used to be a hard refusal. That refusal fired on every repo
    but the first on any host that adopted a group after already logging in
    per-repo, and it fired from the middle of `run_apply`, leaving the profiles
    unwritten.

    Returns ``None`` without rendering anything when stdin is not a TTY. The
    caller turns that into the original refusal, so a piped or CI `jailbee
    apply` still fails loudly instead of blocking on a prompt no one can
    answer.

    The printed note names the `claude_credentials.repos` opt-out, because
    "keep this repo on its own login" is a *config* answer, not a runtime one:
    it is not offered as a third choice (jailbee does not edit `global.yaml`),
    so without the note a user who wants neither shared login sees no way out.
    `container_prefix` is there only to make that note copy-pasteable — it is
    the key `repos` is dictionaries by, and the one part of the block the user
    cannot guess from the prompt.
    """
    if not sys.stdin.isatty():
        return None

    import questionary

    # warn_plain, not warn: the body is two filesystem paths, and `warn`
    # would read any square bracket in one as a Rich style tag and silently
    # delete it — leaving the user a path that does not exist.
    warn_plain(
        f"Both the credential group at {group_dir} and this repo "
        f"({repo_cred}) hold a Claude login. Only one can be shared; the "
        f"other becomes unused and is deleted."
    )
    hint(
        [
            "To keep this repo on its own login instead, cancel and add it "
            "under `claude_credentials.repos` in "
            "~/.config/jailbee/global.yaml:",
            "  claude_credentials:",
            "    repos:",
            f"      {container_prefix}: null",
            "then re-run `jailbee apply`.",
        ]
    )
    choices = [
        questionary.Choice(
            title=f"the group's login ({group_dir.name}) — delete this repo's copy",
            value="group",
        ),
        questionary.Choice(
            title="this repo's login — replaces the group's for every member repo",
            value="repo",
        ),
        # An explicit sentinel, not `value=None`: questionary falls back to
        # the *title* when a Choice's value is None, so cancelling would
        # return the label string and read as a valid answer.
        questionary.Choice(title="cancel — change nothing", value="cancel"),
    ]
    result = questionary.select(
        "Which login should the group keep?",
        choices=choices,
    ).ask()
    # `None` is questionary's own Ctrl-C / ESC answer, which means the same.
    if result is None or result == "cancel":
        return None
    # Spelled out rather than `return result`: `ask()` is typed `Any`, and
    # narrowing here is what keeps the Literal return honest.
    assert result in ("group", "repo"), f"unexpected picker answer: {result!r}"
    return "group" if result == "group" else "repo"


def confirm_destroy_risk(unknown: Sequence[str], summaries: Sequence[RiskSummary]) -> bool:
    """Print what a destroy would discard and ask; return True to proceed.

    Shared "print the summary and confirm" step for every destroy path
    (`jailbee destroy`'s single-name/`--all`/interactive-picker flavors, and
    `jailbee git pull`'s post-merge cleanup) so "risky to destroy" has one
    rendering and one confirmation instead of several that could drift.
    Each caller does its own gather-and-assess (`destroy_guard.assess` needs
    a `ContainerInfo` with a probed `GitStatus`, which differs per caller)
    and hands the results here.

    Returns True immediately when there is nothing to report, so the
    caller's own confirmation stays the only prompt in the common case. A
    container whose git status is unknown still gets a note — silence is
    never rendered as safety. That note's wording comes from
    `destroy_guard.unknown_status_warning` so the Qt dashboard's confirm
    dialog says the same thing about the same container. Never refuses
    outright: the worst outcome is one more prompt.
    """
    if unknown:
        from jailbee.destroy_guard import unknown_status_warning

        warn(unknown_status_warning(unknown))
    if not summaries:
        return True
    for summary in summaries:
        warn(summary.line)
    return typer.confirm("   Destroying loses this. Continue?", default=False)


def _choice_widths(containers: list[ContainerInfo]) -> dict[str, int]:
    # Function-local import: `background` pulls in sqlmodel, and `tui` sits
    # on the `jailbee --help` path, which stays fast deliberately. `lifecycle`
    # also imports `tui`, so a module-level `lifecycle`/`background` import
    # here would risk a circular import too.
    from jailbee import background

    return {
        "name": max(len(c.display_name) for c in containers),
        "state": max(len(c.state) for c in containers),
        "net": max(len(c.network or "-") for c in containers),
        "ip": max(len(c.ip or "-") for c in containers),
        "base": max(len(c.base_branch or "—") for c in containers),
        "wt": max(len(c.git_status.wt if c.git_status else "—") for c in containers),
        "ahead": max(len(c.git_status.ahead_diff if c.git_status else "—") for c in containers),
        "count": max(len(c.git_status.ahead_count if c.git_status else "—") for c in containers),
        "conflict": max(len(c.git_status.conflict if c.git_status else "—") for c in containers),
        "job": max(
            len(background.job_label_or_empty(c.job_phase, c.job_pid, kind=c.job_kind))
            for c in containers
        ),
    }


def _format_choice_title(c: ContainerInfo, widths: dict[str, int]) -> str:
    from jailbee import background

    base = c.base_branch or "—"
    if c.git_status is None:
        wt = "—"
        ahead = "—"
        count = "—"
        conflict = "—"
    else:
        wt = c.git_status.wt
        ahead = c.git_status.ahead_diff
        count = c.git_status.ahead_count
        conflict = c.git_status.conflict
    line = (
        f"{c.display_name:<{widths['name']}}  "
        f"{c.state:<{widths['state']}}  "
        f"{(c.network or '-'):<{widths['net']}}  "
        f"{(c.ip or '-'):<{widths['ip']}}  "
        f"{base:<{widths['base']}}  "
        f"{wt:<{widths['wt']}}  "
        f"{ahead:<{widths['ahead']}}  "
        f"{count:>{widths['count']}}  "
        f"{conflict:<{widths['conflict']}}"
    )
    if widths["job"]:
        label = background.job_label_or_empty(c.job_phase, c.job_pid, kind=c.job_kind)
        line += f"  {label:<{widths['job']}}"
    return line


def checkbox(
    message: str,
    choices: Sequence[Any],
    *,
    default: str | None = None,
    validate: Callable[[list[Any]], bool | str] = lambda _a: True,
    qmark: str | None = None,
    pointer: str | None = None,
    style: Any = None,
    initial_choice: Any = None,
    use_arrow_keys: bool = True,
    use_jk_keys: bool = True,
    use_emacs_keys: bool = True,
    use_search_filter: str | bool | None = False,
    instruction: str | None = None,
    show_description: bool = True,
    default_to_pointed: bool = True,
    **kwargs: Any,
) -> list[Any] | None:
    """Multi-select prompt with a single-select fallback on bare Enter.

    Forked from ``questionary.checkbox`` (2.1.1). Behaviour matches the
    upstream prompt except for the Enter key:

    * Space toggles items as usual.
    * If the user presses Enter without ever toggling anything (i.e.
      ``selected_options`` is empty) and ``default_to_pointed`` is True,
      the currently highlighted row is returned as a single-item list.
    * If at least one item has been toggled, Enter returns those
      selections — unchanged from upstream behaviour.

    Returns the selected values, or ``None`` on Ctrl+C / Ctrl+Q.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.styles import Style
    from questionary import utils
    from questionary.constants import (
        DEFAULT_QUESTION_PREFIX,
        DEFAULT_SELECTED_POINTER,
        INVALID_INPUT,
    )
    from questionary.prompts import common
    from questionary.prompts.common import InquirerControl, Separator
    from questionary.question import Question
    from questionary.styles import merge_styles_default

    if qmark is None:
        qmark = DEFAULT_QUESTION_PREFIX
    if pointer is None:
        pointer = DEFAULT_SELECTED_POINTER

    if not (use_arrow_keys or use_jk_keys or use_emacs_keys):
        raise ValueError(
            "Some option to move the selection is required. Arrow keys or j/k or Emacs keys."
        )
    if use_jk_keys and use_search_filter:
        raise ValueError(
            "Cannot use j/k keys with prefix filter search, since j/k can be part of the prefix."
        )
    if not callable(validate):
        raise ValueError("validate must be callable")

    merged_style = merge_styles_default([Style([("bottom-toolbar", "noreverse")]), style])

    ic = InquirerControl(
        choices,
        default,
        pointer=pointer,
        initial_choice=initial_choice,
        show_description=show_description,
    )

    def get_prompt_tokens() -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        tokens.append(("class:qmark", qmark))
        tokens.append(("class:question", f" {message} "))

        if ic.is_answered:
            nbr_selected = len(ic.selected_options)
            if nbr_selected == 0:
                tokens.append(("class:answer", "done"))
            elif nbr_selected == 1:
                title = ic.get_selected_values()[0].title
                if isinstance(title, list):
                    tokens.append(
                        (
                            "class:answer",
                            "".join([t[1] for t in title]),
                        )
                    )
                else:
                    tokens.append(("class:answer", f"[{title}]"))
            else:
                tokens.append(("class:answer", f"done ({nbr_selected} selections)"))
        else:
            if instruction is not None:
                tokens.append(("class:instruction", instruction))
            else:
                hint_enter = "<enter> on highlighted, " if default_to_pointed else ""
                tokens.append(
                    (
                        "class:instruction",
                        "(Use arrow keys to move, "
                        f"{hint_enter}"
                        "<space> to select, "
                        f"<{'ctrl-a' if use_search_filter else 'a'}> to toggle, "
                        f"<{'ctrl-a' if use_search_filter else 'i'}> to invert"
                        f"{', type to filter' if use_search_filter else ''})",
                    )
                )
        return tokens

    def get_selected_values() -> list[Any]:
        return [c.value for c in ic.get_selected_values()]

    def perform_validation(selected_values: list[Any]) -> bool:
        verdict = validate(selected_values)
        valid = verdict is True
        error_message: FormattedText | None = None
        if not valid:
            if verdict is False:
                error_text = INVALID_INPUT
            else:
                error_text = str(verdict)
            error_message = FormattedText([("class:validation-toolbar", error_text)])
        # InquirerControl.error_message is untyped in questionary (set to None in
        # __init__), so mypy infers it as None. Mirrors the upstream cast in
        # questionary.prompts.checkbox.
        ic.error_message = (
            error_message if not valid and ic.submission_attempted else None  # type: ignore[assignment]
        )
        return valid

    layout = common.create_inquirer_layout(ic, get_prompt_tokens, **kwargs)

    bindings = KeyBindings()

    @bindings.add(Keys.ControlQ, eager=True)
    @bindings.add(Keys.ControlC, eager=True)
    def _quit(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt, style="class:aborting")

    @bindings.add(" ", eager=True)
    def _toggle(_event: Any) -> None:
        pointed_choice = ic.get_pointed_at().value
        if pointed_choice in ic.selected_options:
            ic.selected_options.remove(pointed_choice)
        else:
            ic.selected_options.append(pointed_choice)
        perform_validation(get_selected_values())

    @bindings.add(Keys.ControlI if use_search_filter else "i", eager=True)
    def _invert(_event: Any) -> None:
        inverted_selection = [
            c.value
            for c in ic.choices
            if not isinstance(c, Separator)
            and c.value not in ic.selected_options
            and not c.disabled
        ]
        ic.selected_options = inverted_selection
        perform_validation(get_selected_values())

    @bindings.add(Keys.ControlA if use_search_filter else "a", eager=True)
    def _all(_event: Any) -> None:
        all_selected = True
        for c in ic.choices:
            if (
                not isinstance(c, Separator)
                and c.value not in ic.selected_options
                and not c.disabled
            ):
                ic.selected_options.append(c.value)
                all_selected = False
        if all_selected:
            ic.selected_options = []
        perform_validation(get_selected_values())

    def move_cursor_down(_event: Any) -> None:
        ic.select_next()
        while not ic.is_selection_valid():
            ic.select_next()

    def move_cursor_up(_event: Any) -> None:
        ic.select_previous()
        while not ic.is_selection_valid():
            ic.select_previous()

    if use_search_filter:

        def _search_filter(event: Any) -> None:
            ic.add_search_character(event.key_sequence[0].key)

        for character in string.printable:
            if character in string.whitespace:
                continue
            bindings.add(character, eager=True)(_search_filter)
        bindings.add(Keys.Backspace, eager=True)(_search_filter)

    if use_arrow_keys:
        bindings.add(Keys.Down, eager=True)(move_cursor_down)
        bindings.add(Keys.Up, eager=True)(move_cursor_up)
    if use_jk_keys:
        bindings.add("j", eager=True)(move_cursor_down)
        bindings.add("k", eager=True)(move_cursor_up)
    if use_emacs_keys:
        bindings.add(Keys.ControlN, eager=True)(move_cursor_down)
        bindings.add(Keys.ControlP, eager=True)(move_cursor_up)

    @bindings.add(Keys.ControlM, eager=True)
    def _set_answer(event: Any) -> None:
        # Fallback to pointed-at row when no item has been toggled with space.
        # This lets users single-select by just moving with arrows and hitting Enter.
        if default_to_pointed and not ic.selected_options:
            pointed = ic.get_pointed_at()
            if pointed is not None and not getattr(pointed, "disabled", False):
                ic.selected_options.append(pointed.value)

        selected_values = get_selected_values()
        ic.submission_attempted = True
        if perform_validation(selected_values):
            ic.is_answered = True
            event.app.exit(result=selected_values)

    @bindings.add(Keys.Any)
    def _other(_event: Any) -> None:
        """Disallow inserting other text."""

    question = Question(
        Application(
            layout=layout,
            key_bindings=bindings,
            style=merged_style,
            **utils.used_kwargs(kwargs, Application.__init__),
        )
    )
    result = question.ask()
    if result is None:
        return None
    return list(result)


def pick_container(containers: list[ContainerInfo]) -> str | None:
    """Interactive arrow-key picker for managed containers.

    Returns the chosen container's name, or None if the user cancels
    (Ctrl+C / ESC). Caller is responsible for the TTY check — this
    function unconditionally renders the picker.
    """
    import questionary

    widths = _choice_widths(containers)
    choices = [
        questionary.Choice(title=_format_choice_title(c, widths), value=c.name) for c in containers
    ]
    result = questionary.select(
        "Select a container:",
        choices=choices,
        use_shortcuts=True,
    ).ask()
    if result is None:
        return None
    return str(result)


def pick_containers_multi(
    containers: list[ContainerInfo],
    *,
    message: str = "Select containers to destroy:",
) -> list[str] | None:
    """Interactive checkbox picker for managed containers.

    Returns the chosen containers' full names (possibly empty), or None
    if the user cancels (Ctrl+C / ESC). Caller is responsible for the
    TTY check — this function unconditionally renders the picker.

    Bare Enter (no space-toggles) selects the highlighted row — see
    :func:`checkbox`.
    """
    import questionary

    widths = _choice_widths(containers)
    choices = [
        questionary.Choice(title=_format_choice_title(c, widths), value=c.name) for c in containers
    ]
    result = checkbox(message, choices=choices)
    if result is None:
        return None
    return [str(v) for v in result]


def _claude_choice_title(slot: Slot, width: int) -> str:
    """One picker line: the account, then its organization when it has one.

    Mirrors `jailbee claude ls`'s split — the account column carries
    `display_name`, so the organization is not repeated inside it.
    """
    if not slot.org_hint:
        # No padding: nothing follows, and trailing spaces are only whitespace
        # for the terminal to render.
        return slot.display_name
    return f"{slot.display_name:<{width}}  {slot.org_hint}"


def pick_claude_account(slots: Sequence[Slot], *, message: str) -> str | None:
    """Arrow-key picker over stored Claude logins. Returns the slot *name*.

    The name, not the `Slot`: it is what `claude use`/`claude rm` resolve, and
    resolving again under the credential locks is what keeps one resolution
    authoritative when another process is touching the store.

    Returns None if the user cancels (Ctrl+C / ESC). Caller is responsible for
    the TTY check — this function unconditionally renders the picker.
    """
    import questionary

    width = max(len(s.display_name) for s in slots)
    choices = [
        questionary.Choice(title=_claude_choice_title(s, width), value=s.name) for s in slots
    ]
    result = questionary.select(message, choices=choices, use_shortcuts=True).ask()
    if result is None:
        return None
    return str(result)


def pick_container_for_group(cfg: Config, incus: Incus, names: Sequence[str]) -> str | None:
    """Arrow-key picker over this repo's containers. Returns the name.

    Each row carries the container's current credential group, because
    that is the fact the user is about to change. Returns None if the user
    cancels; the caller does the TTY check.
    """
    import questionary

    from jailbee import claude_groups

    width = max(len(n) for n in names)
    choices = []
    for name in names:
        group = claude_groups.effective_group(cfg, incus, name)
        label = "no group" if group is None else group
        choices.append(questionary.Choice(title=f"{name:<{width}}  {label}", value=name))
    result = questionary.select("Change the group of:", choices=choices).ask()
    return None if result is None else str(result)


_PLAN_HEADINGS = {
    "push": "Push  host ──▶ container",
    "pull": "Pull  container ──▶ host",
    "checkout": "Checkout  container ──▶ host",
}


def _plan_ref_line(summary: RefSummary, width: int) -> str:
    """One ref as 'label  <short-oid> "subject"', absent parts dropped.

    ``width`` pads the label so the source and target OIDs line up in a column;
    a summary with no tip at all renders as the bare label, unpadded.
    """
    tip = summary.oid[:7] if summary.oid else ""
    if summary.subject:
        tip = f'{tip} "{summary.subject}"'.strip()
    if not tip:
        return summary.label
    return f"{summary.label.ljust(width)}  {tip}"


def render_bridge_plan(plan: BridgePlan) -> str:
    """Render a `BridgePlan` as the block shown before a bridge operation.

    Plain text on purpose: branch names may contain Rich markup characters
    (``feat/[wip]``), so callers print this with ``markup=False`` rather than
    letting Rich reinterpret user data. Lines whose value is unavailable are
    dropped instead of showing a placeholder.
    """
    width = max(len(plan.source.label), len(plan.target.label))
    lines = [
        _PLAN_HEADINGS[plan.direction],
        f"  container : {plan.container_short}  ({plan.container_full}, {plan.container_state})",
        f"  source    : {_plan_ref_line(plan.source, width)}",
        f"  target    : {_plan_ref_line(plan.target, width)}",
    ]
    if plan.incoming is not None:
        lines.append(f"            : {plan.incoming} commit(s) to apply")
    lines.append(f"  action    : {plan.action}")
    lines.extend(f"  ⚠ {note}" for note in plan.notes)
    return "\n".join(lines)
