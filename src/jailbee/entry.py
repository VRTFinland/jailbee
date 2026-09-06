"""Console entry point.

On macOS this delegates into a Linux VM before importing the (Linux-only) CLI;
on Linux `maybe_delegate` is a no-op and the normal Typer app runs.

It is also where a `top_level: true` app in `apps:` becomes a command:
`jailbee figma` is rewritten to `jailbee apps run figma` when — and only
when — `figma` is not already a real command.
"""

from __future__ import annotations

import sys


def _command_names() -> set[str]:
    """Every command name the Typer app registers.

    `app.registered_commands[].name` is `None` for a command declared
    without an explicit name, and `registered_groups[].name` is a
    `DefaultPlaceholder` — neither can be read directly. Converting to the
    underlying `typer.core.TyperGroup` via `typer.main.get_command` is the
    only reliable enumeration, and at ~28 ms it is cheap enough for the one
    path that needs it.
    """
    import typer.core
    import typer.main

    from jailbee.cli import app

    command = typer.main.get_command(app)
    if not isinstance(command, typer.core.TyperGroup):
        # A Typer app with subcommands always converts to a TyperGroup;
        # this only guards the type for mypy's sake.
        return set()
    return set(command.commands)


def _top_level_app_names() -> set[str]:
    """Names of `apps:` entries that asked to be top-level commands."""
    from pathlib import Path

    from jailbee.config import load_repo_config

    cfg = load_repo_config(Path.cwd())
    return {name for name, entry in cfg.apps.items() if entry.top_level}


def rewrite_app_argv(argv: list[str]) -> list[str]:
    """Turn `jailbee <app> ...` into `jailbee apps run <app> ...`.

    A registered command always wins, so this can never shadow built-in
    behaviour. Anything that is not a known command and not a known app is
    returned untouched, so Typer produces its own unknown-command error.

    Every failure mode of the config load — no repo, invalid YAML, a
    validation error — resolves to "not an app": a typo must not turn into
    a traceback from the config loader.
    """
    if not argv or argv[0].startswith("-"):
        return argv
    if argv[0] in _command_names():
        return argv
    try:
        apps = _top_level_app_names()
    except Exception:
        return argv
    if argv[0] in apps:
        return ["apps", "run", argv[0], *argv[1:]]
    return argv


def main() -> None:
    from jailbee.macos import BridgeError, maybe_delegate

    try:
        maybe_delegate(sys.argv[1:])  # on macOS this exits; on Linux it returns
    except BridgeError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e
    from jailbee.cli import app
    from jailbee.incus import IncusError

    # After maybe_delegate, so the macOS bridge always sees the user's own argv.
    sys.argv[1:] = rewrite_app_argv(sys.argv[1:])

    try:
        app()
    except IncusError as e:
        # An IncusError is jailbee's own diagnosis: it names the command that
        # failed and carries what incus wrote to stderr. Typer's traceback
        # hook would print a screenful of jailbee internals above it and bury
        # the one line the user needs — a host with no `incus` binary hit
        # exactly that, on every command. Report it and exit non-zero.
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from e
