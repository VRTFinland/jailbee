"""The fleet table's lines, Textual-free: one ``rich.Text`` per line.

The table is a frozen header line (column titles and the left/right scroll marks)
over one line per repo heading and per container of an unfolded repo, in
:func:`jailbee.dashboard.model.selectable_rows` order. Columns come from
:func:`jailbee.dashboard.columns._frame_columns` through
:func:`jailbee.dashboard.viewport.column_viewport`, laid out the way the old
Rich ``Table(box=None, padding=(0, 1), pad_edge=False)`` drew them: two
cells of padding between neighbours, none at the edges, every cell cut with
an ellipsis, never wrapped. ``FleetTable`` draws these lines; the tests read
them directly.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from rich.style import Style
from rich.text import Text

from jailbee.dashboard import hit as dhit
from jailbee.dashboard.columns import FieldSpecCI, _frame_columns
from jailbee.dashboard.model import RepoGroup, Row, selectable_rows
from jailbee.dashboard.settings import CURSOR_STYLE
from jailbee.dashboard.sorting import DEFAULT_SORT, SortSpec
from jailbee.dashboard.viewport import column_viewport
from jailbee.lifecycle import ContainerInfo

EMPTY_TEXT = "(no containers found)"
HIDDEN_TEXT = "All repositories are hidden — open Settings > Visibility to show them"
_GAP = "  "
_HEADER_STYLE = Style(bold=True)
# A marked row's background. The `●` in the indent carries the mark for
# NO_COLOR and 16-colour terminals; the background is the at-a-glance cue.
MARKED_STYLE = Style(bgcolor="blue")


@dataclass(frozen=True)
class Geometry:
    """The drawn columns at one width and offset: first column frozen, marks optional."""

    names: tuple[str, ...]
    headers: tuple[str, ...]
    justify: tuple[str, ...]
    widths: tuple[int, ...]
    marks: tuple[bool, bool]
    fields: tuple[FieldSpecCI, ...] = field(compare=False, repr=False)

    @property
    def width(self) -> int:
        cells = list(self.widths) + [1] * sum(self.marks)
        return sum(cells) + len(_GAP) * max(0, len(cells) - 1)


@dataclass(frozen=True)
class Entry:
    """One line under the header: a repo heading or a container."""

    row: Row
    group: RepoGroup = field(compare=False, repr=False)
    container: ContainerInfo | None = None
    heading: tuple[int, bool, int] | None = None
    """For a heading: (container count, orphan, marked count) — what its text depends on."""
    marked: bool = False
    running: bool = False


@dataclass(frozen=True)
class TableModel:
    entries: tuple[Entry, ...]
    geometry: Geometry
    folded: frozenset[str]
    now: datetime
    has_header: bool
    empty_text: str | None

    @property
    def line_count(self) -> int:
        if self.empty_text is not None:
            return 1
        return int(self.has_header) + len(self.entries)

    @property
    def rows(self) -> tuple[Row, ...]:
        return tuple(entry.row for entry in self.entries)


def line_count(groups: Sequence[RepoGroup], folded: frozenset[str]) -> int:
    """The table's lines without building it: header, headings, unfolded rows."""
    if not groups:
        return 1
    rows = selectable_rows(list(groups), folded)
    has_header = any(group.containers and group.prefix not in folded for group in groups)
    return int(has_header) + len(rows)


def table_model(
    groups: Sequence[RepoGroup],
    *,
    now: datetime,
    enabled: Sequence[str] | None,
    folded: frozenset[str],
    column_widths: Mapping[str, int] | None,
    shown_columns: Sequence[str] | None,
    column_offset: int,
    hidden_by_preferences: bool,
    width: int,
    sort: SortSpec = DEFAULT_SORT,
    marked: frozenset[str] = frozenset(),
    running: frozenset[str] = frozenset(),
) -> TableModel:
    """Everything the table draws at ``width`` cells, columns scrolled by ``column_offset``."""
    fields, widths = _frame_columns(
        list(groups),
        now=now,
        enabled=enabled,
        folded=folded,
        column_widths=column_widths,
        shown_columns=shown_columns,
        available=width,
        sort=sort,
    )
    view = column_viewport(widths, width, column_offset)
    shown = tuple(fields[index] for index in view.indices)
    geometry = Geometry(
        names=tuple(spec.name for spec in shown),
        headers=tuple(spec.header for spec in shown),
        justify=tuple(spec.justify for spec in shown),
        widths=view.widths,
        marks=(view.hidden_left, view.hidden_right),
        fields=shown,
    )
    entries: list[Entry] = []
    for group in groups:
        entries.append(
            Entry(
                Row("repo", group.prefix),
                group,
                None,
                (
                    len(group.containers),
                    group.repo_root is None,
                    sum(1 for c in group.containers if c.name in marked),
                ),
            )
        )
        if group.prefix not in folded:
            entries.extend(
                Entry(
                    Row("container", container.name),
                    group,
                    container,
                    marked=container.name in marked,
                    running=container.name in running,
                )
                for container in group.containers
            )
    empty_text = None
    if not groups:
        empty_text = HIDDEN_TEXT if hidden_by_preferences else EMPTY_TEXT
    return TableModel(
        entries=tuple(entries),
        geometry=geometry,
        folded=folded,
        now=now,
        has_header=any(group.containers and group.prefix not in folded for group in groups),
        empty_text=empty_text,
    )


def _cell(markup: str | Text, width: int, justify: str) -> Text:
    text = markup.copy() if isinstance(markup, Text) else Text.from_markup(markup)
    text.no_wrap = True
    text.end = ""
    text.truncate(width, overflow="ellipsis")
    text.align(
        "right" if justify == "right" else "center" if justify == "center" else "left", width
    )
    return text


def _join(cells: list[Text], style: Style | str = "") -> Text:
    line = Text(style=style, no_wrap=True, end="")
    for index, cell in enumerate(cells):
        if index:
            line.append(" ", style=style)
            line.append(" ", style=style)
        line.append_text(cell)
    return line


def _with_marks(cells: list[Text], geometry: Geometry, left: Text, right: Text) -> list[Text]:
    if geometry.marks[0]:
        cells.insert(1, left)
    if geometry.marks[1]:
        cells.append(right)
    return cells


def header_line(geometry: Geometry) -> Text:
    """The column titles, each a ``sort`` target; the first carries the rows' two-cell indent."""
    cells = [
        _cell(
            Text(
                ("  " if index == 0 else "") + header,
                style=_HEADER_STYLE + dhit.hit_style("sort", name),
            ),
            width,
            justify,
        )
        for index, (name, header, width, justify) in enumerate(
            zip(geometry.names, geometry.headers, geometry.widths, geometry.justify, strict=True)
        )
    ]

    def mark(glyph: str, step: int) -> Text:
        return Text(glyph, style=_HEADER_STYLE + Style(dim=True) + dhit.hit_style("scroll", step))

    return _join(_with_marks(cells, geometry, mark("\u2039", -1), mark("\u203a", 1)), _HEADER_STYLE)


def entry_cells(entry: Entry, geometry: Geometry) -> tuple[str, ...]:
    """A container line's cell values (first indented under its heading); () for a heading."""
    if entry.container is None:
        return ()
    cells: list[str] = []
    for index, spec in enumerate(geometry.fields):
        value = (
            entry.container.name
            if spec.name == "name" and entry.group.repo_root is None
            else spec.cell(entry.container)
        )
        indent = "⟳ " if entry.running else "● " if entry.marked else "  "
        cells.append((indent + value) if index == 0 else value)
    return tuple(cells)


def repo_heading(
    group: RepoGroup, selected: Row | None, folded: frozenset[str], marked: int = 0
) -> Text:
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
    if marked:
        rest += f"  ●{marked}"
    if selected == Row("repo", group.prefix):
        style = CURSOR_STYLE
    else:
        style = "bold yellow" if group.repo_root is None else "bold cyan"
    heading = Text(style=style)
    heading.append(marker, style=dhit.hit_style("fold", group.prefix))
    heading.append(rest, style=dhit.hit_style("repo", group.prefix))
    return heading


def entry_line(
    entry: Entry,
    geometry: Geometry,
    folded: frozenset[str],
    *,
    selected: bool,
    width: int,
) -> Text:
    """One heading or container line; ``width`` cuts an over-long heading."""
    if entry.container is None:
        heading = repo_heading(
            entry.group,
            entry.row if selected else None,
            folded,
            marked=entry.heading[2] if entry.heading else 0,
        )
        heading.no_wrap = True
        heading.end = ""
        heading.truncate(width, overflow="ellipsis")
        return heading
    values = entry_cells(entry, geometry)
    cells = [
        _cell(value, cell_width, justify)
        for value, cell_width, justify in zip(
            values, geometry.widths, geometry.justify, strict=True
        )
    ]
    hit = dhit.hit_style("row", entry.container.name)
    base = MARKED_STYLE + hit if entry.marked else hit
    style = Style.parse(CURSOR_STYLE) + base if selected else base
    return _join(_with_marks(cells, geometry, Text(" "), Text(" ")), style)
