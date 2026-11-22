"""No command declares the same option name twice (e.g. `-c` for two options)."""

from __future__ import annotations

from collections import Counter

import typer

from jailbee.cli import app


def _walk(cmd, path):  # type: ignore[no-untyped-def]
    yield path, cmd
    for name, sub in getattr(cmd, "commands", {}).items():
        yield from _walk(sub, [*path, name])


def test_no_command_reuses_an_option_name() -> None:
    clashes = []
    for path, cmd in _walk(typer.main.get_command(app), ["jailbee"]):
        names = Counter(opt for param in cmd.params for opt in (*param.opts, *param.secondary_opts))
        dup = sorted(n for n, k in names.items() if k > 1 and n.startswith("-"))
        if dup:
            clashes.append(f"{' '.join(path)}: {', '.join(dup)}")
    assert clashes == []
