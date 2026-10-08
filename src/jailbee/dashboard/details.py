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
from rich.table import Table
from rich.text import Text

from jailbee.agent_activity import describe
from jailbee.lifecycle import (
    ContainerInfo,
    format_duration_short,
    ls_field_specs,
    submodule_sub_rows,
)

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoGroup, Row

DETAILS_MAX_ROWS = 8  # content rows inside the border, for the label/value grid
DETAILS_ACTIVITY_ROWS = 3  # rows every view reserves under it while any container has activity
_BLANK = Segment("")  # a blank line the panel's line splitter keeps even at the very end
_MIN_GRID_ROWS = 2  # the grid always keeps these, whatever the activity asks for
DETAILS_PAIR_WIDTH = 36  # columns one label/value pair needs before another fits
DETAILS_MAX_PAIRS = 3
PANEL_INSET_COLS = 4  # the panel's border and padding, either side
_DASH = "[dim]—[/dim]"


@dataclass(frozen=True)
class DetailItem:
    """One label/value row; ``value`` is Rich markup."""

    label: str
    value: str
    group: str | None = None


@dataclass(frozen=True)
class DetailsView:
    """What the panel shows: a title and its rows, before any sizing.

    ``activity`` is the agent's head and tool lines, ``message`` its last
    message and ``history`` its recent steps, newest first — all Rich markup,
    already escaped. ``reserve_rows`` is how many rows under the grid every
    view is guaranteed while any container has activity, so the table keeps
    its height as the cursor moves; the panel grows past it only into rows
    the table does not need.
    """

    title: str
    items: tuple[DetailItem, ...]
    activity: tuple[str, ...] = ()
    reserve_rows: int = 0
    message: str | None = None
    history: tuple[str, ...] = ()

    @property
    def base_rows(self) -> int:
        """Content rows the panel is guaranteed: the grid's cap plus the reservation."""
        return DETAILS_MAX_ROWS + self.reserve_rows


@dataclass(frozen=True)
class ActivityBlock:
    """A container's agent activity as escaped Rich markup; empty when it has none."""

    lines: tuple[str, ...] = ()
    message: str | None = None
    history: tuple[str, ...] = ()


def activity_block(c: ContainerInfo, now: datetime) -> ActivityBlock:
    """The container's agent activity, or an empty block.

    The first agent summary that carries activity speaks (summaries are most
    urgent first). Every part is text an agent wrote, so it is escaped here.
    History tool entries are dim, message entries plain. The newest tool
    event is left out of the history when the tool line shows it, and the
    newest message event when ``message`` shows it: ``last_tool`` /
    ``last_message`` are by construction those same newest events, so the
    history starts with what came before them.
    """
    for summary in c.agent_status:
        text = describe(summary, now)
        if text is None:
            continue
        lines = [escape(text.head)]
        if text.tool is not None:
            lines.append(escape(f"↳ {text.tool}"))
        message = None if text.message is None else f"[dim]{escape(f'“{text.message}”')}[/dim]"
        skip = {"tool": text.tool is not None, "message": text.message is not None}
        earlier: list[str] = []
        for event in reversed(text.recent):
            if skip.get(event.kind):
                skip[event.kind] = False  # only the newest of each kind is shown above
                continue
            earlier.append(
                f"[dim]{escape(event.text)}[/dim]"
                if event.kind == "tool"
                else escape(f"“{event.text}”")
            )
        history = tuple(earlier)
        return ActivityBlock(tuple(lines), message, history)
    return ActivityBlock()


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
        [DetailItem("git", _DASH, "git")]
        if c.git_status is None
        else [
            DetailItem("git wt", cell("wt"), "git"),
            DetailItem("commits", f"↑{cell('ahead_count')} ↓{cell('behind_count')}", "git"),
            DetailItem("target +/-", cell("target_diff"), "git"),
            DetailItem("conflict", cell("conflict"), "git"),
            DetailItem(
                "local +/-",
                f"↑{cell('local_count')} · {cell('local_diff')}",
                "git",
            ),
        ]
    )
    submodules: list[DetailItem] = []
    if c.git_status is not None:
        sub_rows = submodule_sub_rows(c)
        for index, (sub, row) in enumerate(zip(c.git_status.submodules, sub_rows, strict=True)):
            path = escape(sub.path)
            group = f"submodule-{index}"
            submodules.extend(
                (
                    DetailItem("submodule", path, group),
                    DetailItem(
                        "commits",
                        f"{sub.status} · ↑{row['ahead_count'] or '0'} "
                        f"↓{row['behind_count'] or '0'}",
                        group,
                    ),
                    DetailItem("target +/-", row["target_diff"], group),
                    DetailItem("working +/-", row["wt"], group),
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
        DetailItem("state", state, "runtime"),
        DetailItem("network", network, "runtime"),
        *git,
        *submodules,
        DetailItem("agent", cell("agent"), "activity"),
        DetailItem("doing", _or_dash(doing), "activity"),
        DetailItem("resources", f"mem {cell('mem')} · cpu {cell('cpu')}", "resources"),
        DetailItem("mode / base", f"{escape(c.mode)} · {cell('base')}", "configuration"),
        DetailItem("github", _or_dash(github), "github"),
        DetailItem(
            "group",
            escape(c.credential_group) if c.credential_group else "[dim]inherits repo[/dim]",
            "identity",
        ),
        DetailItem("created", created, "identity"),
        DetailItem("mounts", _or_dash(", ".join(escape(m) for m in c.optional_mounts)), "mounts"),
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

    Panel title is:
    - For a container: the display name (repo prefix stripped; full name for orphans).
    - For a repo heading: the repo prefix.

    A container destroyed since the cursor landed on it simply has no panel
    until the selection is reconciled on the next frame.
    """
    if selected is None:
        return None
    reserve = (
        DETAILS_ACTIVITY_ROWS
        if any(s.activity is not None for g in groups for c in g.containers for s in c.agent_status)
        else 0
    )
    if selected.kind == "repo":
        group = next((g for g in groups if g.prefix == selected.key), None)
        return (
            None
            if group is None
            else DetailsView(group.prefix, tuple(repo_details(group)), reserve_rows=reserve)
        )
    for g in groups:
        for c in g.containers:
            if c.name == selected.key:
                block = activity_block(c, now)
                return DetailsView(
                    c.display_name,
                    tuple(container_details(c, now)),
                    block.lines,
                    reserve,
                    block.message,
                    block.history,
                )
    return None


def _one_row(markup: str) -> Text:
    text = Text.from_markup(markup, overflow="ellipsis")
    text.no_wrap = True  # from_markup has no no_wrap parameter
    return text


@dataclass(frozen=True)
class _DetailsBody:
    """Whole detail groups packed into the shortest column, then the agent activity.

    With ``max_rows`` the grid keeps at most ``DETAILS_MAX_ROWS`` rows (two at
    the least, before the reservation) and everything under it is activity,
    filled in order — head and tool lines, the wrapped last message, the
    history — and clipped to the rows left. ``fixed`` pads the grid to its
    share and the panel to exactly ``max_rows``.
    """

    view: DetailsView
    max_rows: int | None
    fixed: bool = False

    def _grid_cap(self) -> int | None:
        if self.max_rows is None:
            return None
        reserved = min(self.view.reserve_rows, max(0, self.max_rows - _MIN_GRID_ROWS))
        return min(DETAILS_MAX_ROWS, self.max_rows - reserved)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        grid_cap = self._grid_cap()
        lines = self._grid_lines(console, options, grid_cap)
        if self.fixed and grid_cap is not None:
            lines += [[_BLANK] for _ in range(grid_cap - len(lines))]
        room = None if self.max_rows is None else max(0, self.max_rows - len(lines))
        for text in self._activity_rows(console, options.max_width, room):
            lines += console.render_lines(text, options.update(height=None), pad=False)[:1]
        if self.fixed and self.max_rows is not None:
            lines += [[_BLANK] for _ in range(self.max_rows - len(lines))]
        for index, line in enumerate(lines):
            if index:
                yield Segment.line()
            yield from line

    def message_lines(self, console: Console, width: int) -> list[Text]:
        """The last message wrapped to ``width``; ``[]`` when there is none."""
        if self.view.message is None:
            return []
        return list(Text.from_markup(self.view.message).wrap(console, max(1, width)))

    def _activity_rows(self, console: Console, width: int, room: int | None) -> list[Text]:
        head = [_one_row(markup) for markup in self.view.activity]
        message = self.message_lines(console, width)
        history = [_one_row(markup) for markup in self.view.history]
        if room is None:
            return head + message + history
        head = head[:room]
        room -= len(head)
        if len(message) > room:
            message = message[:room]
            if message:
                message[-1].truncate(max(0, width - 1))
                message[-1].append("…", style="dim")
        room -= len(message)
        if len(history) > room:
            history = [*history[: room - 1], _one_row("[dim]…[/dim]")] if room > 0 else []
        return head + message + history

    def _grid_lines(
        self, console: Console, options: ConsoleOptions, cap: int | None
    ) -> list[list[Segment]]:
        pairs = max(1, min(DETAILS_MAX_PAIRS, options.max_width // DETAILS_PAIR_WIDTH))
        columns: list[list[DetailItem]] = [[] for _ in range(pairs)]
        groups: list[list[DetailItem]] = []
        for item in self.view.items:
            if item.group is not None and groups and groups[-1][0].group == item.group:
                groups[-1].append(item)
            else:
                groups.append([item])
        for group in groups:
            target = min(range(pairs), key=lambda index: len(columns[index]))
            columns[target].extend(group)

        pair_width = options.max_width // pairs
        label_width = min(12, max(1, pair_width // 3))
        # Padding adds one gap after every column except the last.
        value_width = max(1, (options.max_width - (2 * pairs - 1)) // pairs - label_width)
        row_count = max((len(column) for column in columns), default=0)
        if cap is not None:
            row_count = min(row_count, cap)
        grid = Table.grid(padding=(0, 1), expand=False)
        for _ in range(pairs):
            grid.add_column(width=label_width, style="bold", no_wrap=True, overflow="ellipsis")
            grid.add_column(width=value_width, overflow="ellipsis", no_wrap=True)
        for row in range(row_count):
            cells: list[str] = []
            for column in columns:
                if row == row_count - 1 and len(column) > row_count:
                    cells += ["", "…"]
                elif row < len(column):
                    item = column[row]
                    cells += [item.label, item.value]
                else:
                    cells += ["", ""]
            grid.add_row(*cells)
        return console.render_lines(grid, options.update(height=None), pad=False)


@dataclass(frozen=True)
class DetailsRows:
    """Content rows a details panel is guaranteed and would use if it could."""

    base: int
    want: int


def details_rows(view: DetailsView, console: Console, width: int) -> DetailsRows:
    """``view``'s guaranteed and wanted content rows at the panel's outer ``width``.

    The grid always takes ``DETAILS_MAX_ROWS`` rows in the dashboard (it is
    drawn ``fixed``); everything under it is the activity, the message wrapped
    at the panel's content width.
    """
    message = _DetailsBody(view, None).message_lines(console, width - PANEL_INSET_COLS)
    natural = DETAILS_MAX_ROWS + len(view.activity) + len(message) + len(view.history)
    return DetailsRows(view.base_rows, max(view.base_rows, natural))


def render_details(view: DetailsView, max_rows: int | None, *, fixed: bool = False) -> Panel:
    """The details panel; ``max_rows`` caps its content rows, activity included
    (None: uncapped).

    ``fixed`` also pads it to exactly ``max_rows`` content rows."""
    return Panel(
        _DetailsBody(view, max_rows, fixed),
        title=f"[bold]{escape(view.title)}[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
    )
