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
from jailbee.dashboard.advisories import owing_repos
from jailbee.dashboard.hit import Hit, hit_style
from jailbee.dashboard.model import group_warnings
from jailbee.dashboard.system_bar import min_usage_bar_cells, usage_bar
from jailbee.lifecycle import (
    ContainerInfo,
    _format_bytes,
    _parse_memory_limit,
    fork_source,
    ls_field_specs,
    submodule_sub_rows,
    tracking_warning,
)
from jailbee.outbox.markdown_view import markdown_text_lines
from jailbee.upgrade import ACTION_COMMANDS

if TYPE_CHECKING:
    from jailbee.accounts.models import ActivityEvent
    from jailbee.dashboard.model import RepoGroup, Row

DETAILS_MAX_ROWS = 8  # content rows inside the border, for the label/value grid
DETAILS_ACTIVITY_ROWS = 3  # rows every view reserves under it while any container has activity
_BLANK = Segment("")  # a blank line the panel's line splitter keeps even at the very end
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
    none are drawn. ``processes`` are the top CPU and memory lines drawn
    directly under the summary (0-2; empty: none are drawn). ``memory`` is a
    running container's (used, limit) bytes: drawn as a bar at the right end of
    the summary row when the row has room for it, else not at all.
    ``orchestration`` are an orchestrator's or child's own lines, one row each
    right under the exceptions line (empty: none are drawn).
    """

    summary: tuple[str, ...]
    base: str
    git: tuple[GitRow, ...] | None
    footer: tuple[str, ...]
    exceptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    processes: tuple[str, ...] = ()
    memory: tuple[int, int] | None = None
    orchestration: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdvisoryLinks:
    """Which advisory links a front-end may offer (over SSH they follow the policy)."""

    run_base_build: bool
    run_apply: bool
    dismiss: bool
    upgrade_all: bool

    def run(self, action: str | None) -> bool:
        """Whether the run link of an advisory owing ``action`` may be offered."""
        return (action == "base_build" and self.run_base_build) or (
            action == "apply" and self.run_apply
        )


NO_LINKS = AdvisoryLinks(False, False, False, False)
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

    ``activity`` is the agent's head and tool lines and ``history`` its recent
    steps, newest first — Rich markup, already escaped. ``message`` is its last
    message, rendered as Markdown. ``reserve_rows`` is how many rows under the grid every
    view is guaranteed while any container has activity, so the table keeps
    its height as the cursor moves; the panel grows past it only into rows
    the table does not need. ``panel`` is a container's summary / git table /
    footer, drawn in place of ``items``; a repo heading has none. ``warnings``
    are a repo heading's yellow lines under its grid, each as wide as the panel.
    ``advisories`` are its advisory blocks (``upgrade_all`` the link under the
    last upgrade block), drawn right under the grid and warnings. ``scroll`` is
    how many content rows are scrolled off the top.
    """

    title: str
    items: tuple[DetailItem, ...]
    activity: tuple[str, ...] = ()
    reserve_rows: int = 0
    message: ActivityMessage | None = None
    history: tuple[str, ...] = ()
    panel: ContainerPanel | None = None
    warnings: tuple[str, ...] = ()
    advisories: tuple[AdvisoryBlock, ...] = ()
    upgrade_all: AdvisoryLink | None = None
    scroll: int = 0  # the first content row shown when the content is taller than the panel

    @property
    def base_rows(self) -> int:
        """Content rows the panel is guaranteed: the grid's cap plus the reservation."""
        return DETAILS_MAX_ROWS + self.reserve_rows


@dataclass(frozen=True)
class ActivityMessage:
    """An agent's last message: Markdown it wrote, and the time it wrote it.

    ``text`` is untrusted and raw — it is rendered as Markdown, never as Rich
    markup. ``clock`` is escaped markup (the dim ``HH:MM `` or ``""``) that
    leads the first rendered line; the rest hang under the message.
    """

    text: str
    clock: str = ""


@dataclass(frozen=True)
class ActivityBlock:
    """A container's agent activity as escaped Rich markup; empty when it has none."""

    lines: tuple[str, ...] = ()
    message: ActivityMessage | None = None
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


def _clock(event: ActivityEvent | None) -> str:
    """The local ``HH:MM`` an event happened, dim, with its trailing space; ``""`` if unknown."""
    if event is None or event.at is None:
        return ""
    return f"[dim]{event.at.astimezone():%H:%M}[/dim] "


def _event_markup(event: ActivityEvent) -> str:
    body = (
        tool_markup(event.text, dim_args=True)
        if event.kind == "tool"
        else f"[white]{escape(f'“{event.text}”')}[/white]"
    )
    return _clock(event) + body


def activity_block(c: ContainerInfo, now: datetime) -> ActivityBlock:
    """The container's agent activity, or an empty block.

    The first agent summary that carries activity speaks (summaries are most
    urgent first). Every part is text an agent wrote, so it is escaped here.
    History tool entries have a coloured name and dim arguments, message
    entries are white; the newest message is kept whole and is rendered as
    Markdown, the way the agent laid it out. The newest tool
    event is left out of the history when the tool line shows it, and the
    newest message event when ``message`` shows it: ``last_tool`` /
    ``last_message`` are by construction those same newest events, so the
    history starts with what came before them.
    """
    for summary in c.agent_status:
        text = describe(summary, now)
        if text is None:
            continue
        # The newest tool call and message are what the tool line and `message`
        # show; each leads with the time its transcript record carries.
        newest: dict[str, ActivityEvent] = {}
        skip = {"tool": text.tool is not None, "message": text.message is not None}
        earlier: list[str] = []
        for event in reversed(text.recent):
            if skip.get(event.kind):
                skip[event.kind] = False  # only the newest of each kind is shown above
                newest[event.kind] = event
                continue
            earlier.append(_event_markup(event))
        lines = [escape(text.head)]
        if text.tool is not None:
            lines.append(f"↳ {_clock(newest.get('tool'))}{tool_markup(text.tool, dim_args=False)}")
        message = (
            None
            if text.message is None
            else ActivityMessage(text.message, _clock(newest.get("message")))
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

    # What no other row says: the base is the git table's "vs" header, memory and
    # CPU are the summary bar and the process lines, the table has the age, the
    # mode and the group. Usually that leaves nothing.
    footer: list[str] = []
    if c.memory_override and c.memory_limit:
        footer.append(f"memory {escape(c.memory_limit)}* (override)")
    if (source := fork_source(c)) is not None:
        footer.append(f"⑂ forked from {escape(source)}")
    return ContainerPanel(
        tuple(summary),
        escape(c.base_branch or "base"),
        git,
        tuple(footer),
        _exceptions(c, now, cell("ttl")),
        _warnings(c),
        _process_lines(c),
        _memory_use(c),
        _orchestration_lines(c),
    )


def _orchestration_lines(c: ContainerInfo) -> tuple[str, ...]:
    """An orchestrator's or child's card lines, as escaped markup; empty for others."""

    def names(values: tuple[str, ...]) -> str:
        return ", ".join(escape(v) for v in values) or "—"

    def rights(label: str, mounts: tuple[str, ...], loose: bool, egress: tuple[str, ...]) -> str:
        return (
            f"[dim]{label}[/dim] mounts {names(mounts)} · loose {'yes' if loose else 'no'}"
            f" · egress {names(egress)}"
        )

    gs = c.git_status
    unread = (gs.unread_inbox or 0) if gs else 0
    if c.grant is not None:
        g = c.grant
        head = f"orchestrator · children {c.running_children}/{g.limit}"
        if c.queued_children:
            head += f" · {c.queued_children} queued"
        if unread:
            head += f" · inbox {unread} unread"
        return (
            head,
            f"[dim]may:[/dim] {', '.join(escape(o) for o in g.operations)}",
            rights("may grant:", g.mounts, g.loose, g.egress),
        )
    if c.child is not None:
        ch = c.child
        short = ch.orchestrator
        if c.repo and short.startswith(f"{c.repo}-"):
            short = short[len(c.repo) + 1 :]
        head = f"child of {escape(short)}"
        if c.orchestrator_running is False:
            head += " [red](not running)[/red]"
        head += f" · {escape(ch.state)}"
        if unread:
            head += f" · inbox {unread} unread"
        outbox = (gs.pending_orch_outbox or 0) if gs else 0
        if outbox:
            head += f" · outbox {outbox} pending"
        return (head, rights("rights:", ch.mounts, ch.loose, ch.egress))
    return ()


def _memory_use(c: ContainerInfo) -> tuple[int, int] | None:
    """(used, limit) bytes of a running container with a fixed memory limit."""
    limit = _parse_memory_limit(c.memory_limit)
    if c.state != "Running" or c.memory_usage is None or limit is None:
        return None
    return c.memory_usage, limit


def _process_lines(c: ContainerInfo) -> tuple[str, ...]:
    """The ``cpu`` and ``mem`` top-process lines of a running container, as markup."""
    if c.state != "Running":
        return ()

    def name(comm: str, count: int) -> str:
        return escape(comm) if count == 1 else f"{escape(comm)} x{count}"

    lines: list[str] = []
    if c.top_cpu:
        items = " · ".join(f"{name(p.comm, p.count)} {round(p.percent)}%" for p in c.top_cpu)
        lines.append(f"[dim]cpu[/dim]  {items}")
    if c.top_mem:
        items = " · ".join(
            f"{name(p.comm, p.count)} {_format_bytes(p.rss_bytes)}" for p in c.top_mem
        )
        lines.append(f"[dim]mem[/dim]  {items}")
    return tuple(lines)


def _warnings(c: ContainerInfo) -> tuple[str, ...]:
    """Yellow lines for what is off about ``c``'s git target, as markup."""
    warning = tracking_warning(c)
    return () if warning is None else (f"[yellow]⚠ {escape(warning)}[/yellow]",)


def _exceptions(c: ContainerInfo, now: datetime, ttl: str) -> tuple[str, ...]:
    """What ``c`` may do beyond its repo's defaults, each marked with the red ●.

    Loose network, then the repo's optional mounts (a dim ○ instead of the ●
    while none is attached) with the time each attached one has left,
    then the container's own egress entries, counted first so the count
    survives an ellipsis at the panel's edge.
    """
    parts: list[str] = []
    if c.network == "loose":
        loose = f"[red]●[/red] loose {ttl if c.loose_until is not None else '∞'}"
        if c.loose_until is not None:
            loose += f" →{c.loose_until.astimezone():%H:%M}"
        parts.append(loose)
    mounts = dashboard_format.mount_names_markup(c, now)
    if mounts:
        # Listed whenever the repo offers any, so the MOUNT column's letters can
        # be read; the red ● is kept for a container that has one attached.
        mark = "[red]●[/red]" if c.optional_mounts else "[dim]○[/dim]"
        parts.append(f"{mark} mounts {mounts}")
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
        if a.action is not None and links.run(a.action):
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
    all_groups: Sequence[RepoGroup] | None = None,
) -> DetailsView | None:
    """The panel for ``selected``, or None when there is nothing to describe.

    Panel title is:
    - For a container: the display name (repo prefix stripped; full name for orphans).
    - For a repo heading: the repo prefix.

    A container destroyed since the cursor landed on it simply has no panel
    until the selection is reconciled on the next frame. ``links`` are the
    advisory links a repo heading's blocks may carry; ``all_groups`` (default
    ``groups``) is every repo, hidden ones included, which `jb upgrade` sweeps.
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
        owing = owing_repos(groups if all_groups is None else all_groups)
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
    """A row of markup; one too long for the panel continues on the next line."""
    return Text.from_markup(markup, overflow="fold")


_NAME_MAX = 32  # widest the git table's name column grows
_NAME_FLOOR = 6  # narrowest it is squeezed to while the note column still shows
_NOTE_MIN = 8  # narrowest note column worth showing ("merge ok")
_SUMMARY_GAP = "  "
_MEM_BAR_CELLS = 23  # the memory bar's width inside its brackets, label included


def _cells(*markups: str) -> int:
    """The widest of the markup cells, in terminal columns."""
    return max((Text.from_markup(m).cell_len for m in markups), default=0)


def _git_table(panel: ContainerPanel, width: int) -> Table:
    """The git table, its columns sized so the whole table fits ``width``.

    The four value columns keep their natural width; the name column gives way
    first (to ``_NAME_FLOOR``), and the note column is dropped before anything
    else shrinks. A row's ``extra`` joins its note only if every note still fits.
    """
    assert panel.git is not None  # the caller draws "git —" otherwise
    root, *subs = panel.git
    rows = (root, *subs)
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
    note_min = min(_cells(*notes), _NOTE_MIN)
    note_width = 0
    name_width = max(1, min(name_natural, width - fixed))
    if width - fixed - _NAME_FLOOR - 2 >= note_min:
        name_width = max(_NAME_FLOOR, min(name_natural, width - fixed - 2 - note_min))
        room = width - fixed - name_width - 2
        with_extra = [" · ".join(p for p in (r.note, r.extra) if p) for r in rows]
        if _cells(*with_extra) <= room:
            notes = with_extra
        note_width = min(room, _cells(*notes))
    grid = Table.grid(padding=(0, 2))
    grid.add_column(width=name_width, overflow="fold")
    for w in values:
        grid.add_column(width=w, no_wrap=True, overflow="ellipsis")
    if note_width:
        grid.add_column(width=note_width, overflow="fold")

    def tail(note: str) -> list[str]:
        return [note] if note_width else []

    grid.add_row(head[0], head[1], head[2], target_head, working_head, *tail(""))
    for row, note in zip(rows, notes, strict=True):
        grid.add_row(row.name, row.ahead, row.behind, row.target, row.working, *tail(note))
    return grid


def _summary_row(panel: ContainerPanel, width: int) -> str | Text:
    """The summary, with the memory bar at the right end when it fits whole.

    The bar takes the room the summary leaves, up to ``_MEM_BAR_CELLS``; with
    less than its numbers need it is left out and the summary is untouched.
    """
    summary = _SUMMARY_GAP.join(panel.summary)
    if panel.memory is None:
        return summary
    used, limit = panel.memory
    left = _one_row(summary)
    avail = width - left.cell_len - len(_SUMMARY_GAP)
    if avail < min_usage_bar_cells("mem", used, limit):
        return summary
    inner = min(_MEM_BAR_CELLS, avail - 2)
    bar = usage_bar("mem", used, limit, inner)
    row = Text(no_wrap=True, overflow="ellipsis")
    row.append_text(left)
    row.append(" " * (width - left.cell_len - bar.cell_len))
    row.append_text(bar)
    return row


def _panel_lines(
    panel: ContainerPanel,
    console: Console,
    options: ConsoleOptions,
    pad_to: int | None,
) -> list[list[Segment]]:
    """``panel`` as lines; with ``pad_to`` the footer lands on that row."""
    render = options.update(height=None)
    lines: list[list[Segment]] = []

    def one(markup: str | Text) -> None:
        row = _one_row(markup) if isinstance(markup, str) else markup
        lines.extend(console.render_lines(row, render, pad=False))

    one(_summary_row(panel, options.max_width))
    for markup in panel.processes:
        one(markup)
    if panel.exceptions:
        one(_SUMMARY_GAP.join(panel.exceptions))
    for markup in panel.orchestration:
        one(markup)
    for markup in panel.warnings:
        one(markup)
    lines.append([_BLANK])
    if panel.git is None:
        one("[dim]git —[/dim]")
    else:
        lines.extend(console.render_lines(_git_table(panel, options.max_width), render, pad=False))
    if pad_to is not None:
        lines += [[_BLANK] for _ in range(pad_to - len(lines) - bool(panel.footer))]
    if panel.footer:
        if pad_to is None:
            lines.append([_BLANK])
        one(f"[dim]{' · '.join(panel.footer)}[/dim]")
    return lines


_LINK_STYLE = Style(color="cyan")
_ADVISORY_STYLE = Style(color="yellow")


def _links_text(links: Sequence[AdvisoryLink], indent: str = "    ") -> Text:
    text = Text(indent, overflow="fold")
    for index, link in enumerate(links):
        if index:
            text.append(_LINK_GAP)
        text.append(link.label, style=_LINK_STYLE + hit_style(link.hit.kind, *link.hit.args))
    return text


def _plain_row(markup: str, style: Style) -> Text:
    return Text(markup, style=style, overflow="fold")


def advisory_rows(view: DetailsView) -> list[Text]:
    """The advisory blocks as texts: heading, reasons, links, and under the last
    upgrade block the upgrade-all link."""
    blocks = view.advisories
    last_upgrade = max((i for i, b in enumerate(blocks) if b.upgrade), default=None)
    rows: list[Text] = []
    for index, block in enumerate(blocks):
        rows.append(_plain_row(block.heading, _ADVISORY_STYLE))
        rows += [_plain_row(r, _ADVISORY_STYLE) for r in block.reasons]
        if block.links:
            rows.append(_links_text(block.links))
        if view.upgrade_all is not None and index == last_upgrade:
            rows.append(_links_text((view.upgrade_all,)))
    return rows


@dataclass(frozen=True)
class _DetailsBody:
    """The label/value grid, or a container's panel, then the agent activity.

    Nothing is cut: a row too long for the panel wraps onto the next line, and
    with ``max_rows`` set, content taller than that is shown as a window of
    ``max_rows`` rows starting at the view's ``scroll``, with a scrollbar in
    the last column. ``fixed`` pads the grid to ``DETAILS_MAX_ROWS`` and the
    panel to exactly ``max_rows``.
    """

    view: DetailsView
    max_rows: int | None
    fixed: bool = False

    def natural_lines(self, console: Console, options: ConsoleOptions) -> list[list[Segment]]:
        """Every row of the body at ``options.max_width``, wrapped and uncut."""
        render = options.update(height=None)
        grid_rows = DETAILS_MAX_ROWS if self.fixed else None
        lines = self._grid_lines(console, options, grid_rows)
        if grid_rows is not None and not self.view.advisories:
            lines += [[_BLANK] for _ in range(grid_rows - len(lines))]
        for text in advisory_rows(self.view):
            lines += console.render_lines(text, render, pad=False)
        for text in self._activity_rows(console, options.max_width):
            lines += console.render_lines(text, render, pad=False)
        return lines

    def scroll_lines(self, console: Console, options: ConsoleOptions) -> tuple[int, int]:
        """``(rows the body has, the furthest it can scroll)`` at ``options.max_width``.

        The scrollbar takes a column, which can wrap one more row: the second
        number counts with it.
        """
        total = len(self.natural_lines(console, options))
        rows = self.max_rows
        if rows is None or total <= rows or options.max_width < 2:
            return total, 0
        narrow = len(self.natural_lines(console, options.update(width=options.max_width - 1)))
        return total, max(0, narrow - rows)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        lines = self.natural_lines(console, options)
        rows = self.max_rows
        if rows is not None and len(lines) > rows and options.max_width >= 2:
            width = options.max_width - 1
            narrow_lines = self.natural_lines(console, options.update(width=width))
            # Wrapping at one column narrower can land on <= rows lines even
            # though the wider check above found more: no scrollbar is needed
            # then, and drawing one would divide by zero.
            if len(narrow_lines) > rows:
                lines = _with_scrollbar(narrow_lines, rows, width, self.view.scroll)
            else:
                lines = narrow_lines
                if self.fixed:
                    lines += [[_BLANK] for _ in range(rows - len(lines))]
        elif rows is not None and self.fixed:
            lines += [[_BLANK] for _ in range(rows - len(lines))]
        for index, line in enumerate(lines):
            if index:
                yield Segment.line()
            yield from line

    def message_lines(self, console: Console, width: int) -> list[Text]:
        """The last message rendered as Markdown ``width`` wide; ``[]`` when there is none.

        The clock leads the first line and the rest hang under the message.
        """
        message = self.view.message
        if message is None:
            return []
        clock = Text.from_markup(message.clock)
        indent = clock.cell_len
        body = markdown_text_lines(message.text, max(1, width - indent))
        return [
            (clock.copy() if index == 0 else Text(" " * indent)) + line
            for index, line in enumerate(body)
        ] or [clock]

    def _activity_rows(self, console: Console, width: int) -> list[Text]:
        head = [_one_row(markup) for markup in self.view.activity]
        history = [_one_row(markup) for markup in self.view.history]
        return head + self.message_lines(console, width) + history

    def _grid_lines(
        self, console: Console, options: ConsoleOptions, pad_to: int | None
    ) -> list[list[Segment]]:
        if self.view.panel is not None:
            return _panel_lines(self.view.panel, console, options, pad_to)
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
        grid = Table.grid(padding=(0, 1), expand=False)
        for _ in range(pairs):
            grid.add_column(width=label_width, style="bold", no_wrap=True, overflow="ellipsis")
            grid.add_column(width=value_width, overflow="fold")
        for row in range(row_count):
            cells: list[str] = []
            for column in columns:
                if row < len(column):
                    item = column[row]
                    cells += [item.label, item.value]
                else:
                    cells += ["", ""]
            grid.add_row(*cells)
        render = options.update(height=None)
        lines = console.render_lines(grid, render, pad=False)
        for markup in self.view.warnings:
            lines += console.render_lines(_one_row(markup), render, pad=False)
        return lines


# One look for every scrollbar of the dashboard: a thin line, the thumb heavier.
SCROLL_THUMB_GLYPH, SCROLL_TRACK_GLYPH = "┃", "│"
SCROLL_THUMB_STYLE, SCROLL_TRACK_STYLE = Style(bold=True), Style(dim=True)


def _with_scrollbar(
    lines: list[list[Segment]], rows: int, width: int, scroll: int
) -> list[list[Segment]]:
    """The ``rows``-row window of ``lines`` from row ``scroll``, a scrollbar to its right.

    Each bar cell is a click target that scrolls to the matching position.
    """
    total = len(lines)
    furthest = total - rows
    top = max(0, min(scroll, furthest))
    thumb = max(1, rows * rows // total)
    start = round(top * (rows - thumb) / furthest)
    out = []
    for row in range(rows):
        target = round(row * furthest / (rows - 1)) if rows > 1 else 0
        on = start <= row < start + thumb
        bar = Segment(
            SCROLL_THUMB_GLYPH if on else SCROLL_TRACK_GLYPH,
            (SCROLL_THUMB_STYLE if on else SCROLL_TRACK_STYLE)
            + hit_style("details-scroll", target),
        )
        out.append([*Segment.adjust_line_length(lines[top + row], width), bar])
    return out


@dataclass(frozen=True)
class DetailsRows:
    """Content rows a details panel is guaranteed and would use if it could.

    ``total`` is the rows its whole content takes at the measured width, and
    ``total_barred`` the same beside a scrollbar, which can wrap one more row.
    """

    base: int
    want: int
    total: int = 0
    total_barred: int = 0

    def furthest(self, rows: int) -> int:
        """How far a panel of ``rows`` content rows scrolls (0: its content fits)."""
        return 0 if self.total <= rows else max(0, self.total_barred - rows)


def details_rows(view: DetailsView, console: Console, width: int) -> DetailsRows:
    """``view``'s guaranteed and wanted content rows at the panel's outer ``width``.

    The grid always takes ``DETAILS_MAX_ROWS`` rows in the dashboard (it is
    drawn ``fixed``) unless advisory blocks sit right under it; everything under
    it is the activity, wrapped at the panel's content width like every row.
    """
    inner = console.options.update(width=max(1, width - PANEL_INSET_COLS), height=None)
    body = _DetailsBody(view, None, fixed=True)
    total = len(body.natural_lines(console, inner))
    barred = len(body.natural_lines(console, inner.update(width=max(1, inner.max_width - 1))))
    return DetailsRows(view.base_rows, max(view.base_rows, total), total, barred)


def render_details(view: DetailsView, max_rows: int | None, *, fixed: bool = False) -> Panel:
    """The details panel; ``max_rows`` is its content rows, activity included
    (None: as many as the content takes), scrolled from ``view.scroll`` when
    the content is taller.

    ``fixed`` also pads it to exactly ``max_rows`` content rows."""
    return Panel(
        _DetailsBody(view, max_rows, fixed),
        title=f"[bold]{escape(view.title)}[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
    )
