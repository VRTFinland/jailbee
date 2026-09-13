"""Structural guard: every CLI parameter carries help text.

`jailbee --help` is the primary documentation for a flag — `docs/commands.md`
summarises commands, not every option — so an undocumented parameter is a
parameter nobody outside this repo can use. A whole batch of them accumulated
silently (`jailbee new --from-base`, `--net`, `--memory`, ... among them)
because nothing failed when one was added without `help=`.

Walks the real Typer command tree rather than a hand-maintained list, the same
way `test_completion_wiring.py` does; see that module's header for why the
isinstance checks key on Typer's own classes rather than on Click.
"""

from __future__ import annotations

import typer
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption

from jailbee.cli import app

# `--help` itself is Click's, added to every command; it has no help text of
# its own and is not ours to document.
EXEMPT_PARAM_NAMES = {"help"}


def _walk(cmd: TyperCommand | TyperGroup, path: str = ""):
    """Yield (command path, parameter) for every command in the tree."""
    here = f"{path} {cmd.name}".strip()
    if isinstance(cmd, TyperGroup):
        for sub in cmd.commands.values():
            yield from _walk(sub, here)
        return
    for param in cmd.params:
        yield here, param


def _documented(param: TyperArgument | TyperOption) -> bool:
    return bool((getattr(param, "help", None) or "").strip())


def test_every_cli_parameter_has_help_text():
    cli = typer.main.get_command(app)
    missing = [
        f"{cmd_path}:{param.name}"
        for cmd_path, param in _walk(cli)
        if param.name not in EXEMPT_PARAM_NAMES and not _documented(param)
    ]
    assert not missing, f"CLI parameters with no help text: {missing}"


def test_the_guard_walks_the_whole_tree():
    """A walk that yields nothing would make the guard above vacuously pass."""
    cli = typer.main.get_command(app)
    params = list(_walk(cli))
    commands = {cmd_path for cmd_path, _ in params}
    assert len(commands) > 50, f"only walked {len(commands)} commands"
    assert any(
        cmd_path == "jailbee new" and param.name == "from_base" for cmd_path, param in params
    ), "the walk missed `jailbee new --from-base`"
