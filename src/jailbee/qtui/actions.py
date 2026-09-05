"""Map dashboard menu verbs to concrete ``jailbee`` commands and launch specs.

Framework-free (no PySide6). Mirrors the TUI's dispatch: every action runs
``jailbee <verb> <name>`` addressed at one repo — ``--config <path>`` for a
configured repo, the child's working directory for one with no config file
(see :class:`jailbee.dashboard.RepoTarget`) — so the target repo's own config
drives behaviour. How the GUI has to *run* that command differs per verb,
though: some need a real TTY, some exist only for the text they print, and the
rest are fire and forget — see :data:`LaunchMode`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from jailbee.dashboard import ATTACH_VERBS, PRINTING_VERBS
from jailbee.qtui.terminal import TerminalSpec, build_terminal_command

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.dashboard import RepoTarget

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

# Verbs that warrant a confirmation dialog before dispatching.
_CONFIRM_VERBS: frozenset[str] = frozenset({"destroy", "git pull"})

# Confirm verbs whose CLI *also* prompts, and so need --force to proceed without
# the stdin the GUI's detached child does not have. Only `destroy` is in that
# position: `jailbee git pull` has no --force option at all — passing one would
# be a usage error, not a shortcut — and given an explicit container name it
# asks nothing, so there is no prompt to skip.
_FORCE_ON_CONFIRM: frozenset[str] = frozenset({"destroy"})

# Verbs dispatched with `--force`, i.e. with the CLI's own question already
# answered. Two unrelated reasons land here: a confirm verb has been through
# the Qt dialog, and an attach verb's "continue anyway?" would only repeat the
# failed-job state the card already showed. :data:`ATTACH_VERBS` is not the same
# set as ``_TERMINAL_VERBS``: `ide`/`chrome` need no terminal but do hit the
# guard.
_ASSUME_YES_VERBS: frozenset[str] = _FORCE_ON_CONFIRM | ATTACH_VERBS


def launch_mode(verb: str) -> LaunchMode:
    """Which launch path ``verb`` needs.

    The "output" set is :data:`jailbee.dashboard.PRINTING_VERBS`, shared with the
    TUI rather than copied: a new printing verb must not be able to land in one
    front-end's list and be forgotten in the other's.
    """
    if verb in _TERMINAL_VERBS:
        return "terminal"
    if verb in PRINTING_VERBS:
        return "output"
    return "detached"


class TerminalNotFoundError(RuntimeError):
    """Raised when an interactive action is requested but no terminal emulator
    could be found on the host."""


@dataclass(frozen=True)
class ActionCommand:
    """A resolved jailbee command plus how the GUI should launch it.

    ``cwd`` is the repo root the child must run in. Never optional: it is how a
    repo with no config file is addressed at all, and every spawn site — the
    detached ``Popen``, the terminal wrapper, the output window's ``QProcess``
    — passes it straight through.
    """

    argv: list[str]
    launch: LaunchMode
    confirm: bool
    cwd: Path


def build_action(
    verb: str,
    container: str,
    target: RepoTarget,
    *,
    extra_flags: list[str] | None = None,
) -> ActionCommand:
    """Build the jailbee command for ``verb`` on ``container`` in ``target``.

    ``target`` says how to address the repo: ``--config <path>`` for a
    configured one, the child's cwd for a repo with no config file (see
    :class:`jailbee.dashboard.RepoTarget`). The cwd is set either way, so
    there is one launch path rather than two.

    ``verb`` may be a single token (``"shell"``) or a space-separated
    multi-token subcommand (``"net loose"``) — it's split into separate argv
    entries either way, so ``jailbee net loose <container> --config <path>``
    dispatches correctly.

    Verbs in ``_ASSUME_YES_VERBS`` get ``--force`` appended, for two
    unrelated reasons. ``destroy`` has already been through the GUI's own
    confirmation dialog, and the detached ``Popen`` child has no interactive
    stdin, so the CLI's ``typer.confirm`` would read EOF and abort the
    operation silently. Not every confirm verb is forced, though — see
    ``_FORCE_ON_CONFIRM``. The attach verbs are forced for a different
    reason: their "continue anyway?" question would only restate the failed
    background job the card was already showing when the operator clicked.
    ``shell``/``tmux`` do get a real terminal, but ``ide``/``chrome`` would
    hang on the prompt in whatever terminal launched the GUI.

    ``extra_flags`` are the answers the GUI collected for the questions the CLI
    would have prompted for — `net loose`'s ``--for <duration>``, `git push`'s
    merge/rebase choice, `pr`'s draft state (see :mod:`jailbee.qtui.prompts`).
    They go last, so nothing lands between the verb and its container name.

    The resolved ``launch`` mode comes from :func:`launch_mode`, so the caller
    never has to know which verbs want a terminal and which want their output
    captured — that knowledge stays in one place here.
    """
    confirm = verb in _CONFIRM_VERBS
    argv = ["jailbee", *verb.split(), container, *target.flags()]
    if verb in _ASSUME_YES_VERBS:
        argv.append("--force")
    if extra_flags:
        argv += extra_flags
    return ActionCommand(argv=argv, launch=launch_mode(verb), confirm=confirm, cwd=target.cwd())


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
