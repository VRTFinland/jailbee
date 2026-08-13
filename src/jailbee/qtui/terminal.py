"""Host terminal-emulator detection for interactive GUI actions.

Framework-free (no PySide6). Interactive verbs (shell, tmux) need a real
terminal window; under Wayland an external terminal cannot be reparented
into the Qt window, so v1 spawns a standalone host emulator.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

# Args each known emulator wants *before* the command to run. Emulators not
# listed here fall back to ``-e`` (the historical default).
_RUN_ARGS: dict[str, list[str]] = {
    "x-terminal-emulator": ["-e"],
    "xterm": ["-e"],
    "konsole": ["-e"],
    "alacritty": ["-e"],
    "gnome-terminal": ["--"],
    "ptyxis": ["--"],
    "foot": [],
    "kitty": [],
}

# Auto-detect priority when no $JAILBEE_TERMINAL override is set.
_DETECT_ORDER: list[str] = [
    "x-terminal-emulator",
    "ptyxis",
    "gnome-terminal",
    "konsole",
    "foot",
    "alacritty",
    "kitty",
    "xterm",
]


@dataclass(frozen=True)
class TerminalSpec:
    """A resolved terminal emulator and the args it wants before a command."""

    binary: str
    run_args: list[str]


def detect_terminal(
    *,
    env: Mapping[str, str],
    which: Callable[[str], str | None],
) -> TerminalSpec | None:
    """Find a terminal emulator to launch interactive verbs in.

    ``$JAILBEE_TERMINAL`` (if set) is tried first, then a known priority list.
    ``which`` resolves a binary name to a path or None. Returns None when no
    emulator is found.
    """
    candidates: list[str] = []
    override = env.get("JAILBEE_TERMINAL")
    if override:
        candidates.append(override)
    candidates.extend(_DETECT_ORDER)
    for name in candidates:
        if which(name):
            return TerminalSpec(binary=name, run_args=_RUN_ARGS.get(name, ["-e"]))
    return None


def build_terminal_command(spec: TerminalSpec, inner_argv: list[str]) -> list[str]:
    """Compose the full argv that launches ``inner_argv`` in a new terminal."""
    return [spec.binary, *spec.run_args, *inner_argv]
