"""One terminal dashboard frame as a Rich renderable."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime
from typing import Any

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text

from jailbee.dashboard import accounts as da
from jailbee.dashboard import hit as dhit
from jailbee.dashboard.columns import FieldSpecCI, _frame_columns, window_rows
from jailbee.dashboard.details import (
    DETAILS_MAX_ROWS,
    DETAILS_PAIR_WIDTH,
    DetailsView,
    details_for,
    render_details,
)
from jailbee.dashboard.egress import (
    EgressState,
    removable_entry,
    render_egress,
)
from jailbee.dashboard.menus import MenuGroup
from jailbee.dashboard.model import RepoGroup, Row
from jailbee.dashboard.overlays import (
    MIN_LIST_ROWS,
    PICKER_HINT,
    PROMPT_HINT,
    SUGGEST_HINT,
    Picker,
    TextPrompt,
    render_picker,
    render_prompt,
    window_lines,
)
from jailbee.dashboard.settings import (
    CURSOR_STYLE,
    SettingsState,
    render_settings,
)
from jailbee.dashboard.tui.keys import _GATE_NOTE, KEY_BINDINGS
from jailbee.dashboard.tui.menu_state import MenuState, RepoMenuState, _menu_entries, menu_hotkeys
from jailbee.dashboard.tui.overlay import CommandState, Overlay
from jailbee.dashboard.viewport import column_viewport
from jailbee.lifecycle import (
    ContainerInfo,
)

_INLINE_NOTICE_MAX = 80  # longer notices wrap below the table instead of the border


def _render_menu(menu: MenuState | RepoMenuState, max_rows: int | None = None) -> RenderableType:
    """The action menu as a bordered panel: one row per action, cursor on the
    highlighted one, windowed to ``max_rows`` around the cursor."""
    entries = _menu_entries(menu)
    lines = []
    for i, (item, key) in enumerate(zip(entries, menu_hotkeys(entries), strict=True)):
        label = item.label if isinstance(item, MenuGroup) else item[0]
        tag = f"[bold]\\[{key}][/]" if key else "   "
        line = (
            f"[bold cyan]▸[/] {tag} [{CURSOR_STYLE}]{label}[/]"
            if i == menu.index
            else f"  {tag} {label}"
        )
        lines.append(dhit.hit_markup(line, "menu", i))
    if isinstance(menu, RepoMenuState):
        title = (
            f"{menu.repo} → {menu.active_group.removesuffix(' →')}"
            if menu.active_group
            else f"{menu.repo} →"
        )
    elif menu.active_group:
        title = f"{menu.container} → {menu.active_group.removesuffix(' →')}"
    else:
        title = f"{menu.container} →"
    return Panel(
        "\n".join(window_lines(lines, menu.index, max_rows)),
        title=f"[bold]{title}[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
        expand=False,
    )


def _render_help() -> RenderableType:
    """The keybinding help as a bordered panel, grouped as the table declares.

    Rows come from :data:`KEY_BINDINGS`, so a new key documents itself. The
    closing note explains why an action key can decline to fire — without it
    a correctly-gated key looks broken.
    """
    width = max((len(b.hint) for b in KEY_BINDINGS if b.hint), default=0)
    lines: list[str] = []
    for group in dict.fromkeys(b.group for b in KEY_BINDINGS):
        if lines:
            lines.append("")
        lines.append(f"[bold cyan]{group}[/]")
        lines += [
            f"  [bold]{b.hint:<{width}}[/]  {b.label}"
            for b in KEY_BINDINGS
            if b.group == group and b.hint
        ]
    lines += [
        "",
        "ST: ▶ running, ■ stopped, Ⅱ frozen; NET: ● strict, ○ loose.",
        "AGE: container age; AI: agent status (◆ waiting, ● busy, ◐ shell, ○ idle).",
        "BASE ↗: remote-tracking base; MODE: cln clone, mnt mount.",
        "WT / DIFF / L DIFF: ✓ clean; DIFF vs host target, L DIFF vs host HEAD.",
        "DOING ×N: process count; JOB auto:stage: autostart stage.",  # noqa: RUF001 - intentional multiplication sign
        "Menus: the key in brackets picks that entry, like Enter on it.",
        "Egress panel: a adds, r removes a scoped override; Esc backs to its menu.",
        "Accounts panel: Enter acts on a login or group, n creates a group.",
        "Repo menu: Apply config…, Diagnostics →, Prune stale containers…",
        "Container menu: Snapshots…, Mount…/Unmount…, autostart status/cancel.",
        "",
        f"[dim]{_GATE_NOTE}[/dim]",
    ]
    return Panel(
        "\n".join(lines),
        title="[bold]keys[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
        width=72,
    )


_MENU_PICK_HINT = "[bold]\\[key][/bold] pick"


def _hint_line(overlay: Overlay | None) -> str:
    """Contextual controls shown only while an overlay is open."""
    if isinstance(overlay, MenuState):
        if overlay.active_group is not None:
            return (
                f"[bold]↑/↓[/bold] move  ·  {_MENU_PICK_HINT}  ·  [bold]Enter[/bold] run  ·  "
                "[bold]Esc[/bold] back  ·  [bold]q[/bold] close"
            )
        return (
            f"[bold]↑/↓[/bold] move  ·  {_MENU_PICK_HINT}  ·  "
            "[bold]Enter[/bold] open/run  ·  [bold]Esc[/bold] cancel"
        )
    if isinstance(overlay, RepoMenuState):
        return (
            f"[bold]↑/↓[/bold] move  ·  {_MENU_PICK_HINT}  ·  "
            "[bold]Enter[/bold] run  ·  [bold]Esc[/bold] cancel"
        )
    if isinstance(overlay, EgressState):
        return (
            "[bold]↑/↓[/bold] move  ·  [bold]a[/bold] add  ·  "
            "[bold]r[/bold] remove  ·  [bold]Esc[/bold] back"
        )
    if isinstance(overlay, SettingsState):
        return (
            "[bold]↑/↓[/bold] move  ·  [bold]Space[/bold] toggle  ·  "
            "[bold]Tab[/bold] switch  ·  [bold]Esc[/bold] close"
        )
    if isinstance(overlay, CommandState):
        return "[bold]Enter[/bold] run  ·  [bold]Tab[/bold] complete  ·  [bold]Esc[/bold] cancel"
    if isinstance(overlay, TextPrompt):
        return SUGGEST_HINT if overlay.suggestions else PROMPT_HINT
    if isinstance(overlay, Picker):
        return PICKER_HINT
    if isinstance(overlay, da.AccountsState):
        return da.ACCOUNTS_HINT
    if overlay is not None:  # "help"
        return "[bold]Esc[/bold] / [bold]h[/bold] close"
    return ""


def repo_heading(group: RepoGroup, selected: Row | None, folded: frozenset[str]) -> Text:
    """Render a repo heading independently of the table's data columns.

    The cursor heading is marked by :data:`CURSOR_STYLE` alone, like a
    container row; an inserted marker would shift the whole line whenever
    the cursor landed on it. The marker and the rest carry separate click targets
    (``fold`` and ``repo``).
    """
    marker = "▸" if group.prefix in folded else "▾"
    rest = f" {group.prefix}  ({len(group.containers)})"
    if group.repo_root is None:
        rest += "  (orphan)"
    if selected == Row("repo", group.prefix):
        style = CURSOR_STYLE
    else:
        style = "bold yellow" if group.repo_root is None else "bold cyan"
    heading = Text(style=style)
    heading.append(marker, style=dhit.hit_style("fold", group.prefix))
    heading.append(rest, style=dhit.hit_style("repo", group.prefix))
    return heading


def _aligned_table(
    fields: list[FieldSpecCI],
    widths: tuple[int, ...],
    *,
    show_header: bool,
    marks: tuple[bool, bool] = (False, False),
) -> Table:
    """An empty table with the dashboard's shared, fixed column geometry.

    The first title carries the same two-cell indent that
    :func:`repo_table` puts in front of every first-column cell. ``marks``
    adds a one-cell left mark after the first column and a right mark at the
    end (see :mod:`jailbee.dashboard.viewport`). Rows leave them blank.
    """
    table = Table(
        box=None,
        pad_edge=False,
        expand=False,
        show_edge=False,
        show_header=show_header,
        padding=(0, 1),
    )

    def add_mark(glyph: str, step: int) -> None:
        table.add_column(
            Text(glyph, style=Style(dim=True) + dhit.hit_style("scroll", step))
            if show_header
            else "",
            width=1,
            min_width=1,
            no_wrap=True,
        )

    for index, (field_spec, width) in enumerate(zip(fields, widths, strict=True)):
        title = ("  " if index == 0 else "") + field_spec.header
        table.add_column(
            title if show_header else "",
            justify=field_spec.justify,
            width=width,
            min_width=1,
            # Every row stays one line regardless of the current cell values.
            no_wrap=True,
            overflow="ellipsis",
        )
        if index == 0 and marks[0]:
            add_mark("\u2039", -1)
    if marks[1]:
        add_mark("\u203a", 1)
    return table


def column_header(
    fields: list[FieldSpecCI],
    widths: tuple[int, ...],
    marks: tuple[bool, bool] = (False, False),
) -> Table:
    """The column titles, drawn once above every repo section."""
    return _aligned_table(fields, widths, show_header=True, marks=marks)


def _container_cells(
    group: RepoGroup, container: ContainerInfo, fields: list[FieldSpecCI]
) -> list[str]:
    cells: list[str] = []
    for index, field_spec in enumerate(fields):
        value = (
            container.name
            if field_spec.name == "name" and group.repo_root is None
            else field_spec.cell(container)
        )
        if index == 0:
            value = "  " + value  # indent under the repo heading
        cells.append(value)
    return cells


def repo_table(
    group: RepoGroup,
    fields: list[FieldSpecCI],
    widths: tuple[int, ...],
    selected: Row | None,
    marks: tuple[bool, bool] = (False, False),
) -> Table:
    """Render one repo's rows, headerless, aligned with :func:`column_header`."""
    table = _aligned_table(fields, widths, show_header=False, marks=marks)
    for container in group.containers:
        is_selected = selected == Row("container", container.name)
        cells = _container_cells(group, container, fields)
        if marks[0]:
            cells.insert(1, "")
        if marks[1]:
            cells.append("")
        row_style = dhit.hit_style("row", container.name)
        table.add_row(
            *cells,
            style=Style.parse(CURSOR_STYLE) + row_style if is_selected else row_style,
        )
    return table


def container_row(
    group: RepoGroup,
    container: ContainerInfo,
    fields: list[FieldSpecCI],
    widths: tuple[int, ...],
    selected: Row | None,
    marks: tuple[bool, bool] = (False, False),
) -> Table:
    """One container as a single-row table, so the table can be windowed by row."""
    return repo_table(replace(group, containers=[container]), fields, widths, selected, marks)


@dataclass(frozen=True)
class _RepoSections:
    groups: list[RepoGroup]
    fields: list[FieldSpecCI]
    widths: tuple[int, ...]
    selected: Row | None
    folded: frozenset[str]
    empty: bool
    hidden_by_preferences: bool = False
    column_offset: int = 0
    max_rows: int | None = None
    """Line budget including the column header and the "more" markers; None draws every row."""

    def line_count_floor(self) -> int:
        """A lower bound on the drawn line count, without rendering anything.

        One line per heading and container row, plus the column header; a
        wrapped row draws more, so the true count is never smaller.
        """
        if self.empty:
            return 1
        expanded = [g for g in self.groups if g.containers and g.prefix not in self.folded]
        return (1 if expanded else 0) + len(self.groups) + sum(len(g.containers) for g in expanded)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        view = column_viewport(self.widths, options.max_width, self.column_offset)
        fields = [self.fields[i] for i in view.indices]
        widths = view.widths
        marks = (view.hidden_left, view.hidden_right)
        if self.empty:
            yield (
                "All repositories are hidden — open Settings > Visibility to show them"
                if self.hidden_by_preferences
                else "(no containers found)"
            )
            return
        expanded = {g.prefix for g in self.groups if g.containers and g.prefix not in self.folded}
        header = column_header(fields, widths, marks) if expanded else None
        blocks: list[tuple[Row, RenderableType]] = []
        for group in self.groups:
            blocks.append(
                (Row("repo", group.prefix), repo_heading(group, self.selected, self.folded))
            )
            if group.prefix in expanded:
                blocks += [
                    (
                        Row("container", c.name),
                        container_row(group, c, fields, widths, self.selected, marks),
                    )
                    for c in group.containers
                ]
        if self.max_rows is None:
            yield Group(*([header] if header is not None else []), *(b for _, b in blocks))
            return
        free = options.update(height=None)
        head = console.render_lines(header, free, pad=False) if header is not None else []
        rendered = [console.render_lines(b, free, pad=False) for _, b in blocks]
        rows = [row for row, _ in blocks]
        cursor = rows.index(self.selected) if self.selected in rows else None
        window = window_rows([len(r) for r in rendered], cursor, max(1, self.max_rows - len(head)))
        lines = list(head)
        if window.hidden_above:
            lines += console.render_lines(
                Text(f"  ↑ {window.hidden_above} more", style="dim"), free, pad=False
            )
        for block in rendered[window.start : window.stop]:
            lines += block
        if window.hidden_below:
            lines += console.render_lines(
                Text(f"  ↓ {window.hidden_below} more", style="dim"), free, pad=False
            )
        if len(lines) > self.max_rows and cursor is not None:
            # A cursor row taller than the budget overran its window: show that
            # row alone (its top lines) rather than let the frame cut it.
            lines = [*head, *rendered[cursor]][: self.max_rows]
        for index, line in enumerate(lines):
            if index:
                yield Segment.line()
            yield from line


def _render_overlay(overlay: Overlay, max_rows: int | None = None) -> RenderableType:
    """The overlay's panel; ``max_rows`` windows the scrollable list overlays."""
    if isinstance(overlay, EgressState):
        return render_egress(
            overlay,
            can_add=overlay.can_add,
            can_rm=overlay.can_rm and removable_entry(overlay) is not None,
        )
    if isinstance(overlay, (MenuState, RepoMenuState)):
        return _render_menu(overlay, max_rows)
    if isinstance(overlay, CommandState):
        lines = [f"> {overlay.text}▏"]
        if overlay.suggestions:
            lines.append("  " + "   ".join(overlay.suggestions))
        return Panel("\n".join(lines), title="command", box=box.ROUNDED, expand=False)
    if isinstance(overlay, SettingsState):
        return render_settings(overlay, dynamic=frozenset())
    if isinstance(overlay, TextPrompt):
        return render_prompt(overlay)
    if isinstance(overlay, Picker):
        return render_picker(overlay, max_rows)
    if isinstance(overlay, da.AccountsState):
        return da.render_accounts(overlay)
    return _render_help()


# Panel border rows: around a windowed overlay's list, and around the frame.
_OVERLAY_BORDER_ROWS = 2
_FRAME_BORDER_ROWS = 2
# Content rows a details panel needs to say anything; with fewer it is left out.
_MIN_DETAILS_ROWS = 2
# Table rows (column header not counted) kept on screen under the bottom area.
MIN_TABLE_ROWS = 5


@dataclass(frozen=True)
class _FrameBody:
    """The dashboard body, fitted to the screen.

    Top to bottom: the table window, the inline notice, a blank gap, the
    bottom area, then the hint. The bottom area is the details panel, an
    overlay, or — for an action menu — the details with the menu on the right.

    Without ``max_height`` (a plain ``console.print``) everything is drawn
    whole. Under the full-screen ``Live`` anything past the terminal's height
    would be clipped by the screen, so the table keeps :data:`MIN_TABLE_ROWS`
    before the bottom area may grow; a menu or picker is windowed to what is
    left, never below :data:`MIN_LIST_ROWS`; if even that does not fit, lines
    are dropped from the top so the bottom border and hint stay.

    ``max_height`` is passed in rather than read from ``options.height``:
    Rich's ``Screen`` wraps its renderable in a ``Group``, which resets the
    height before anything below it renders.
    """

    sections: _RepoSections
    notice: RenderableType | None
    overlay: Overlay | None
    details: DetailsView | None = None
    max_height: int | None = None

    def _bottom(
        self, list_rows: int | None, details_rows: int | None, *, fixed: bool = False
    ) -> RenderableType | None:
        """Details, an overlay, or both side by side with the menu on the right.

        ``details_rows`` caps the panel's content rows; None leaves the panel
        out. ``fixed`` pads it to exactly that many, so it keeps one shape.

        Only the action menus share the row: every other overlay is a task of
        its own (a picker, a prompt, a settings page) and gets the full width.
        """
        menu = isinstance(self.overlay, (MenuState, RepoMenuState))
        details = (
            render_details(self.details, details_rows, fixed=fixed)
            if self.details is not None
            and details_rows is not None
            and (self.overlay is None or menu)
            else None
        )
        if self.overlay is None:
            return details
        panel = _render_overlay(self.overlay, list_rows)
        if details is None:
            return panel
        row = Table.grid(expand=True)
        row.add_column(ratio=1)
        row.add_column()
        row.add_row(details, panel)
        return row

    def _details_cap(self) -> int:
        """Content rows the details panel may take: its grid plus any activity reservation."""
        return DETAILS_MAX_ROWS if self.details is None else self.details.max_rows

    def _details_fit_beside_menu(self, console: Console, options: ConsoleOptions) -> bool:
        """Whether the details keep a column wide enough to read next to a menu."""
        if not isinstance(self.overlay, (MenuState, RepoMenuState)):
            return True
        menu = Measurement.get(console, options, _render_overlay(self.overlay)).maximum
        return options.max_width - menu >= DETAILS_PAIR_WIDTH

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        hint = _hint_line(self.overlay) if self.overlay is not None else None
        extras: list[RenderableType] = [] if self.notice is None else [self.notice]
        details_fit = self._details_fit_beside_menu(console, options)
        if self.max_height is None:
            bottom = self._bottom(None, self._details_cap() if details_fit else None, fixed=True)
            tail: list[RenderableType] = [] if bottom is None else ["", bottom]
            if hint is not None:
                tail.append(hint)
            yield Group(self.sections, *extras, *tail)
            return

        free = options.update(height=None)

        def lines_of(renderable: RenderableType) -> list[list[Segment]]:
            return console.render_lines(renderable, free, pad=False)

        notice_lines = lines_of(Group(*extras)) if extras else []
        hint_lines = lines_of(hint) if hint is not None else []
        rest = self.max_height - len(notice_lines) - len(hint_lines)
        has_bottom = self.overlay is not None or self.details is not None
        gap_lines = lines_of(Text("")) if has_bottom else []
        bottom_lines: list[list[Segment]] = []
        if has_bottom:
            # The table's line count is only needed against thresholds, and a
            # table with more rows than the floor has at least that many lines.
            count = self.sections.line_count_floor()
            full = None if count > MIN_TABLE_ROWS + 1 else len(lines_of(self.sections))
            natural = count if full is None else full
            floor = min(natural, MIN_TABLE_ROWS + 1)  # + the column header
            room = max(0, rest - len(gap_lines) - floor) - _OVERLAY_BORDER_ROWS
            # A panel with fewer than two content rows says nothing: leave it out.
            details_rows = min(self._details_cap(), room)
            with_details = details_fit and details_rows >= _MIN_DETAILS_ROWS
            # Keep the panel's shape when selection or live content changes,
            # even when the table fits without scrolling.
            fixed = with_details
            bottom = self._bottom(
                max(room, MIN_LIST_ROWS), details_rows if with_details else None, fixed=fixed
            )
            if bottom is not None:
                bottom_lines = lines_of(bottom)
            else:
                gap_lines = []
        table_rows = max(0, rest - len(gap_lines) - len(bottom_lines))
        table_lines = lines_of(replace(self.sections, max_rows=table_rows))
        out = [*table_lines, *notice_lines, *gap_lines, *bottom_lines, *hint_lines]
        if len(out) > self.max_height:
            # Even the minimum bottom area does not fit: lose the top, never
            # the hint or the frame's bottom border.
            out = out[len(out) - self.max_height :]
        for index, line in enumerate(out):
            if index:
                yield Segment.line()
            yield from line


def render(
    groups: list[RepoGroup],
    selected: Row | None,
    *,
    now: datetime,
    git_enabled: bool,
    enabled: Sequence[str] | None = None,
    overlay: Overlay | None = None,
    notice: str | None = None,
    folded: frozenset[str] = frozenset(),
    column_offset: int = 0,
    hidden_by_preferences: bool = False,
    height: int | None = None,
    show_details: bool = False,
    column_widths: Mapping[str, int] | None = None,
    shown_columns: Sequence[str] | None = None,
) -> RenderableType:
    """Build the Rich renderable for one dashboard frame.

    ``column_offset`` scrolls the columns after the first; it is clamped at render time.

    Repo sections are rendered in the dashboard body with aligned columns.
    The selected row, heading or container, is marked by its
    :data:`CURSOR_STYLE` highlight alone. Wrapped in a rounded Panel whose
    left-aligned title carries the summary and the clock; the subtitle carries
    a short transient notice and nothing else.

    ``overlay`` is an open action menu or the keybinding help, drawn *below*
    the table so the dashboard it acts on stays on screen. When the frame is
    taller than the terminal the table scrolls to its cursor (keeping at least
    :data:`MIN_TABLE_ROWS` rows) and a menu or picker scrolls with its own
    (see :class:`_FrameBody`). ``height`` is the terminal's height; None draws
    everything whole.

    ``show_details`` draws the details panel for ``selected`` under the table
    — the dashboard's `v` toggle; off by default so plain renders are
    unchanged. An action menu then sits to the right of it; every other
    overlay hides it.

    ``notice`` is a transient message (a rejected key, a view-only row) shown
    in the subtitle, or — longer than :data:`_INLINE_NOTICE_MAX` — wrapped
    right below the table.
    """
    all_containers = [c for g in groups for c in g.containers]
    fields, widths = _frame_columns(
        groups,
        now=now,
        enabled=enabled,
        folded=folded,
        column_widths=column_widths,
        shown_columns=shown_columns,
    )
    visible_groups = groups
    # A notice too long for the bottom border is drawn whole, wrapped, right
    # below the table: a CLI refusal ends in its remedy ("… pass --force"),
    # which an ellipsis on the border would cut. A plain `Text`, not markup: a
    # CLI message may contain `[...]`.
    inline_notice = notice if notice and len(notice) > _INLINE_NOTICE_MAX else None
    sections = _RepoSections(
        visible_groups,
        fields,
        widths,
        selected,
        folded,
        empty=not groups,
        hidden_by_preferences=hidden_by_preferences,
        column_offset=column_offset,
    )
    details = details_for(visible_groups, selected, now) if show_details and groups else None
    n_repos = len({g.prefix for g in groups})
    n_ctr = len(all_containers)
    n_folded = len({g.prefix for g in groups if g.prefix in folded and g.containers})
    folded_note = f" · {n_folded} folded" if n_folded else ""
    git_note = "" if git_enabled else "  ·  [dim](no-git)[/dim]"
    title = (
        f"[bold]🐝 jailbee dashboard[/]  ·  [dim]h/? help[/]"
        f"  ·  {n_repos} repos · {n_ctr} containers{folded_note}{git_note}  ·  {now:%H:%M:%S}"
    )
    # Subtitle is notice-only: a short transient message on the bottom border
    # cannot push the table around. Should the terminal still be narrower than
    # a short notice, it is cut on the right so its start (the verdict) stays.
    subtitle = (
        Text(notice, style="yellow", no_wrap=True, overflow="ellipsis")
        if notice and inline_notice is None
        else None
    )
    return Panel(
        _FrameBody(
            sections,
            Text(inline_notice, style="yellow") if inline_notice is not None else None,
            overlay,
            details,
            None if height is None else max(0, height - _FRAME_BORDER_ROWS),
        ),
        title=title,
        title_align="left",
        subtitle=subtitle,
        box=box.ROUNDED,
        padding=(0, 1),
    )


@dataclass(frozen=True)
class HoverHighlight:
    """``renderable`` with the hovered click target given :data:`HOVER_STYLE`."""

    renderable: RenderableType
    hover: dhit.Hit | None

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if self.hover is None:
            yield self.renderable
            return
        target = self.hover.meta_value()
        for segment in console.render(self.renderable, options):
            style = segment.style
            if style is not None and style.meta.get(dhit.HIT_KEY) == target:
                yield Segment(segment.text, style + dhit.HOVER_STYLE, segment.control)
            else:
                yield segment


@dataclass(frozen=True)
class DashboardView:
    """Everything one frame shows: :func:`render`'s arguments plus the hovered target.

    Produced by the session after every refresh and key, consumed by both
    frontends and read by the tests in place of the old ``render`` call
    arguments. Equality is cheap enough to skip unchanged repaints.
    """

    groups: list[RepoGroup]
    selected: Row | None
    now: datetime
    git_enabled: bool
    enabled: Sequence[str] | None
    overlay: Overlay | None
    notice: str | None
    folded: frozenset[str]
    column_offset: int
    hidden_by_preferences: bool
    show_details: bool
    column_widths: Mapping[str, int] | None
    shown_columns: Sequence[str] | None
    hover: dhit.Hit | None = None

    def render_kwargs(self) -> dict[str, Any]:
        """:func:`render`'s keyword arguments (everything but ``hover``)."""
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name != "hover"}


def render_view(view: DashboardView, *, height: int | None) -> RenderableType:
    """The frame for ``view`` on a terminal ``height`` rows tall."""
    return HoverHighlight(render(**view.render_kwargs(), height=height), view.hover)
