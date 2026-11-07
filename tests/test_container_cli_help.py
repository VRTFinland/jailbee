"""Every in-container command, group, option and argument carries help text."""

import typer.main
from typer._click import Command
from typer.core import TyperGroup as Group

from jailbee.container_cli import container_app


def _walk(command: Command, path: str) -> list[str]:
    missing: list[str] = []
    if command.hidden:
        return missing
    if not (command.help or command.short_help):
        missing.append(f"command {path}")
    for param in command.params:
        if getattr(param, "hidden", False) or param.name == "help":
            continue
        if not getattr(param, "help", None):
            missing.append(f"{path} {param.opts[0] if param.opts else param.name}")
    if isinstance(command, Group):
        for name, sub in command.commands.items():
            missing.extend(_walk(sub, f"{path} {name}"))
    return missing


def test_every_container_command_and_option_has_help() -> None:
    root = typer.main.get_command(container_app)
    missing = _walk(root, "jailbee")
    assert not missing, "missing --help text:\n" + "\n".join(missing)
