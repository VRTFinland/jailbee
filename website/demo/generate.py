"""Render demo terminal output using JailBee's own renderers.

The website shows real output over invented data: every table on the page
comes from the same functions the CLI calls, so a column that changes in
the code changes here too (and `tests/test_website.py` fails until the
scene is regenerated). Nothing here touches Incus, the network, or a real
container.

Run from the repo root:  uv run python website/demo/generate.py
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console

from jailbee import table_format
from jailbee.git_status import GitStatus
from jailbee.lifecycle import ContainerInfo, ls_field_specs

# Fixed so the output is byte-stable and the test can pin it.
NOW = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)
WIDTH = 118
FIELDS = "name,base,state,network,ttl,mem,wt,ahead_count,pr"

OUT = Path(__file__).parent / "scenes" / "generated"


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    # `_environ={}` makes the render independent of the process environment:
    # without it, `TERM=dumb`/`NO_COLOR=1` (set by tests/conftest.py for
    # every test, this generator's own test included) makes Rich treat the
    # console as a dumb terminal, which silently overrides both `width=`
    # and colour. Explicit `width=`/`force_terminal=`/`color_system=` must
    # win regardless of who imports this module or from where.
    console = Console(
        file=buffer,
        width=WIDTH,
        force_terminal=True,
        color_system="truecolor",
        no_color=False,
        _environ={},
    )
    return console, buffer


def _containers() -> list[ContainerInfo]:
    return [
        ContainerInfo(
            name="gisgro-feat-invoice-pdf",
            state="Running",
            network="strict",
            ip="10.42.0.117",
            memory_limit="8GiB",
            # Set so `_mem_cell` (lifecycle.py) shows `used / limit`, the same
            # branch a real `Running` container with a usage sample takes.
            memory_usage=int(3.7 * 1024**3),
            repo="gisgro",
            base_branch="main",
            created_at=NOW - timedelta(hours=3),
            git_status=GitStatus(
                wt="+12 -3", ahead_diff="+245 -18", ahead_count="3", conflict="ok"
            ),
        ),
        ContainerInfo(
            name="gisgro-fix-login-race",
            state="Running",
            network="loose",
            ip="10.42.0.118",
            memory_limit="8GiB",
            repo="gisgro",
            base_branch="main",
            loose_until=NOW + timedelta(minutes=41),
            created_at=NOW - timedelta(days=1),
            git_status=GitStatus(wt="clean", ahead_diff="+31 -4", ahead_count="1", conflict="ok"),
        ),
        ContainerInfo(
            name="gisgro-pr-482",
            state="Stopped",
            network="strict",
            ip=None,
            memory_limit="8GiB",
            repo="gisgro",
            base_branch="release/2.9",
            pr_number=482,
            created_at=NOW - timedelta(days=2),
            # `list_containers()` (lifecycle.py) only probes git status for a
            # `Running` container — a stopped row's `git_status` is always
            # None, which WT/↑/MERGE render as "—".
            git_status=None,
        ),
    ]


def render_ls() -> str:
    """The `jailbee ls` table, rendered by the code the command itself uses."""
    console, buffer = _console()
    table_format.emit(
        _containers(),
        ls_field_specs(now=NOW, all_repos=False, show_submodules=False),
        fmt="table",
        fields=FIELDS,
        console=console,
        title="jailbee containers",
        empty_message="[dim](no containers found)[/dim]",
    )
    return buffer.getvalue()


def render_net_switch() -> str:
    """The output of `jb net strict`/`jb net loose` — a transcription, not a render.

    This deliberately does not render `jailbee net status` — see
    `scenes/README.md` for why. `net_status_cmd` (`src/jailbee/cli.py`,
    near line 4855) is not renderable from synthetic rows: it shells out
    to a real ``systemctl --user is-active jailbee-net-refresh.timer``,
    queries a real SQLite database through `sqlmodel`'s
    `Session`/`get_engine()` for registered repos and pool sizes, and its
    `_print_loose_status()` tail calls `list_containers()` against a real
    `Incus()`. Faking any of that would mean stubbing Incus or a database,
    which this generator must not do.

    So instead of inventing a status table, this transcribes exactly what
    `jailbee net strict`/`jailbee net loose` print on success: the
    `success(...)` call in `_switch()` (`src/jailbee/cli.py`, line 4710),

        success(f"Container '{short_name(cfg, resolved)}' is now on network: {mode}")

    where `success()` (`src/jailbee/tui.py`, line 26) renders as

        console.print(f"[green]✓[/green] {msg}")

    The two containers below are the same ``strict`` and ``loose`` rows
    `render_ls()` uses, so the two scenes tell one consistent story.
    """
    console, buffer = _console()
    strict_container, loose_container = _containers()[0], _containers()[1]
    for container, mode in ((strict_container, "strict"), (loose_container, "loose")):
        name = container.display_name
        console.print(f"[green]✓[/green] Container '{name}' is now on network: {mode}")
    return buffer.getvalue()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ls.txt").write_text(render_ls())
    (OUT / "net-switch.txt").write_text(render_net_switch())


if __name__ == "__main__":
    main()
