"""Typer registration for session lifecycle hooks."""

import json

import typer

from jailbee.inbox import hooks
from jailbee.inbox.role import local_role


def register_hooks(inbox_app: typer.Typer) -> None:
    app = typer.Typer(help="Handle session lifecycle hooks.")
    inbox_app.add_typer(app, name="hook")

    @app.command("session-start")
    def session_start() -> None:
        result = hooks.session_start(hooks.read_input(), local_role())
        if result is not None:
            print(json.dumps(result))

    @app.command("stop")
    def stop() -> None:
        result = hooks.stop(hooks.read_input(), local_role())
        if result is not None:
            print(json.dumps(result))
