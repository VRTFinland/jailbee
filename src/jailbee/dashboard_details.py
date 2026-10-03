"""The terminal dashboard's details panel: everything known about one row.

Pure — no I/O. Container values are composed from the same
``FieldSpec.cell`` functions the table uses (``lifecycle.ls_field_specs``),
so the panel and the table never format the same fact differently. The
panel shows a fixed set of rows, one per concept, so it keeps its shape as
the cursor moves; columns that say the same thing twice in the table
(``agent``/``agent_compact``, the git columns and ``git_status``, ``network``
/``ttl``/``loose_until``) collapse into one row here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from rich import box
from rich.console import Console, ConsoleOptions, RenderResult
from rich.markup import escape
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.table import Table

from jailbee.lifecycle import ContainerInfo, format_duration_short, ls_field_specs

if TYPE_CHECKING:
    from jailbee.dashboard import RepoGroup, Row

DETAILS_MAX_ROWS = 8  # content rows inside the border
DETAILS_PAIR_WIDTH = 36  # columns one label/value pair needs before another fits
DETAILS_MAX_PAIRS = 3
_DASH = "[dim]—[/dim]"


@dataclass(frozen=True)
class DetailItem:
    """One label/value row; ``value`` is Rich markup."""

    label: str
    value: str


@dataclass(frozen=True)
class DetailsView:
    """What the panel shows: a title and its rows, before any sizing."""

    title: str
    items: tuple[DetailItem, ...]


def _or_dash(value: str) -> str:
    return value if value.strip() else _DASH


def container_details(c: ContainerInfo, now: datetime) -> list[DetailItem]:
    """The container's rows, most important first — a cut panel loses its tail."""
    cells = {f.name: f.cell for f in ls_field_specs(now=now, all_repos=False)}

    def cell(name: str) -> str:
        return cells[name](c)

    state = cell("state")
    if c.job_phase is not None:
        state += f" · {cell('job')}"
    error = c.job_error.strip().splitlines()[0] if c.job_error and c.job_error.strip() else ""
    if error:
        state += f" · [red]{escape(error)}[/red]"

    network = c.network or "—"
    if c.network == "loose":
        ttl = cell("ttl")
        network += (
            f" ({ttl}, until {c.loose_until.astimezone():%H:%M})"
            if c.loose_until is not None
            else f" ({ttl})"
        )
    network += f" · {c.ip or '—'}"

    git = (
        _DASH
        if c.git_status is None
        else (
            f"wt {cell('wt')} · ↑{cell('ahead_count')} ↓{cell('behind_count')}"
            f" · ± {cell('target_diff')} · {cell('conflict')}"
            f" · local ↑{cell('local_count')} ± {cell('local_diff')}"
        )
    )
    # Every busy process, unlike the cell, which stops at DOING_MAX_NAMES.
    doing = ", ".join(
        escape(p.comm) if p.count == 1 else f"{escape(p.comm)} x{p.count}" for p in c.activity
    )
    issues = cell("issues")
    github = " · ".join(part for part in (cell("pr"), f"issues {issues}" if issues else "") if part)
    created = cell("created")
    if c.created_at is not None:
        created = f"{format_duration_short(now - c.created_at)} ago · {created}"

    return [
        DetailItem("state", state),
        DetailItem("network", network),
        DetailItem("git", git),
        DetailItem("agent", cell("agent")),
        DetailItem("doing", _or_dash(doing)),
        DetailItem("resources", f"mem {cell('mem')} · cpu {cell('cpu')}"),
        DetailItem("mode / base", f"{escape(c.mode)} · {cell('base')}"),
        DetailItem("github", _or_dash(github)),
        DetailItem(
            "group",
            escape(c.credential_group) if c.credential_group else "[dim]inherits repo[/dim]",
        ),
        DetailItem("created", created),
        DetailItem("mounts", _or_dash(", ".join(escape(m) for m in c.optional_mounts))),
    ]


def repo_details(group: RepoGroup) -> list[DetailItem]:
    """A repo heading's rows: what :class:`RepoGroup` knows about the repo."""
    running = sum(1 for c in group.containers if c.state == "Running")
    if group.repo_root is None:
        root, config = "[yellow]orphan[/yellow]", _DASH
    else:
        root = escape(group.repo_root)
        config = (
            escape(str(group.config_path))
            if group.config_path is not None
            else "[dim]synthesized[/dim]"
        )
    return [
        DetailItem("root", root),
        DetailItem("config", config),
        DetailItem("containers", f"{running} running / {len(group.containers)}"),
        DetailItem("loose ttl", group.loose_ttl_default or "[dim]no auto-revert[/dim]"),
        DetailItem("mounts", _or_dash(", ".join(escape(m) for m in group.optional_mounts))),
    ]


def details_for(
    groups: Sequence[RepoGroup], selected: Row | None, now: datetime
) -> DetailsView | None:
    """The panel for ``selected``, or None when there is nothing to describe.

    A container destroyed since the cursor landed on it simply has no panel
    until the selection is reconciled on the next frame.
    """
    if selected is None:
        return None
    if selected.kind == "repo":
        group = next((g for g in groups if g.prefix == selected.key), None)
        return None if group is None else DetailsView(group.prefix, tuple(repo_details(group)))
    for g in groups:
        for c in g.containers:
            if c.name == selected.key:
                return DetailsView(c.name, tuple(container_details(c, now)))
    return None


@dataclass(frozen=True)
class _DetailsGrid:
    """The label/value grid, flowed into as many pairs per line as fit, and
    cut to ``max_rows`` lines with a dim ``…`` as the last one, or — with
    ``fixed`` — padded with blank lines up to exactly ``max_rows``, so the
    panel keeps one shape whatever it describes."""

    items: tuple[DetailItem, ...]
    max_rows: int | None
    fixed: bool = False

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        pairs = max(1, min(DETAILS_MAX_PAIRS, options.max_width // DETAILS_PAIR_WIDTH))
        grid = Table.grid(padding=(0, 1), expand=True)
        for _ in range(pairs):
            grid.add_column(style="bold", no_wrap=True)
            grid.add_column(ratio=1, overflow="fold")
        for start in range(0, len(self.items), pairs):
            chunk = self.items[start : start + pairs]
            cells: list[str] = []
            for item in chunk:
                cells += [item.label, item.value]
            cells += ["", ""] * (pairs - len(chunk))
            grid.add_row(*cells)
        lines = console.render_lines(grid, options.update(height=None), pad=False)
        if self.max_rows is not None and len(lines) > self.max_rows:
            keep = max(0, self.max_rows - 1)
            lines = [*lines[:keep], [Segment("…", Style(dim=True))]]
        if self.fixed and self.max_rows is not None:
            lines += [[] for _ in range(self.max_rows - len(lines))]
        for index, line in enumerate(lines):
            if index:
                yield Segment.line()
            yield from line


def render_details(view: DetailsView, max_rows: int | None, *, fixed: bool = False) -> Panel:
    """The details panel; ``max_rows`` caps its content rows (None: uncapped).

    ``fixed`` also pads it to exactly ``max_rows`` content rows."""
    return Panel(
        _DetailsGrid(view.items, max_rows, fixed),
        title=f"[bold]{escape(view.title)}[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
    )
