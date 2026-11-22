"""Every `jailbee` command line in docs/everyday.md names a real command and flags."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import typer.main
from typer import _click as click
from typer.core import TyperGroup

from jailbee.cli import app

PAGE = Path(__file__).resolve().parents[1] / "docs" / "everyday.md"


def _command_lines() -> list[str]:
    text = PAGE.read_text()
    lines: list[str] = []
    for block in re.findall(r"```bash\n(.*?)```", text, re.S):
        for raw in block.splitlines():
            line = re.split(r"\s+#\s", raw)[0].strip()
            if line.startswith(("jailbee ", "jb ")):
                lines.append(line)
    return lines


def _check(line: str) -> None:
    root = typer.main.get_command(app)
    ctx = click.Context(root)
    cmd: click.Command = root
    words = shlex.split(line)[1:]
    rest: list[str] = []
    for i, word in enumerate(words):
        if isinstance(cmd, TyperGroup) and word in cmd.commands:
            cmd = cmd.commands[word]
            continue
        rest = words[i:]
        break
    assert not isinstance(cmd, TyperGroup) or not rest, f"unknown command in {line!r}"
    known = {o for p in cmd.get_params(ctx) for o in (*p.opts, *p.secondary_opts)}
    for word in rest:
        if word == "--":
            break
        if word.startswith("-"):
            assert word.split("=")[0] in known, f"unknown flag {word} in {line!r}"


def test_the_page_has_runnable_command_sequences() -> None:
    assert len(_command_lines()) >= 20


@pytest.mark.parametrize("line", _command_lines())
def test_each_documented_command_exists(line: str) -> None:
    _check(line)
