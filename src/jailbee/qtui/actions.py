"""Map dashboard menu verbs to concrete ``jailbee`` commands and launch specs.

Framework-free (no PySide6). Mirrors the TUI's dispatch: every action runs
``jailbee <verb> <name> --config <path>`` so the target repo's own config drives
behaviour. Interactive verbs (shell/tmux) need a terminal window; the rest
run in the background (ide/chrome self-detach via gui.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jailbee.qtui.terminal import TerminalSpec, build_terminal_command

# Verbs that need an interactive TTY, hence a host terminal window.
INTERACTIVE_VERBS: frozenset[str] = frozenset({"shell", "tmux"})

# Verbs that mutate irreversibly and warrant a confirmation dialog first.
_CONFIRM_VERBS: frozenset[str] = frozenset({"destroy"})

# Verbs routed through the CLI's attach guard. Not the same set as
# INTERACTIVE_VERBS: `ide`/`chrome` need no terminal but do hit the guard.
_ATTACH_VERBS: frozenset[str] = frozenset({"shell", "tmux", "ide", "chrome"})

# Verbs dispatched with `--force`, i.e. with the CLI's own question already
# answered. Two unrelated reasons land here: a confirm verb has been through
# the Qt dialog, and an attach verb's "continue anyway?" would only repeat the
# failed-job state the card already showed.
_ASSUME_YES_VERBS: frozenset[str] = _CONFIRM_VERBS | _ATTACH_VERBS

# Verbs whose duration the GUI must ask for before dispatching. The CLI
# prompts interactively, but the detached Popen child has no stdin, so the
# question has to be a Qt dialog and the answer an explicit flag.
ASKS_DURATION_VERBS: frozenset[str] = frozenset({"net loose"})


class TerminalNotFoundError(RuntimeError):
    """Raised when an interactive action is requested but no terminal emulator
    could be found on the host."""


@dataclass(frozen=True)
class ActionCommand:
    """A resolved jailbee command plus how the GUI should launch it."""

    argv: list[str]
    interactive: bool
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
) -> ActionCommand:
    """Build the jailbee command for ``verb`` on ``container`` under ``config_path``.

    ``verb`` may be a single token (``"shell"``) or a space-separated
    multi-token subcommand (``"net loose"``) — it's split into separate argv
    entries either way, so ``jailbee net loose <container> --config <path>``
    dispatches correctly.

    Verbs in ``_ASSUME_YES_VERBS`` get ``--force`` appended, for two
    unrelated reasons. ``destroy`` has already been through the GUI's own
    confirmation dialog, and the detached ``Popen`` child has no interactive
    stdin, so the CLI's ``typer.confirm`` would read EOF and abort the
    operation silently. The attach verbs are forced for a different reason:
    their "continue anyway?" question would only restate the failed background
    job the card was already showing when the operator clicked.
    ``shell``/``tmux`` do get a real terminal, but ``ide``/``chrome`` would
    hang on the prompt in whatever terminal launched the GUI.

    ``duration`` appends ``--for <duration>`` for the verbs in
    ``ASKS_DURATION_VERBS``; passing it also clears ``duration_prompt`` so the
    GUI does not ask twice.
    """
    confirm = verb in _CONFIRM_VERBS
    argv = ["jailbee", *verb.split(), container, "--config", str(config_path)]
    if verb in _ASSUME_YES_VERBS:
        argv.append("--force")
    if duration is not None:
        argv += ["--for", duration]
    return ActionCommand(
        argv=argv,
        interactive=verb in INTERACTIVE_VERBS,
        confirm=confirm,
        duration_prompt=verb in ASKS_DURATION_VERBS and duration is None,
    )


def resolve_launch(action: ActionCommand, terminal: TerminalSpec | None) -> list[str]:
    """Return the final argv to spawn.

    Non-interactive actions run directly. Interactive actions are wrapped in
    the detected terminal emulator; if none was found, raise
    :class:`TerminalNotFoundError`.
    """
    if not action.interactive:
        return action.argv
    if terminal is None:
        raise TerminalNotFoundError(
            "No terminal emulator found for an interactive action. "
            "Set $JAILBEE_TERMINAL or install one (e.g. gnome-terminal, konsole, xterm)."
        )
    return build_terminal_command(terminal, action.argv)
