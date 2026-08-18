"""Map dashboard menu verbs to concrete ``jailbee`` commands and launch specs.

Framework-free (no PySide6). Mirrors the TUI's dispatch: every action runs
``jailbee <verb> <name> --config <path>`` so the target repo's own config drives
behaviour. How the GUI has to *run* that command differs per verb, though: some
need a real TTY, some exist only for the text they print, and the rest are fire
and forget — see :data:`LaunchMode`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jailbee.qtui.terminal import TerminalSpec, build_terminal_command

# How the GUI has to run a verb.
#   "terminal" — needs an interactive TTY, so it gets a host terminal window.
#   "output"   — its point is the text it prints: run it under a QProcess and
#                show that text in a Qt window. None of these needs a TTY (each
#                degrades cleanly off-TTY), and Rich emits no ANSI escapes into
#                a pipe, so the text renders as-is.
#   "detached" — fire and forget: it self-detaches (ide/chrome) or has nothing
#                to say beyond its exit code.
LaunchMode = Literal["terminal", "output", "detached"]

_TERMINAL_VERBS: frozenset[str] = frozenset({"shell", "tmux"})

# Matched exactly, not by leading token: `pr --open` just opens a browser, and
# `job log` reaches the GUI in both its plain and its --follow form.
_OUTPUT_VERBS: frozenset[str] = frozenset(
    {"pr", "git push", "git pull", "git diff", "job log", "job log --follow"}
)


# Verbs that warrant a confirmation dialog before dispatching.
_CONFIRM_VERBS: frozenset[str] = frozenset({"destroy", "git pull"})

# Confirm verbs whose CLI *also* prompts, and so need --force to proceed
# without the stdin the GUI's detached child does not have. `git pull` with an
# explicit container name asks nothing, so it must not be forced — --force
# means something entirely different there.
_FORCE_ON_CONFIRM: frozenset[str] = frozenset({"destroy"})

# Verbs whose duration the GUI must ask for before dispatching. The CLI
# prompts interactively, but the detached Popen child has no stdin, so the
# question has to be a Qt dialog and the answer an explicit flag.
ASKS_DURATION_VERBS: frozenset[str] = frozenset({"net loose"})


def launch_mode(verb: str) -> LaunchMode:
    """Which launch path ``verb`` needs."""
    if verb in _TERMINAL_VERBS:
        return "terminal"
    if verb in _OUTPUT_VERBS:
        return "output"
    return "detached"


class TerminalNotFoundError(RuntimeError):
    """Raised when an interactive action is requested but no terminal emulator
    could be found on the host."""


@dataclass(frozen=True)
class ActionCommand:
    """A resolved jailbee command plus how the GUI should launch it."""

    argv: list[str]
    launch: LaunchMode
    confirm: bool
    # True when the verb needs a duration the caller has not supplied yet:
    # the GUI asks, then rebuilds the action with `duration=`.
    duration_prompt: bool = False


def build_action(
    verb: str,
    container: str,
    config_path: Path,
    *,
    duration: str | None = None,
    extra_flags: list[str] | None = None,
) -> ActionCommand:
    """Build the jailbee command for ``verb`` on ``container`` under ``config_path``.

    ``verb`` may be a single token (``"shell"``) or a space-separated
    multi-token subcommand (``"net loose"``) — it's split into separate argv
    entries either way, so ``jailbee net loose <container> --config <path>``
    dispatches correctly.

    Verbs in ``_FORCE_ON_CONFIRM`` (``destroy``) get ``--force`` appended: the
    GUI has already shown its own confirmation dialog, and the detached
    ``Popen`` child has no interactive stdin, so the CLI's own
    ``typer.confirm`` prompt would read EOF and abort the operation silently.
    Not every confirm verb is forced — see ``_FORCE_ON_CONFIRM``.

    ``duration`` appends ``--for <duration>`` for the verbs in
    ``ASKS_DURATION_VERBS``; passing it also clears ``duration_prompt`` so the
    GUI does not ask twice.

    ``extra_flags`` are the answers the GUI collected for the questions the CLI
    would have prompted for (see :mod:`jailbee.qtui.prompts`); they go last, so
    nothing lands between the verb and its container name.

    The resolved ``launch`` mode comes from :func:`launch_mode`, so the caller
    never has to know which verbs want a terminal and which want their output
    captured — that knowledge stays in one place here.
    """
    confirm = verb in _CONFIRM_VERBS
    argv = ["jailbee", *verb.split(), container, "--config", str(config_path)]
    if verb in _FORCE_ON_CONFIRM:
        argv.append("--force")
    if duration is not None:
        argv += ["--for", duration]
    if extra_flags:
        argv += extra_flags
    return ActionCommand(
        argv=argv,
        launch=launch_mode(verb),
        confirm=confirm,
        duration_prompt=verb in ASKS_DURATION_VERBS and duration is None,
    )


def resolve_launch(action: ActionCommand, terminal: TerminalSpec | None) -> list[str]:
    """Return the final argv to spawn.

    Only ``"terminal"`` actions are wrapped in the detected terminal emulator;
    if none was found, raise :class:`TerminalNotFoundError`. ``"output"`` and
    ``"detached"`` actions run their argv as-is — the GUI owns them, either
    capturing what they print or letting them detach — so a host with no
    terminal emulator at all can still dispatch them.
    """
    if action.launch != "terminal":
        return action.argv
    if terminal is None:
        raise TerminalNotFoundError(
            "No terminal emulator found for an interactive action. "
            "Set $JAILBEE_TERMINAL or install one (e.g. gnome-terminal, konsole, xterm)."
        )
    return build_terminal_command(terminal, action.argv)
