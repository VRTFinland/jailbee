"""`tui.fleet`: one `Text` per table line, identical to the Rich table it replaces."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest
from rich.console import Console

from jailbee.dashboard import columns as dcolumns
from jailbee.dashboard import hit as dhit
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import fleet
from jailbee.dashboard.tui import frame as tframe
from jailbee.dashboard.viewport import column_viewport
from tests.dashboard_fixtures import WIDE, ci, wide_group

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
Row = dmodel.Row


def _ansi(renderable, width: int) -> list[str]:  # type: ignore[no-untyped-def]
    console = Console(width=width, file=io.StringIO(), record=True, color_system="truecolor")
    console.print(renderable, crop=False, soft_wrap=False)
    return console.export_text(styles=True).splitlines()


def _old_lines(groups, width, *, offset=0, selected=None, enabled=None):  # type: ignore[no-untyped-def]
    fields, widths = dcolumns._frame_columns(
        groups, now=NOW, enabled=enabled, folded=frozenset(), column_widths=None, shown_columns=None
    )
    view = column_viewport(widths, width, offset)
    vf = [fields[i] for i in view.indices]
    marks = (view.hidden_left, view.hidden_right)
    lines = _ansi(tframe.column_header(vf, view.widths, marks), width)
    for group in groups:
        lines += _ansi(tframe.repo_heading(group, selected, frozenset()), width)
        for c in group.containers:
            lines += _ansi(tframe.container_row(group, c, vf, view.widths, selected, marks), width)
    return lines


def _new_lines(groups, width, *, offset=0, selected=None, enabled=None):  # type: ignore[no-untyped-def]
    model = fleet.table_model(
        groups,
        now=NOW,
        enabled=enabled,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=offset,
        hidden_by_preferences=False,
        width=width,
    )
    lines = _ansi(fleet.header_line(model.geometry), width)
    for entry in model.entries:
        line = fleet.entry_line(
            entry, model.geometry, model.folded, selected=entry.row == selected, width=width
        )
        lines += _ansi(line, width)
    return lines


@pytest.mark.parametrize("width", [36, 44, 80, 200])
@pytest.mark.parametrize("offset", [0, 1, 3])
def test_lines_match_the_rich_table(tmp_path, width, offset):
    groups = [wide_group(tmp_path)]
    selected = Row("container", groups[0].containers[0].name)
    kw = {"offset": offset, "selected": selected, "enabled": WIDE}
    assert _new_lines(groups, width, **kw) == _old_lines(groups, width, **kw)


def test_a_selected_heading_and_an_orphan_match(tmp_path):
    groups = [
        dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")]),
        dmodel.RepoGroup("ghost", None, None, [ci("ghost-x", "ghost")]),
    ]
    sel = Row("repo", "alpha")
    assert _new_lines(groups, 80, selected=sel) == _old_lines(groups, 80, selected=sel)


def test_a_heading_longer_than_the_table_is_cut_not_wrapped(tmp_path):
    group = dmodel.RepoGroup("a" * 60, str(tmp_path), None, [ci("x", "a" * 60)])
    model = fleet.table_model(
        [group],
        now=NOW,
        enabled=None,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=30,
    )
    line = fleet.entry_line(
        model.entries[0], model.geometry, model.folded, selected=False, width=30
    )
    assert line.cell_len <= 30 and line.plain.endswith("…")


def test_line_count_counts_headings_header_and_unfolded_rows(tmp_path):
    a = dmodel.RepoGroup("a", str(tmp_path), None, [ci("a-1", "a"), ci("a-2", "a")])
    b = dmodel.RepoGroup("b", str(tmp_path), None, [ci("b-1", "b")])
    empty = dmodel.RepoGroup("e", str(tmp_path), None, [])
    assert fleet.line_count([a, b, empty], frozenset()) == 1 + 3 + 3
    assert fleet.line_count([a, b], frozenset({"a", "b"})) == 2  # no header: nothing unfolded
    assert fleet.line_count([], frozenset()) == 1  # the empty text


def test_the_model_rows_follow_selectable_rows(tmp_path):
    a = dmodel.RepoGroup("a", str(tmp_path), None, [ci("a-1", "a")])
    model = fleet.table_model(
        [a],
        now=NOW,
        enabled=None,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=80,
    )
    assert list(model.rows) == dmodel.selectable_rows([a], frozenset())


@pytest.mark.parametrize("folded", [frozenset({"a"}), frozenset({"a", "b"})])
def test_folded_models_entries_header_and_count(tmp_path, folded):
    groups = [
        dmodel.RepoGroup("a", str(tmp_path), None, [ci("a-1", "a")]),
        dmodel.RepoGroup("b", str(tmp_path), None, [ci("b-1", "b")]),
    ]
    model = fleet.table_model(
        groups, now=NOW, enabled=None, folded=folded, column_widths=None,
        shown_columns=None, column_offset=0, hidden_by_preferences=False, width=80,
    )
    expected = [Row("repo", "a"), Row("repo", "b")]
    if "b" not in folded:
        expected.append(Row("container", "b-1"))
    assert [entry.row for entry in model.entries] == expected
    assert model.has_header == ("b" not in folded)
    assert model.line_count == len(expected) + int(model.has_header)
    assert model.line_count == fleet.line_count(groups, folded)


def test_empty_and_hidden_texts(tmp_path):
    kw = dict(
        now=NOW,
        enabled=None,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        width=80,
    )
    assert fleet.table_model([], hidden_by_preferences=False, **kw).empty_text == fleet.EMPTY_TEXT
    assert fleet.table_model([], hidden_by_preferences=True, **kw).empty_text == fleet.HIDDEN_TEXT


def test_row_hits_cover_the_padding_and_the_fold_marker_one_cell(tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    model = fleet.table_model(
        [group],
        now=NOW,
        enabled=None,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=80,
    )
    console = Console(width=80, file=io.StringIO())
    heading, row = model.entries
    row_segments = list(
        fleet.entry_line(row, model.geometry, model.folded, selected=False, width=80).render(
            console
        )
    )
    assert all(
        dhit.Hit.of(s.style.meta if s.style else {}) == dhit.Hit("row", ("alpha-one",))
        for s in row_segments
        if s.text
    )
    fold = [
        s
        for s in fleet.entry_line(
            heading, model.geometry, model.folded, selected=False, width=80
        ).render(console)
        if s.style and dhit.Hit.of(s.style.meta) == dhit.Hit("fold", ("alpha",))
    ]
    assert sum(len(s.text) for s in fold) == 1
