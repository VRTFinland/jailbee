"""The terminal dashboard's details panel: everything known about one row.

Pure — no I/O. Container values are composed from the same
``FieldSpec.cell`` functions the table uses (``lifecycle.ls_field_specs``),
so the panel and the table never format the same fact differently. A
container's panel reads top to bottom as a one-line summary, a git table (the
container's own tree, then one row per changed submodule) and a dim footer;
facts the table says twice (``agent``/``agent_compact``, the git columns and
``git_status``, ``network``/``ttl``/``loose_until``) collapse into one place
here. A repo heading keeps the label/value grid.
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
from rich.text import Text

from jailbee.agent_activity import describe
from jailbee.dashboard import format as dashboard_format
from jailbee.dashboard.hit import Hit, hit_style
from jailbee.dashboard.model import group_warnings
from jailbee.lifecycle import (
    ContainerInfo,
    format_duration_short,
    ls_field_specs,
    submodule_sub_rows,
    tracking_warning,
)
from jailbee.upgrade import ACTION_COMMANDS

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
class GitRow:
    """One line of the details panel's git table; every field is Rich markup.

    The container's own tree comes first, then one row per changed
    submodule. ``extra`` is shown only when the note column has room for it.
    """

    name: str
    ahead: str
    behind: str
    target: str
    working: str
    note: str = ""
    extra: str = ""


@dataclass(frozen=True)
class ContainerPanel:
    """A container's details, read top to bottom; every part is Rich markup.

    ``summary`` is one line of what the container is doing, ``exceptions``
    one undimmed line under it of what this container is allowed beyond its
    repo's defaults (loose network, attached mounts, its own egress entries;
    empty: the line is not drawn), ``git`` its trees (None without a git
    status), ``footer`` one dim line of the rest. ``base`` names the branch
    the git table's diff column compares with. ``warnings`` are yellow lines
    under the exceptions line (the fetched target is behind, say); empty:
    none are drawn.
    """

    summary: tuple[str, ...]
    base: str
    git: tuple[GitRow, ...] | None
    footer: tuple[str, ...]
    exceptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdvisoryLinks:
    """Which advisory links a front-end may offer (over SSH they follow the policy)."""

    run: bool
    dismiss: bool
    upgrade_all: bool


NO_LINKS = AdvisoryLinks(False, False, False)
UPGRADE_ALL_LABEL = "[run jb upgrade (all repos)]"
_LINK_GAP = "   "


@dataclass(frozen=True)
class AdvisoryLink:
    """One clickable ``[label]`` in an advisory block."""

    label: str
    hit: Hit


@dataclass(frozen=True)
class AdvisoryBlock:
    """One advisory under a repo's grid: a heading, its reason lines, its links.

    All text is plain (not markup); ``upgrade`` marks an owed upgrade action,
    as opposed to a deprecation notice.
    """

    heading: str
    reasons: tuple[str, ...]
    links: tuple[AdvisoryLink, ...] = ()
    upgrade: bool = False


@dataclass(frozen=True)
class DetailsView:
    """What the panel shows: a title and its rows, before any sizing.

    ``activity`` is the agent's head and tool lines, ``message`` its last
    message and ``history`` its recent steps, newest first — all Rich markup,
    already escaped. ``reserve_rows`` is how many rows under the grid every
    view is guaranteed while any container has activity, so the table keeps
    its height as the cursor moves; the panel grows past it only into rows
    the table does not need. ``panel`` is a container's summary / git table /
    footer, drawn in place of ``items``; a repo heading has none. ``warnings``
    are a repo heading's yellow lines under its grid, each as wide as the panel.
    ``advisories`` are its advisory blocks (``upgrade_all`` the link under the
    last upgrade block), drawn right under the grid and warnings.
    """

    title: str
    items: tuple[DetailItem, ...]
    activity: tuple[str, ...] = ()
    reserve_rows: int = 0
    message: str | None = None
    history: tuple[str, ...] = ()
    panel: ContainerPanel | None = None
    warnings: tuple[str, ...] = ()
    advisories: tuple[AdvisoryBlock, ...] = ()
    upgrade_all: AdvisoryLink | None = None

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


_TOOL_STYLE = {
    **dict.fromkeys(("Read", "Grep", "Glob", "LS", "NotebookRead"), "cyan"),
    **dict.fromkeys(("Edit", "MultiEdit", "Write", "NotebookEdit"), "yellow"),
    **dict.fromkeys(("Bash", "BashOutput", "KillShell"), "magenta"),
    **dict.fromkeys(("WebFetch", "WebSearch", "Task", "Agent"), "blue"),
}
"""Tool name -> colour by what the tool does; an unknown tool (an MCP one, say) is dim."""


def tool_markup(text: str, *, dim_args: bool) -> str:
    """A ``Name  argument`` tool line as escaped markup, the name coloured by kind."""
    name, _, args = text.partition("  ")
    style = _TOOL_STYLE.get(name, "dim")
    out = f"[{style}]{escape(name)}[/{style}]"
    if args:
        out += "  " + (f"[dim]{escape(args)}[/dim]" if dim_args else escape(args))
    return out


def activity_block(c: ContainerInfo, now: datetime) -> ActivityBlock:
    """The container's agent activity, or an empty block.

    The first agent summary that carries activity speaks (summaries are most
    urgent first). Every part is text an agent wrote, so it is escaped here.
    History tool entries have a coloured name and dim arguments, message
    entries are white; the newest message is bold white so it stands out. The newest tool
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
            lines.append(f"↳ {tool_markup(text.tool, dim_args=False)}")
        message = (
            None
            if text.message is None
            else f"[bold white]{escape(f'“{text.message}”')}[/bold white]"
        )
        skip = {"tool": text.tool is not None, "message": text.message is not None}
        earlier: list[str] = []
        for event in reversed(text.recent):
            if skip.get(event.kind):
                skip[event.kind] = False  # only the newest of each kind is shown above
                continue
            earlier.append(
                tool_markup(event.text, dim_args=True)
                if event.kind == "tool"
                else f"[white]{escape(f'“{event.text}”')}[/white]"
            )
        history = tuple(earlier)
        return ActivityBlock(tuple(lines), message, history)
    return ActivityBlock()


def _or_dash(value: str) -> str:
    return value if value.strip() else _DASH


_STATE_STYLE = {"Running": "green", "Frozen": "blue"}


def _git_value(value: str) -> str:
    """A submodule value styled the way the table styles its git cells."""
    if value in ("", "0"):
        return "[dim]0[/dim]"
    if value == "clean":
        return "[dim]clean[/dim]"
    if value == "?":
        return "[yellow]?[/yellow]"
    return escape(value)


def _present(markup: str) -> bool:
    """Whether a cell shows a value, not an empty string or the dash placeholder."""
    return Text.from_markup(markup).plain.strip() not in ("", "—")


def container_panel(c: ContainerInfo, now: datetime) -> ContainerPanel:
    """The container's details panel content: summary, git table, footer."""
    cells = {f.name: f.cell for f in ls_field_specs(now=now, all_repos=False)}

    def cell(name: str) -> str:
        return cells[name](c)

    style = _STATE_STYLE.get(c.state, "dim")
    state = f"[{style}]{dashboard_format.state_label(c.state)}[/{style}]"
    if c.job_phase is not None:
        state += f" · {cell('job')}"
    error = c.job_error.strip().splitlines()[0] if c.job_error and c.job_error.strip() else ""
    if error:
        state += f" · [red]{escape(error)}[/red]"
    summary = [state]
    if c.network == "strict":
        summary.append("[dim]strict[/dim]")
    elif c.network and c.network != "loose":
        summary.append(escape(c.network))
    if c.ip:
        summary.append(escape(c.ip))
    pr, issues = cell("pr"), cell("issues")
    github = " · ".join(
        part
        for part in (
            f"PR {pr}" if _present(pr) else "",
            f"issues {issues}" if _present(issues) else "",
        )
        if part
    )
    if github:
        summary.append(github)
    agent = cell("agent")
    if _present(agent):
        summary.append(agent)
    # Every busy process, unlike the cell, which stops at DOING_MAX_NAMES.
    doing = ", ".join(
        escape(p.comm) if p.count == 1 else f"{escape(p.comm)} x{p.count}" for p in c.activity
    )
    if doing:
        summary.append(f"[dim]{doing}[/dim]")

    git: tuple[GitRow, ...] | None = None
    if c.git_status is not None:
        root = GitRow(
            name=f"[bold]{escape(c.display_name)}[/bold]",
            ahead=cell("ahead_count"),
            behind=cell("behind_count"),
            target=cell("target_diff"),
            working=cell("wt"),
            note=f"[dim]merge[/dim] {cell('conflict')}",
            extra=f"[dim]host HEAD[/dim] ↑{cell('local_count')} {cell('local_diff')}",
        )
        subs = tuple(
            GitRow(
                name=escape(sub.path),
                ahead=_git_value(row["ahead_count"]),
                behind=_git_value(row["behind_count"]),
                target=_git_value(row["target_diff"]),
                working=_git_value(row["wt"]),
                note=f"[dim]{escape(sub.status)}[/dim]",
            )
            for sub, row in zip(c.git_status.submodules, submodule_sub_rows(c), strict=True)
        )
        git = (root, *subs)

    created = cell("created")
    if c.created_at is not None:
        created = f"{format_duration_short(now - c.created_at)} ago ({created})"
    group = escape(c.credential_group) if c.credential_group else "inherits repo"
    footer = [f"base {cell('base')}", escape(c.mode), f"group {group}", f"created {created}"]
    footer += [f"mem {cell('mem')}", f"cpu {cell('cpu')}"]
    return ContainerPanel(
        tuple(summary),
        escape(c.base_branch or "base"),
        git,
        tuple(footer),
        _exceptions(c, now, cell("ttl")),
        _warnings(c),
    )


def _warnings(c: ContainerInfo) -> tuple[str, ...]:
    """Yellow lines for what is off about ``c``'s git target, as markup."""
    warning = tracking_warning(c)
    return () if warning is None else (f"[yellow]⚠ {escape(warning)}[/yellow]",)


def _exceptions(c: ContainerInfo, now: datetime, ttl: str) -> tuple[str, ...]:
    """What ``c`` may do beyond its repo's defaults, each marked with the red ●.

    Loose network, then attached optional mounts with the time each has left,
    then the container's own egress entries, counted first so the count
    survives an ellipsis at the panel's edge.
    """
    parts: list[str] = []
    if c.network == "loose":
        loose = f"[red]●[/red] loose {ttl if c.loose_until is not None else '∞'}"
        if c.loose_until is not None:
            loose += f" →{c.loose_until.astimezone():%H:%M}"
        parts.append(loose)
    if c.optional_mounts:
        mounts = []
        for m in c.optional_mounts:
            until = c.mount_until.get(m)
            rest = "∞" if until is None else format_duration_short(until - now).replace(" ", "")
            mounts.append(f"{escape(m)} {rest}")
        parts.append(f"[red]●[/red] mounts {', '.join(mounts)}")
    if c.egress_extra:
        entries = ", ".join(escape(e) for e in c.egress_extra)
        parts.append(f"[red]●[/red] egress +{len(c.egress_extra)}: {entries}")
    return tuple(parts)


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


def repo_warnings(group: RepoGroup) -> tuple[str, ...]:
    """A repo heading's warnings (a retired config key, a fetched target ahead of
    the local one) as yellow markup lines."""
    return tuple(
        f"[yellow]⚠ {escape(label)}: {escape(text)}[/yellow]"
        for label, text in group_warnings(group, advisories=False)
    )


def repo_advisory_blocks(group: RepoGroup, links: AdvisoryLinks) -> tuple[AdvisoryBlock, ...]:
    """``group``'s advisories as blocks, with the links ``links`` allows."""
    blocks = []
    for a in group.advisories:
        offered: list[AdvisoryLink] = []
        if links.run and a.action is not None:
            offered.append(
                AdvisoryLink(
                    f"[run {ACTION_COMMANDS[a.action]}]",
                    Hit("advisory-run", (group.prefix, a.key)),
                )
            )
        if links.dismiss:
            offered.append(
                AdvisoryLink("[dismiss]", Hit("advisory-dismiss", (group.prefix, a.key)))
            )
        blocks.append(
            AdvisoryBlock(
                f"⚠ {a.lines[0]}" if a.lines else f"⚠ {a.summary}",
                tuple(a.lines[1:]),
                tuple(offered),
                a.action is not None,
            )
        )
    return tuple(blocks)


def details_for(
    groups: Sequence[RepoGroup],
    selected: Row | None,
    now: datetime,
    *,
    links: AdvisoryLinks = NO_LINKS,
) -> DetailsView | None:
    """The panel for ``selected``, or None when there is nothing to describe.

    Panel title is:
    - For a container: the display name (repo prefix stripped; full name for orphans).
    - For a repo heading: the repo prefix.

    A container destroyed since the cursor landed on it simply has no panel
    until the selection is reconciled on the next frame. ``links`` are the
    advisory links a repo heading's blocks may carry.
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
        if group is None:
            return None
        blocks = repo_advisory_blocks(group, links)
        owing = sum(any(a.action is not None for a in g.advisories) for g in groups)
        upgrade_all = (
            AdvisoryLink(UPGRADE_ALL_LABEL, Hit("upgrade-all", (group.prefix,)))
            if links.upgrade_all and owing >= 2 and any(b.upgrade for b in blocks)
            else None
        )
        return DetailsView(
            group.prefix,
            tuple(repo_details(group)),
            reserve_rows=reserve,
            warnings=repo_warnings(group),
            advisories=blocks,
            upgrade_all=upgrade_all,
        )
    for g in groups:
        for c in g.containers:
            if c.name == selected.key:
                block = activity_block(c, now)
                return DetailsView(
                    c.display_name,
                    (),
                    block.lines,
                    reserve,
                    block.message,
                    block.history,
                    container_panel(c, now),
                )
    return None


def _one_row(markup: str) -> Text:
    text = Text.from_markup(markup, overflow="ellipsis")
    text.no_wrap = True  # from_markup has no no_wrap parameter
    return text


_NAME_MAX = 32  # widest the git table's name column grows
_NAME_FLOOR = 6  # narrowest it is squeezed to while the note column still shows
_NOTE_MIN = 8  # narrowest note column worth showing ("merge ok")
_SUMMARY_GAP = "  "


@dataclass(frozen=True)
class PanelFit:
    """Which parts of a container panel fit the grid's rows."""

    blanks: bool  # the blank lines around the git table
    git: bool  # its header and the container's own row, or the "git —" line
    subs: int  # submodule rows shown
    more: int  # submodule rows counted by the "… +N more submodules" row
    footer: bool


def panel_fit(panel: ContainerPanel, cap: int | None) -> PanelFit:
    """What of ``panel`` fits ``cap`` rows (None: everything).

    Blank lines go first, then submodule rows, which one row then counts;
    the summary, the exceptions line and the footer stay. Only a panel too
    short for the table's head loses the table, and then the footer; the
    summary, the exceptions line and the warnings stay, cut from the bottom
    only by a cap below their own count.
    """
    # the summary, then the exceptions line and the warnings under it
    top = 1 + bool(panel.exceptions) + len(panel.warnings)
    subs = 0 if panel.git is None else len(panel.git) - 1
    head = 1 if panel.git is None else 2
    full = top + head + subs + 1
    if cap is None or full + 2 <= cap:
        return PanelFit(True, True, subs, 0, True)
    if full <= cap:
        return PanelFit(False, True, subs, 0, True)
    room = cap - top - head - 1  # rows left for submodules once the top, head and footer sit
    if room >= 1:
        return PanelFit(False, True, room - 1, subs - (room - 1), True)
    git = cap - top >= head
    return PanelFit(False, git, 0, 0, cap - top - (head if git else 0) >= 1)


def _cells(*markups: str) -> int:
    """The widest of the markup cells, in terminal columns."""
    return max((Text.from_markup(m).cell_len for m in markups), default=0)


def _git_table(panel: ContainerPanel, fit: PanelFit, width: int) -> Table:
    """The git table, its columns sized so the whole table fits ``width``.

    The four value columns keep their natural width; the name column gives way
    first (to ``_NAME_FLOOR``), and the note column is dropped before anything
    else shrinks. A row's ``extra`` joins its note only if every note still fits.
    """
    assert panel.git is not None  # the caller draws "git —" otherwise
    root, *subs = panel.git
    rows = (root, *subs[: fit.subs])
    hidden = len(subs) - fit.subs - fit.more
    hint = f"[dim]+{hidden} submodules[/dim]" if hidden > 0 else ""
    head = ("[dim]git[/dim]", "[dim]↑[/dim]", "[dim]↓[/dim]")
    target_head, working_head = f"[dim]vs {panel.base}[/dim]", "[dim]working[/dim]"
    values = (
        _cells(head[1], *(r.ahead for r in rows)),
        _cells(head[2], *(r.behind for r in rows)),
        _cells(target_head, *(r.target for r in rows)),
        _cells(working_head, *(r.working for r in rows)),
    )
    fixed = sum(values) + 2 * len(values)  # values and the gap before each
    name_natural = min(_cells(head[0], *(r.name for r in rows)), _NAME_MAX)
    notes = [r.note for r in rows]
    note_min = min(_cells(hint, *notes), _NOTE_MIN)
    note_width = 0
    name_width = max(1, min(name_natural, width - fixed))
    if width - fixed - _NAME_FLOOR - 2 >= note_min:
        name_width = max(_NAME_FLOOR, min(name_natural, width - fixed - 2 - note_min))
        room = width - fixed - name_width - 2
        with_extra = [" · ".join(p for p in (r.note, r.extra) if p) for r in rows]
        if _cells(*with_extra) <= room:
            notes = with_extra
        note_width = min(room, _cells(hint, *notes))
    grid = Table.grid(padding=(0, 2))
    grid.add_column(width=name_width, no_wrap=True, overflow="ellipsis")
    for w in values:
        grid.add_column(width=w, no_wrap=True, overflow="ellipsis")
    if note_width:
        grid.add_column(width=note_width, no_wrap=True, overflow="ellipsis")

    def tail(note: str) -> list[str]:
        return [note] if note_width else []

    grid.add_row(head[0], head[1], head[2], target_head, working_head, *tail(hint))
    for row, note in zip(rows, notes, strict=True):
        grid.add_row(row.name, row.ahead, row.behind, row.target, row.working, *tail(note))
    return grid


def _panel_lines(
    panel: ContainerPanel,
    console: Console,
    options: ConsoleOptions,
    cap: int | None,
    pad_to: int | None,
) -> list[list[Segment]]:
    """``panel`` as lines; with ``pad_to`` the footer lands on that row."""
    fit = panel_fit(panel, cap)
    render = options.update(height=None)
    lines: list[list[Segment]] = []

    def one(markup: str) -> None:
        lines.extend(console.render_lines(_one_row(markup), render, pad=False)[:1])

    top = [_SUMMARY_GAP.join(panel.summary)]
    if panel.exceptions:
        top.append(_SUMMARY_GAP.join(panel.exceptions))
    top.extend(panel.warnings)
    for markup in top if cap is None else top[:cap]:  # a cap under the top lines cuts the last
        one(markup)
    if fit.blanks:
        lines.append([_BLANK])
    if fit.git:
        if panel.git is None:
            one("[dim]git —[/dim]")
        else:
            table = _git_table(panel, fit, options.max_width)
            lines.extend(console.render_lines(table, render, pad=False))
            if fit.more:
                one(f"[dim]… +{fit.more} more submodules[/dim]")
    if fit.footer:
        if fit.blanks:
            lines.append([_BLANK])
        if pad_to is not None:
            lines += [[_BLANK] for _ in range(pad_to - len(lines) - 1)]
        one(f"[dim]{' · '.join(panel.footer)}[/dim]")
    return lines


_LINK_STYLE = Style(color="cyan")
_ADVISORY_STYLE = Style(color="yellow")


def _links_text(links: Sequence[AdvisoryLink], indent: str = "    ") -> Text:
    text = Text(indent, no_wrap=True, overflow="ellipsis")
    for index, link in enumerate(links):
        if index:
            text.append(_LINK_GAP)
        text.append(link.label, style=_LINK_STYLE + hit_style(link.hit.kind, *link.hit.args))
    return text


def _plain_row(markup: str, style: Style) -> Text:
    return Text(markup, style=style, no_wrap=True, overflow="ellipsis")


def advisory_rows(view: DetailsView, room: int | None) -> list[Text]:
    """The advisory blocks as one-row texts, fitted to ``room`` rows (None: all).

    Under a cap the reason lines go first (the rest of a block's reasons become
    ``- … N more``); a shown block always keeps its heading and links line.
    When even those do not fit, trailing blocks are dropped and a last row
    counts them. The upgrade-all link is the first thing to go.
    """
    blocks = view.advisories
    if not blocks:
        return []
    last_upgrade = max((i for i, b in enumerate(blocks) if b.upgrade), default=None)
    all_row = 1 if view.upgrade_all is not None and last_upgrade is not None else 0
    full = sum(1 + len(b.reasons) + bool(b.links) for b in blocks) + all_row
    shown, show_all = len(blocks), bool(all_row)
    budgets = [len(b.reasons) for b in blocks]
    if room is not None and full > room:
        shown, used = 0, 0
        for block in blocks:
            need = 1 + bool(block.links)
            if used + need + (shown + 1 < len(blocks)) > room:
                break
            used += need
            shown += 1
        if shown == 0:
            return [_more_notices(len(blocks))] if room >= 1 else []
        left = room - used - (shown < len(blocks))
        show_all = bool(all_row) and last_upgrade is not None and last_upgrade < shown and left >= 1
        left -= show_all
        budgets = []
        for block in blocks[:shown]:
            take = min(len(block.reasons), left)
            budgets.append(take)
            left -= take
    rows: list[Text] = []
    for index, block in enumerate(blocks[:shown]):
        rows.append(_plain_row(block.heading, _ADVISORY_STYLE))
        reasons, budget = block.reasons, budgets[index]
        if budget >= len(reasons):
            rows += [_plain_row(r, _ADVISORY_STYLE) for r in reasons]
        elif budget >= 1:
            rows += [_plain_row(r, _ADVISORY_STYLE) for r in reasons[: budget - 1]]
            rows.append(_plain_row(f"    - … {len(reasons) - budget + 1} more", _ADVISORY_STYLE))
        if block.links:
            rows.append(_links_text(block.links))
        if show_all and index == last_upgrade and view.upgrade_all is not None:
            rows.append(_links_text((view.upgrade_all,)))
    if shown < len(blocks):
        rows.append(_more_notices(len(blocks) - shown))
    return rows


def _more_notices(count: int) -> Text:
    return _plain_row(f"… {count} more notices (jb doctor)", Style(dim=True))


@dataclass(frozen=True)
class _DetailsBody:
    """The label/value grid, or a container's panel, then the agent activity.

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
        if self.fixed and grid_cap is not None and not self.view.advisories:
            lines += [[_BLANK] for _ in range(grid_cap - len(lines))]
        room = None if self.max_rows is None else max(0, self.max_rows - len(lines))
        advisory = advisory_rows(self.view, room)
        for text in advisory:
            lines += console.render_lines(text, options.update(height=None), pad=False)[:1]
        if room is not None:
            room -= len(advisory)
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
                message[-1].append("…", style="bold white")
        room -= len(message)
        if len(history) > room:
            history = [*history[: room - 1], _one_row("[dim]…[/dim]")] if room > 0 else []
        return head + message + history

    def _grid_lines(
        self, console: Console, options: ConsoleOptions, cap: int | None
    ) -> list[list[Segment]]:
        if self.view.panel is not None:
            pad_to = cap if self.fixed else None
            return _panel_lines(self.view.panel, console, options, cap, pad_to)
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
        lines = console.render_lines(grid, options.update(height=None), pad=False)
        room = None if cap is None else max(0, cap - len(lines))
        for markup in self.view.warnings if room is None else self.view.warnings[:room]:
            lines += console.render_lines(_one_row(markup), options.update(height=None), pad=False)[
                :1
            ]
        return lines


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
    body = _DetailsBody(view, None)
    message = body.message_lines(console, width - PANEL_INSET_COLS)
    grid = DETAILS_MAX_ROWS
    if view.advisories:
        # The grid is not padded: the blocks sit right under it and its warnings.
        inner = console.options.update(width=max(1, width - PANEL_INSET_COLS), height=None)
        grid = len(body._grid_lines(console, inner, None)) + len(advisory_rows(view, None))
    natural = grid + len(view.activity) + len(message) + len(view.history)
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
