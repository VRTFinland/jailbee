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

# Modules that read stdin's TTY-ness for something other than "may I ask?",
# keyed by path relative to src/jailbee.
_NOT_A_PROMPT = {
    "prompting.py": "the predicate itself",
    "macos.py": "decides whether the delegated host command gets a pty",
    "dashboard/tui/session.py": (
        "a full-screen TUI needs a terminal on both ends, env override or not"
    ),
}


def _tty_probes(source: str) -> list[int]:
    """Line numbers of terminal-ness probes on stdin: `<x>.stdin.isatty()`,
    `os.isatty(...)`, and any use of `<x>.__stdin__` (the original stdin,
    which is how one would sidestep a patched `sys.stdin`)."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        stdin_isatty = (
            node.attr == "isatty"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "stdin"
        )
        os_isatty = (
            node.attr == "isatty" and isinstance(node.value, ast.Name) and node.value.id == "os"
        )
        if stdin_isatty or os_isatty or node.attr == "__stdin__":
            lines.append(node.lineno)
    return lines


def test_stdin_isatty_only_in_prompting() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        if path.relative_to(SRC).as_posix() in _NOT_A_PROMPT:
            continue
        offenders += [f"{path.relative_to(SRC)}:{line}" for line in _tty_probes(path.read_text())]
    assert offenders == [], (
        "use jailbee.prompting.is_interactive() to decide whether to ask: " + ", ".join(offenders)
    )


def test_the_tty_probe_checker_flags_what_it_should() -> None:
    assert _tty_probes("import os\nos.isatty(0)\n") == [2]
    assert _tty_probes("import sys\nsys.__stdin__.isatty()\n") == [2]
    assert _tty_probes("import sys\nsys.stdin.isatty()\n") == [2]
    assert _tty_probes("import sys\nsys.stdout.isatty()\n") == []
    assert _tty_probes("import sys\nsys.stderr.isatty()\n") == []


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
