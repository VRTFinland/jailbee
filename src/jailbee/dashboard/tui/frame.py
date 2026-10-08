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
from jailbee.dashboard.columns import FieldSpecCI, window_rows
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
from jailbee.dashboard.tui.fleet import (
    TableModel,
    entry_line,
    header_line,
    line_count,
    table_model,
)
from jailbee.dashboard.tui.fleet import (
    repo_heading as repo_heading,
)
from jailbee.dashboard.tui.keys import _GATE_NOTE, KEY_BINDINGS
from jailbee.dashboard.tui.layout import MIN_TABLE_ROWS as MIN_TABLE_ROWS
from jailbee.dashboard.tui.layout import frame_layout
from jailbee.dashboard.tui.menu_state import MenuState, RepoMenuState, _menu_entries, menu_hotkeys
from jailbee.dashboard.tui.overlay import CommandState, Overlay
from jailbee.lifecycle import ContainerInfo

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
        "Mouse: click selects; double- or right-click opens the menu;",
        "▾/▸ folds, ‹ › scroll columns, the wheel moves; Shift-drag selects text.",  # noqa: RUF001 - the arrows the frame draws
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
    now: datetime
    enabled: Sequence[str] | None
    folded: frozenset[str]
    column_widths: Mapping[str, int] | None
    shown_columns: Sequence[str] | None
    column_offset: int
    hidden_by_preferences: bool
    selected: Row | None
    max_rows: int | None = None
    """Line budget including the column header and the "more" markers; None draws every row."""

    def _model(self, width: int) -> TableModel:
        return table_model(
            self.groups,
            now=self.now,
            enabled=self.enabled,
            folded=self.folded,
            column_widths=self.column_widths,
            shown_columns=self.shown_columns,
            column_offset=self.column_offset,
            hidden_by_preferences=self.hidden_by_preferences,
            width=width,
        )

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        model = self._model(options.max_width)
        if model.empty_text is not None:
            yield model.empty_text
            return
        header = header_line(model.geometry) if model.has_header else None
        blocks = [
            (
                entry.row,
                entry_line(
                    entry,
                    model.geometry,
                    model.folded,
                    selected=entry.row == self.selected,
                    width=options.max_width,
                ),
            )
            for entry in model.entries
        ]
        if self.max_rows is None:
            # Group owns line separators; fleet's standalone lines have no terminator.
            text_lines = ([header] if header is not None else []) + [line for _, line in blocks]
            for text_line in text_lines:
                text_line.end = "\n"
            yield Group(*text_lines)
            return
        free = options.update(height=None)
        head = console.render_lines(header, free, pad=False) if header is not None else []
        rendered = [console.render_lines(line, free, pad=False) for _, line in blocks]
        rows = [row for row, _ in blocks]
        cursor = rows.index(self.selected) if self.selected in rows else None
        window = window_rows(
            [len(lines) for lines in rendered], cursor, max(1, self.max_rows - len(head))
        )
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


_FRAME_BORDER_ROWS = 2


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
        bottom_lines_rendered: list[list[Segment]] = []

        def bottom_lines(list_rows: int, details_rows: int | None) -> int:
            nonlocal bottom_lines_rendered
            bottom = self._bottom(list_rows, details_rows, fixed=details_rows is not None)
            bottom_lines_rendered = [] if bottom is None else lines_of(bottom)
            return len(bottom_lines_rendered)

        # Fleet entries are one physical line; the no-groups instruction wraps.
        placeholder_lines = lines_of(self.sections) if not self.sections.groups else None
        natural_table_lines = (
            len(placeholder_lines)
            if placeholder_lines is not None
            else line_count(self.sections.groups, self.sections.folded)
        )
        layout = frame_layout(
            height=self.max_height,
            table_lines=natural_table_lines,
            notice_lines=len(notice_lines),
            hint_lines=len(hint_lines),
            has_bottom=self.overlay is not None or self.details is not None,
            details_cap=self._details_cap(),
            details_fit=details_fit,
            bottom_lines=bottom_lines,
        )
        table_lines = (
            placeholder_lines
            if placeholder_lines is not None
            else lines_of(replace(self.sections, max_rows=layout.table_rows))
            if layout.table_rows
            else []
        )
        # window_rows may exceed a tiny budget to retain its cursor and markers.
        # The old whole-frame crop removed these excess lines from the table's top.
        table_lines = table_lines[-layout.table_rows :] if layout.table_rows else []
        # Keep the remedy at the end; [-0:] would incorrectly keep the whole notice.
        shown_notice = notice_lines[-layout.notice_rows :] if layout.notice_rows else []
        gap_lines = lines_of(Text("")) if layout.gap else []
        out = [
            *table_lines,
            *shown_notice,
            *gap_lines,
            *bottom_lines_rendered[layout.crop_top :],
            *hint_lines,
        ]
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
    visible_groups = groups
    # A notice too long for the bottom border is drawn whole, wrapped, right
    # below the table: a CLI refusal ends in its remedy ("… pass --force"),
    # which an ellipsis on the border would cut. A plain `Text`, not markup: a
    # CLI message may contain `[...]`.
    inline_notice = notice if notice and len(notice) > _INLINE_NOTICE_MAX else None
    sections = _RepoSections(
        groups=visible_groups,
        now=now,
        enabled=enabled,
        folded=folded,
        column_widths=column_widths,
        shown_columns=shown_columns,
        selected=selected,
        column_offset=column_offset,
        hidden_by_preferences=hidden_by_preferences,
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
