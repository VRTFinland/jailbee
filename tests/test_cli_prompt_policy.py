"""The missing-value policy, enforced over the whole tree (CLAUDE.md:
"Missing required values are asked for, never usage errors")."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import typer.main
from typer._click import Context

from jailbee.cli import app

SRC = Path(__file__).resolve().parents[1] / "src" / "jailbee"

# Modules that read stdin's TTY-ness for something other than "may I ask?".
_NOT_A_PROMPT = {
    "prompting.py": "the predicate itself",
    "macos.py": "decides whether the delegated host command gets a pty",
    "dashboard.py": "a full-screen TUI needs a terminal on both ends, env override or not",
}


def test_stdin_isatty_only_in_prompting() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name in _NOT_A_PROMPT and path.parent == SRC:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "isatty"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "stdin"
            ):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == [], (
        "use jailbee.prompting.is_interactive() to decide whether to ask: " + ", ".join(offenders)
    )


def _walk(cmd: Any, path: str) -> Iterator[tuple[str, Any]]:
    # Typer vendors Click: an isinstance check against top-level `click.Group`
    # is always False here, so groups are recognised by duck typing.
    if hasattr(cmd, "list_commands"):
        ctx = Context(cmd)
        for name in cmd.list_commands(ctx):
            sub = cmd.get_command(ctx, name)
            if sub is not None:
                yield from _walk(sub, f"{path} {name}")
    else:
        yield path, cmd


def test_no_command_has_a_required_positional() -> None:
    root = typer.main.get_command(app)
    offenders = [
        f"{path} {param.name}"
        for path, cmd in _walk(root, "jailbee")
        for param in cmd.params
        if param.param_type_name == "argument" and param.required
    ]
    assert offenders == [], (
        "a missing required value is asked for, never a usage error "
        "(CLAUDE.md, jailbee.prompting): " + ", ".join(offenders)
    )


def test_the_walk_sees_the_whole_tree() -> None:
    paths = {p for p, _ in _walk(typer.main.get_command(app), "jailbee")}
    assert {
        "jailbee exec",
        "jailbee snapshot restore",
        "jailbee account group create",
        "jailbee outbox show",
    } <= paths
