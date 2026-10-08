"""Credential groups and logins for the dashboard: rows, argv builders, runner.

Everything here is pure except `run_cli_quiet`, the one subprocess seam. The
dashboard drives account changes by re-executing `jailbee`, never by reaching
into `accounts/` — the CLI stays the single place that knows the pool rules
(locks, running-agent refusals, parking). This module only shapes the rows the
listing prints and the argv the actions run.

Must not import `jailbee.dashboard.overlays` or anything under
`jailbee.dashboard.tui`: the dependency direction is
`dashboard.overlays -> dashboard.accounts`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

ACCOUNT_LS_FIELDS = "agent,group,account,state,repos,containers"

ACCOUNTS_HINT = (
    "[bold]↑/↓[/bold] move  ·  [bold]Enter[/bold] actions  ·  "
    "[bold]n[/bold] new group  ·  [bold]Esc[/bold] close"
)

# Column index -> widest a fixed-width column may grow (GROUP, AGENT, STATE).
_FIXED_COLUMNS = {0: 20, 1: 12, 3: 8}
# ACCOUNT and USED BY share the remaining width in these proportions.
_FLEX_RATIOS = {2: 1, 4: 1}

ACCOUNT_HEADERS = ("GROUP", "AGENT", "ACCOUNT", "STATE", "USED BY")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class AccountLoadError(Exception):
    """`jailbee account ls` printed something that is not a list of rows."""


@dataclass(frozen=True)
class AccountRow:
    """One line of `account ls`. `account` is the slot name `use`/`rm` take."""

    agent: str
    group: str | None
    account: str | None
    state: str  # "live" | "parked" | "empty"
    repos: tuple[str, ...]
    containers: tuple[str, ...]


@dataclass(frozen=True)
class CliResult:
    """Outcome of a quiet CLI run: `message` for a notice, `stdout` for loaders."""

    ok: bool
    message: str
    stdout: str = ""


@dataclass(frozen=True)
class AccountsState:
    rows: tuple[AccountRow, ...]
    start_index: int = 0
    prefix: str = ""


def account_ls_argv() -> list[str]:
    return ["account", "ls", "-o", "json", "--fields", ACCOUNT_LS_FIELDS]


def group_ls_argv() -> list[str]:
    """`account group ls`: the same row shape as `account ls`, groups only."""
    return ["account", "group", "ls", "-o", "json", "--fields", ACCOUNT_LS_FIELDS]


def _bad_output() -> AccountLoadError:
    return AccountLoadError("unexpected output from 'jailbee account ls'")


def _str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _bad_output()
    return tuple(value)


def _opt_str(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise _bad_output()
    return value


def parse_account_rows(stdout: str) -> tuple[AccountRow, ...]:
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise _bad_output() from exc
    if not isinstance(data, list):
        raise _bad_output()
    rows: list[AccountRow] = []
    for item in data:
        if not isinstance(item, dict):
            raise _bad_output()
        agent, state = item.get("agent"), item.get("state")
        if not isinstance(agent, str) or not isinstance(state, str):
            raise _bad_output()
        rows.append(
            AccountRow(
                agent=agent,
                group=_opt_str(item.get("group")),
                account=_opt_str(item.get("account")),
                state=state,
                repos=_str_tuple(item.get("repos")),
                containers=_str_tuple(item.get("containers")),
            )
        )
    return tuple(rows)


_VERDICT_MARKS = ("\u2713", "\u2717", "error")


def _is_verdict(line: str) -> bool:
    return line.lower().startswith(_VERDICT_MARKS)


def _verdict(text: str, *, whole_paragraph: bool) -> str:
    """The one line of CLI output that says what happened.

    The CLI prints `✓ ...` / `✗ ...` (or `error: ...`) first and hints after, so
    the verdict is the first line carrying a marker, else the first line. Rich
    hard-wraps output at 80 columns when piped, so a refusal may span several
    lines: `whole_paragraph` joins the continuation lines (up to a blank line or
    the next verdict) back into one. A success keeps only its first line; what
    follows is a hint.
    """
    lines = [_ANSI.sub("", ln).strip() for ln in text.splitlines()]
    start = next((i for i, ln in enumerate(lines) if ln and _is_verdict(ln)), None)
    if start is None:
        start = next((i for i, ln in enumerate(lines) if ln), None)
    if start is None:
        return ""
    parts = [lines[start]]
    if whole_paragraph:
        for ln in lines[start + 1 :]:
            if not ln or _is_verdict(ln):
                break
            parts.append(ln)
    # A success line wraps before its path ("Created group `g` for claude →").
    return " ".join(parts).rstrip(" \u2192")


def run_cli_quiet(argv: Sequence[str], *, cwd: Path, timeout: float = 60.0) -> CliResult:
    """Run `jailbee <argv>` capturing all output; never touches the terminal."""
    try:
        proc = subprocess.run(
            ["jailbee", *argv],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            cwd=cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return CliResult(False, "timed out")
    except (OSError, ValueError) as exc:  # ValueError: undecodable output
        return CliResult(False, str(exc))
    if proc.returncode == 0:
        return CliResult(True, _verdict(proc.stdout, whole_paragraph=False) or "done", proc.stdout)
    message = (
        _verdict(proc.stderr, whole_paragraph=True)
        or _verdict(proc.stdout, whole_paragraph=True)
        or f"exited {proc.returncode}"
    )
    return CliResult(False, message)


def use_argv(agent: str, group: str | None, ref: str) -> list[str]:
    argv = ["account", "use", ref, "-a", agent]
    if group is not None:
        argv += ["-g", group]
    return argv


def park_argv(agent: str, group: str | None) -> list[str]:
    argv = ["account", "park", "-a", agent]
    if group is not None:
        argv += ["-g", group]
    return argv


def rm_login_argv(agent: str, ref: str) -> list[str]:
    return ["account", "rm", ref, "-a", agent, "--yes"]


def group_create_argv(name: str) -> list[str]:
    return ["account", "group", "create", name]


def group_rm_argv(name: str) -> list[str]:
    return ["account", "group", "rm", name, "--yes"]


def repo_group_set_argv(group: str) -> list[str]:
    return ["account", "group", "set", group]


def repo_group_unset_argv() -> list[str]:
    return ["account", "group", "unset"]


def container_group_use_argv(group: str, container: str) -> list[str]:
    return ["account", "group", "use", group, container]


def container_group_reset_argv(container: str) -> list[str]:
    return ["account", "group", "reset", container]


def group_names(rows: Sequence[AccountRow]) -> tuple[str, ...]:
    return tuple(sorted({r.group for r in rows if r.group is not None}))


def parked_for(rows: Sequence[AccountRow], agent: str) -> tuple[AccountRow, ...]:
    return tuple(r for r in rows if r.state == "parked" and r.agent == agent)


def account_actions(row: AccountRow, rows: Sequence[AccountRow]) -> tuple[tuple[str, str], ...]:
    """`(label, action_id)` pairs the picker offers for `row`."""
    if row.state == "parked":
        actions: list[tuple[str, str]] = []
        if group_names(rows):
            actions.append(("Use in a group…", "use-in"))
        actions.append(("Delete this login…", "delete"))
        return tuple(actions)
    if row.group is None:
        return ()
    actions = []
    if parked_for(rows, row.agent):
        actions.append(("Use a stored login…", "use"))
    if row.state == "live":
        actions.append(("Park the live login", "park"))
    elif row.state == "empty":
        actions.append(("Remove this group", "group-rm"))
    return tuple(actions)


def account_lines(
    rows: Sequence[AccountRow], width: int
) -> tuple[tuple[Text, ...], tuple[Text, ...]]:
    """The panel's table at ``width``: its header lines, then exactly one line per row.

    GROUP, AGENT and STATE get a fixed width that fits their content (no-wrap
    columns all shrink proportionally, so a long login would squeeze them to
    nothing); ACCOUNT and USED BY share the rest. Drawn once by Rich and cut
    into lines so a list widget can own the cursor while the header stays put.
    """
    table = Table(box=box.SIMPLE_HEAD, expand=True, pad_edge=False, show_edge=False)
    cells_by_row = [
        (
            row.group or "-",
            row.agent,
            row.account or "-",
            row.state,
            ", ".join((*row.repos, *row.containers)) or "-",
        )
        for row in rows
    ]
    for column, header in enumerate(ACCOUNT_HEADERS):
        fixed = None
        if column in _FIXED_COLUMNS:
            fixed = min(
                max([len(header), *(len(cells[column]) for cells in cells_by_row)]),
                _FIXED_COLUMNS[column],
            )
        table.add_column(
            header, overflow="ellipsis", no_wrap=True, width=fixed, ratio=_FLEX_RATIOS.get(column)
        )
    for cells in cells_by_row:
        table.add_row(*(Text(c) for c in cells))
    console = Console(width=max(20, width), color_system=None, legacy_windows=False)
    # Explicit options: a dumb terminal's Console.size ignores the width given above.
    options = console.options.update(width=max(20, width), height=None)
    rendered = [
        Text.assemble(
            *((segment.text, segment.style or "") for segment in line if not segment.control)
        )
        for line in console.render_lines(table, options, pad=False)
    ]
    head = len(rendered) - len(rows)
    return tuple(rendered[:head]), tuple(rendered[head:])
