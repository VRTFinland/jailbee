"""The missing-value policy, enforced over the whole tree (CLAUDE.md:
"Missing required values are asked for, never usage errors")."""

from __future__ import annotations

import ast
from pathlib import Path

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
