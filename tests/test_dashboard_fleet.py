"""`tui.fleet`: one `Text` per table line, identical to the Rich table it replaces."""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from rich.console import Console
from rich.style import Style

from jailbee.dashboard import hit as dhit
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import fleet
from jailbee.dashboard.tui.frame import frame_title
from jailbee.orchestration.view import ChildView
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


def test_merge_roles_mark_the_source_and_dim_the_ineligible(tmp_path):  # type: ignore[no-untyped-def]
    group = dmodel.RepoGroup(
        "r",
        str(tmp_path),
        None,
        [ci("r-a", "r"), ci("r-b", "r"), ci("r-s", "r", "Stopped")],
    )
    model = fleet.table_model(
        [group],
        now=NOW,
        enabled=("name",),
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=80,
        marked=frozenset({"r-a"}),
        sources=frozenset({"r-b"}),
        ineligible=frozenset({"r-s"}),
    )
    a, b, s = (e for e in model.entries if e.container)

    assert [fleet.entry_cells(e, model.geometry)[0][:2] for e in (a, b, s)] == [
        "● ",
        "◆ ",
        "  ",
    ]

    def line(entry):  # type: ignore[no-untyped-def]
        return fleet.entry_line(entry, model.geometry, frozenset(), selected=False, width=80)

    assert line(s).style.dim
    assert not line(a).style.dim
    assert not line(b).style.dim
    assert line(a).style.bgcolor == fleet.MARKED_STYLE.bgcolor


def test_a_stopped_container_row_is_dim(tmp_path):  # type: ignore[no-untyped-def]
    group = dmodel.RepoGroup(
        "r",
        str(tmp_path),
        None,
        [ci("r-run", "r"), ci("r-stop", "r", "Stopped"), ci("r-frozen", "r", "Frozen")],
    )
    model = fleet.table_model(
        [group],
        now=NOW,
        enabled=("name",),
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=80,
    )
    run, stop, frozen = (e for e in model.entries if e.container)

    def line(entry, selected=False):  # type: ignore[no-untyped-def]
        return fleet.entry_line(entry, model.geometry, frozenset(), selected=selected, width=80)

    assert line(stop).style.dim
    assert line(stop, selected=True).style.dim
    assert not line(run).style.dim
    assert not line(frozen).style.dim


def test_a_heading_shows_the_checked_out_branch(tmp_path):  # type: ignore[no-untyped-def]
    group = dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [ci("alpha-a", "alpha")], checkout="main"
    )

    assert fleet.repo_heading(group, None, frozenset()).plain == "\u25be alpha [main]  (1)"


def test_a_heading_without_a_checkout_is_unchanged(tmp_path):  # type: ignore[no-untyped-def]
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])

    assert fleet.repo_heading(group, None, frozenset()).plain == "\u25be alpha  (1)"


@pytest.mark.parametrize("selected", [False, True])
def test_a_failed_autostart_flags_the_name_in_the_attention_colour(tmp_path, selected):  # type: ignore[no-untyped-def]
    import dataclasses

    from jailbee import background

    failed = dataclasses.replace(
        ci("alpha-a", "alpha"),
        job_kind=background.JOB_AUTOSTART,
        job_phase=background.PHASE_FAILED,
    )
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [failed, ci("alpha-b", "alpha")])
    model = fleet.table_model(
        [group],
        now=NOW,
        enabled=("name",),
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=80,
    )
    bad, fine = (e for e in model.entries if e.container)

    line = fleet.entry_line(bad, model.geometry, frozenset(), selected=selected, width=80)

    assert "⚠" in line.plain
    assert (
        "⚠"
        not in fleet.entry_line(fine, model.geometry, frozenset(), selected=False, width=80).plain
    )
    assert line.get_style_at_offset(Console(), line.plain.index("⚠")).color.name == "yellow"


@pytest.mark.parametrize("selected", [False, True])
def test_the_heading_warning_keeps_the_attention_colour(tmp_path, selected):  # type: ignore[no-untyped-def]
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])
    row = Row("repo", "alpha") if selected else None

    heading = fleet.repo_heading(group, row, frozenset(), warned=True)

    style = heading.get_style_at_offset(Console(), heading.plain.index("⚠"))
    assert style.color.name == "yellow"


def _name_model(group, *, column_widths=None):  # type: ignore[no-untyped-def]
    return fleet.table_model(
        [group],
        now=NOW,
        enabled=("name",),
        folded=frozenset(),
        column_widths=column_widths,
        shown_columns=("name",),
        column_offset=0,
        hidden_by_preferences=False,
        width=80,
    )


def _fork_rows():  # type: ignore[no-untyped-def]
    parent = ci("p-parent", "p")
    fork = replace(ci("p-fork", "p"), child=ChildView("p-parent", "working", (), False, ()))
    deep = replace(ci("p-deep", "p"), child=ChildView("p-fork", "working", (), False, ()))
    orphan = replace(ci("p-orph", "p"), child=ChildView("p-gone", "working", (), False, ()))
    return [parent, fork, deep, orphan]


def test_fork_rows_carry_the_tree_prefix_after_the_status_indent(tmp_path):
    group = dmodel.RepoGroup("p", str(tmp_path), None, _fork_rows())
    model = _name_model(group)
    names = [fleet.entry_cells(e, model.geometry)[0] for e in model.entries if e.container]
    assert names == ["  parent", "  └ fork", "    └ deep", "  ⑂ orph"]


def test_entry_equality_changes_when_the_parent_disappears(tmp_path):
    fork = replace(ci("p-fork", "p"), child=ChildView("p-parent", "working", (), False, ()))
    with_parent = dmodel.RepoGroup("p", str(tmp_path), None, [ci("p-parent", "p"), fork])
    without_parent = dmodel.RepoGroup("p", str(tmp_path), None, [fork])
    a = next(e for e in _name_model(with_parent).entries if e.container == fork)
    b = next(e for e in _name_model(without_parent).entries if e.container == fork)
    assert a.fork_prefix == "└ "
    assert b.fork_prefix == "⑂ "
    assert a != b


def test_deep_fork_name_is_cut_with_an_ellipsis_not_wrapped(tmp_path):
    rows = [ci("p-c0", "p")]
    for i in range(1, 6):
        rows.append(
            replace(ci(f"p-c{i}", "p"), child=ChildView(f"p-c{i - 1}", "working", (), False, ()))
        )
    group = dmodel.RepoGroup("p", str(tmp_path), None, rows)
    model = _name_model(group, column_widths={"name": 10})
    entry = next(e for e in model.entries if e.container and e.container.name == "p-c5")
    line = fleet.entry_line(entry, model.geometry, model.folded, selected=False, width=80)
    assert "\n" not in line.plain
    assert line.plain.rstrip().endswith("…")


def _fitted_name_model(group):  # type: ignore[no-untyped-def]
    """The NAME column as the dashboard's width fitting (`o`, auto-fit) sizes it."""
    from jailbee.dashboard.columns import optimize_column_widths

    widths = optimize_column_widths([group], now=NOW, enabled=("name",))
    return _name_model(group, column_widths=widths)


def test_a_long_name_is_capped_and_cut_at_the_end(tmp_path):  # type: ignore[no-untyped-def]
    from jailbee.lifecycle import NAME_MAX_WIDTH

    long = "x" * (NAME_MAX_WIDTH + 20)
    group = dmodel.RepoGroup("p", str(tmp_path), None, [ci(f"p-{long}", "p")])
    model = _fitted_name_model(group)
    # The first column carries the two-cell status indent on top of the cap.
    assert model.geometry.widths[0] == NAME_MAX_WIDTH + 2
    entry = next(e for e in model.entries if e.container)
    line = fleet.entry_line(entry, model.geometry, model.folded, selected=False, width=200)
    assert line.plain.rstrip().endswith("…")
    assert len(line.plain.rstrip()) == NAME_MAX_WIDTH + 2


def test_a_long_base_branch_is_capped_like_the_name(tmp_path):  # type: ignore[no-untyped-def]
    """A long base branch must not widen the table; the details card has it in full."""
    from jailbee.dashboard.columns import optimize_column_widths
    from jailbee.lifecycle import NAME_MAX_WIDTH

    row = replace(ci("p-a", "p"), base_branch="feature/" + "b" * 60)
    group = dmodel.RepoGroup("p", str(tmp_path), None, [row])
    widths = optimize_column_widths([group], now=NOW, enabled=("name", "base"))
    assert widths["base"] == NAME_MAX_WIDTH


def test_a_capped_fork_name_counts_its_tree_prefix(tmp_path):  # type: ignore[no-untyped-def]
    from jailbee.lifecycle import NAME_MAX_WIDTH

    long = "y" * NAME_MAX_WIDTH
    parent = ci("p-parent", "p")
    fork = replace(ci(f"p-{long}", "p"), child=ChildView("p-parent", "working", (), False, ()))
    model = _fitted_name_model(dmodel.RepoGroup("p", str(tmp_path), None, [parent, fork]))
    entry = next(e for e in model.entries if e.container == fork)
    line = fleet.entry_line(entry, model.geometry, model.folded, selected=False, width=200)
    assert line.plain.rstrip() == "  └ " + "y" * (NAME_MAX_WIDTH - 3) + "…"


def test_a_capped_name_keeps_its_failed_autostart_mark(tmp_path):  # type: ignore[no-untyped-def]
    from jailbee import background
    from jailbee.lifecycle import NAME_MAX_WIDTH

    long = "z" * (NAME_MAX_WIDTH + 20)
    failed = replace(
        ci(f"p-{long}", "p"),
        job_kind=background.JOB_AUTOSTART,
        job_phase=background.PHASE_FAILED,
    )
    model = _fitted_name_model(dmodel.RepoGroup("p", str(tmp_path), None, [failed]))
    entry = next(e for e in model.entries if e.container)
    line = fleet.entry_line(entry, model.geometry, model.folded, selected=False, width=200)
    assert line.plain.rstrip().endswith("… ⚠")
    assert len(line.plain.rstrip()) == NAME_MAX_WIDTH + 2


def _phase_lines(tmp_path, *containers):  # type: ignore[no-untyped-def]  # test helper
    group = dmodel.RepoGroup("r", str(tmp_path), None, list(containers))
    model = fleet.table_model(
        [group],
        now=NOW,
        enabled=("name",),
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=80,
    )
    return [
        fleet.entry_line(e, model.geometry, frozenset(), selected=False, width=80)
        for e in model.entries
        if e.container
    ]


def _job(name, kind, phase, state="Running", pid=None, failed=0):  # type: ignore[no-untyped-def]  # test helper
    c = ci(name, "r", state, job_phase=phase, job_pid=pid)
    c.job_kind = kind
    c.job_live = pid is not None and phase != "failed"
    c.autostart_failed = failed
    return c


def test_phase_rows_get_distinct_styles(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from jailbee import background

    monkeypatch.setattr(background, "worker_alive", lambda pid: True)
    boot = _job("r-boot", background.JOB_BOOT, "starting", "Stopped", pid=1)
    auto = _job("r-auto", background.JOB_AUTOSTART, "autostart:claude", pid=1)
    other = _job("r-job", background.JOB_STOP, "stopping", pid=1)
    dead = _job("r-dead", background.JOB_CREATE, "failed")
    afail = _job("r-afail", None, None, failed=2)
    afail.job_phase = None
    lines = _phase_lines(tmp_path, boot, auto, other, dead, afail)
    colours = [line.style.color.name for line in lines]
    assert colours == ["orange3", "dark_cyan", "magenta", "red", "red"]
    assert not any(line.style.dim for line in lines)  # a booting Stopped row is not dim


def test_a_row_at_rest_has_no_phase_colour(tmp_path):  # type: ignore[no-untyped-def]
    (line,) = _phase_lines(tmp_path, ci("r-run", "r"))
    assert line.style.color is None


@pytest.mark.parametrize("kind", ["boot", "create"])
def test_a_job_in_the_autostart_phase_is_an_autostart_row(tmp_path, monkeypatch, kind):  # type: ignore[no-untyped-def]
    from jailbee import background
    from jailbee.dashboard.format import row_phase

    monkeypatch.setattr(background, "worker_alive", lambda pid: True)
    c = _job("r-x", getattr(background, f"JOB_{kind.upper()}"), background.PHASE_AUTOSTART, pid=1)
    assert row_phase(c) == "autostart"


def test_a_stopped_row_with_an_old_autostart_failure_stays_dim(tmp_path):  # type: ignore[no-untyped-def]
    from jailbee.dashboard.format import row_phase

    c = _job("r-s", None, None, state="Stopped", failed=2)
    assert row_phase(c) is None
    (line,) = _phase_lines(tmp_path, c)
    assert line.style.dim
    assert line.style.color is None


def test_a_running_row_with_an_autostart_failure_is_still_red(tmp_path):  # type: ignore[no-untyped-def]
    (line,) = _phase_lines(tmp_path, _job("r-f", None, None, failed=2))
    assert line.style.color.name == "red"


def test_a_marked_phase_row_drops_the_phase_foreground(tmp_path):  # type: ignore[no-untyped-def]
    from jailbee import background

    dead = _job("r-dead", background.JOB_CREATE, "failed")
    group = dmodel.RepoGroup("r", str(tmp_path), None, [dead])
    model = fleet.table_model(
        [group],
        now=NOW,
        enabled=("name",),
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=80,
        marked=frozenset({"r-dead"}),
    )
    (entry,) = (e for e in model.entries if e.container)
    line = fleet.entry_line(entry, model.geometry, frozenset(), selected=False, width=80)
    assert line.style.bgcolor == fleet.MARKED_STYLE.bgcolor
    assert line.style.color is None


def test_phase_styles_are_distinct_and_avoid_plain_yellow():
    from jailbee.dashboard.format import ROW_PHASE_STYLES

    assert len(set(ROW_PHASE_STYLES.values())) == len(ROW_PHASE_STYLES)
    assert "yellow" not in ROW_PHASE_STYLES.values()


def _merging(name, in_progress="merge", unmerged=None):  # type: ignore[no-untyped-def]  # test helper
    from jailbee.git_status import GitStatus

    gs = GitStatus(
        wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok", in_progress=in_progress
    )
    return ci(name, "r", git_status=replace(gs, unmerged=unmerged))


@pytest.mark.parametrize(
    ("in_progress", "unmerged", "expected"),
    [
        ("merge", None, True),
        ("merge", 2, True),
        ("", 2, True),
        ("", 0, False),
        ("", None, False),
        ("?", None, False),
        ("rebase", None, False),
    ],
)
def test_merge_in_progress_follows_the_probe(in_progress, unmerged, expected):  # type: ignore[no-untyped-def]
    from jailbee.dashboard.format import merge_in_progress

    assert merge_in_progress(_merging("r-m", in_progress, unmerged)) is expected


def test_merge_in_progress_is_false_without_a_git_probe():
    from jailbee.dashboard.format import merge_in_progress

    assert merge_in_progress(ci("r-x", "r")) is False


def _name_line(tmp_path, container, width=80):  # type: ignore[no-untyped-def]
    group = dmodel.RepoGroup("r", str(tmp_path), None, [container])
    model = _fitted_name_model(group)
    entry = next(e for e in model.entries if e.container)
    return fleet.entry_line(entry, model.geometry, model.folded, selected=False, width=width)


def test_a_merging_row_carries_a_marker_distinct_from_the_phase_styles(tmp_path):  # type: ignore[no-untyped-def]
    from jailbee.dashboard.format import MERGE_MARK_GLYPH, ROW_PHASE_STYLES

    line = _name_line(tmp_path, _merging("r-m"))
    assert MERGE_MARK_GLYPH in line.plain
    style = line.get_style_at_offset(Console(), line.plain.index(MERGE_MARK_GLYPH))
    # Explicit foreground AND background: legible on light and dark terminals.
    assert style.color is not None and style.bgcolor is not None and style.bold
    # Phase styles are foreground-only, so the marker's background sets it apart.
    assert all(Style.parse(v).bgcolor is None for v in ROW_PHASE_STYLES.values())
    assert MERGE_MARK_GLYPH not in _name_line(tmp_path, _merging("r-m", "", None)).plain


def test_the_marker_leaves_with_the_merge(tmp_path):  # type: ignore[no-untyped-def]
    from jailbee.dashboard.format import MERGE_MARK_GLYPH

    assert MERGE_MARK_GLYPH not in _name_line(tmp_path, _merging("r-m", "", 0)).plain


def test_a_capped_name_keeps_its_merge_and_autostart_marks(tmp_path):  # type: ignore[no-untyped-def]
    from jailbee import background
    from jailbee.dashboard.format import MERGE_MARK_GLYPH
    from jailbee.lifecycle import NAME_MAX_WIDTH

    long = "z" * (NAME_MAX_WIDTH + 20)
    c = replace(
        _merging(f"r-{long}"),
        job_kind=background.JOB_AUTOSTART,
        job_phase=background.PHASE_FAILED,
    )
    line = _name_line(tmp_path, c, width=200)
    assert line.plain.rstrip().endswith(f"… {MERGE_MARK_GLYPH} ⚠")
    assert len(line.plain.rstrip()) == NAME_MAX_WIDTH + 2
