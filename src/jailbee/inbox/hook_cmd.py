"""Typer registration for session lifecycle hooks."""

import json
import os
import pwd
from pathlib import Path

from jailbee.agent_snapshot import register_session, window_names

import typer

from jailbee.inbox import hooks
from jailbee.inbox.role import local_role


def register_hooks(inbox_app: typer.Typer) -> None:
    app = typer.Typer(help="Handle session lifecycle hooks.")
    inbox_app.add_typer(app, name="hook")

    @app.command("session-start")
    def session_start() -> None:
        """Provide context when a session resumes or forks."""
        data = hooks.read_input()
        try:
            if isinstance(data.get("session_id"), str) and isinstance(data.get("cwd"), str):
                register_session(
                    Path(pwd.getpwuid(os.getuid()).pw_dir), data,
                    windows=window_names(timeout=0.2) if os.environ.get("TMUX") else {},
                )
        except Exception:
            pass  # Session bookkeeping must never prevent Claude from starting.
        result = hooks.session_start(data, local_role())
        if result is not None:
            print(json.dumps(result))

    @app.command("session-end")
    def session_end() -> None:
        """Remove a session that Claude has ended normally."""
        data = hooks.read_input()
        try:
            register_session(Path(pwd.getpwuid(os.getuid()).pw_dir), data, ended=True)
        except Exception:
            pass  # Best effort, including a busy or unwritable registry.

    @app.command("stop")
    def stop() -> None:
        """Keep a child inbox waiter armed before stopping."""
        result = hooks.stop(hooks.read_input(), local_role())
        if result is not None:
            print(json.dumps(result))
