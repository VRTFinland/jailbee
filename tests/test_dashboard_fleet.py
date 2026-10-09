"""`tui.fleet`: one `Text` per table line, identical to the Rich table it replaces."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest
from rich.console import Console

from jailbee.dashboard import hit as dhit
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import fleet
from jailbee.dashboard.tui.frame import frame_title
from tests.dashboard_fixtures import WIDE, ci, wide_group

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
Row = dmodel.Row


def _ansi(renderable, width: int) -> list[str]:  # type: ignore[no-untyped-def]
    console = Console(width=width, file=io.StringIO(), record=True, color_system="truecolor")
    console.print(renderable, crop=False, soft_wrap=False)
    return console.export_text(styles=True).splitlines()


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


def test_lines_keep_the_rich_table_layout(tmp_path):
    import re

    groups = [wide_group(tmp_path)]
    selected = Row("container", groups[0].containers[0].name)
    plain = [
        re.sub(r"\x1b\[[0-9;]*m", "", line)
        for line in _new_lines(groups, 44, offset=1, selected=selected, enabled=WIDE)
    ]
    assert plain == [
        "  NAME                ‹  ST  AGE    LOOSE  ›",  # noqa: RUF001 - literal scroll marks
        "▾ alpha  (1)",
        "  one                    ▶   129d   ● ∞     ",
    ]


def test_a_selected_heading_and_an_orphan_keep_their_text_and_style(tmp_path):
    from jailbee.dashboard.settings import CURSOR_STYLE

    groups = [
        dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")]),
        dmodel.RepoGroup("ghost", None, None, [ci("ghost-x", "ghost")]),
    ]
    selected = fleet.repo_heading(groups[0], Row("repo", "alpha"), frozenset())
    assert selected.plain == "▾ alpha  (1)" and str(selected.style) == CURSOR_STYLE
    orphan = fleet.repo_heading(groups[1], None, frozenset())
    assert orphan.plain == "▾ ghost  (1)  (orphan)" and str(orphan.style) == "bold yellow"
    assert any("ghost-x" in line for line in _new_lines(groups, 80, selected=Row("repo", "alpha")))


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
        groups,
        now=NOW,
        enabled=None,
        folded=folded,
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=80,
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


def test_a_loose_ttl_cell_is_not_cut_in_the_default_table(tmp_path):
    import dataclasses
    import re
    from datetime import timedelta

    group = wide_group(tmp_path)
    group.containers[0] = dataclasses.replace(
        group.containers[0], loose_until=NOW + timedelta(hours=1, minutes=59, seconds=30)
    )
    plain = [
        re.sub(r"\x1b\[[0-9;]*m", "", line)
        for line in _new_lines([group], 80, enabled=("name", "network"))
    ]
    assert "● 1h59m" in plain[-1] and "…" not in plain[-1]


def _marked_model(tmp_path, **kw):  # type: ignore[no-untyped-def]
    group = dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [ci("alpha-a", "alpha"), ci("alpha-b", "alpha")]
    )
    return fleet.table_model(
        [group],
        now=NOW,
        enabled=("name",),
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=80,
        **kw,
    )


def _first_cells(model):  # type: ignore[no-untyped-def]
    return [fleet.entry_cells(e, model.geometry)[0] for e in model.entries if e.container]


def test_a_marked_row_shows_a_dot_in_its_indent(tmp_path):  # type: ignore[no-untyped-def]
    model = _marked_model(tmp_path, marked=frozenset({"alpha-a"}))

    assert _first_cells(model) == ["\u25cf a", "  b"]


def test_a_running_bulk_child_shows_the_busy_glyph_over_the_dot(tmp_path):  # type: ignore[no-untyped-def]
    model = _marked_model(tmp_path, marked=frozenset({"alpha-a"}), running=frozenset({"alpha-a"}))

    assert _first_cells(model)[0] == "\u27f3 a"


def test_a_marked_row_has_the_marked_background(tmp_path):  # type: ignore[no-untyped-def]
    model = _marked_model(tmp_path, marked=frozenset({"alpha-a"}))
    marked, plain = (e for e in model.entries if e.container)

    line = fleet.entry_line(marked, model.geometry, frozenset(), selected=False, width=80)
    other = fleet.entry_line(plain, model.geometry, frozenset(), selected=False, width=80)
    cursor = fleet.entry_line(marked, model.geometry, frozenset(), selected=True, width=80)

    assert line.style.bgcolor == fleet.MARKED_STYLE.bgcolor
    assert other.style.bgcolor is None
    assert cursor.style.bgcolor is not None


def test_a_folded_heading_counts_its_marks(tmp_path):  # type: ignore[no-untyped-def]
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])

    heading = fleet.repo_heading(group, None, frozenset({"alpha"}), marked=1)

    assert heading.plain.endswith("\u25cf1")


def test_the_frame_title_counts_the_marks():  # type: ignore[no-untyped-def]
    assert "3 selected" in frame_title([], frozenset(), git_enabled=True, now=NOW, marked=3).plain
    assert "selected" not in frame_title([], frozenset(), git_enabled=True, now=NOW).plain
