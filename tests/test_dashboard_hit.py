"""Click targets in the dashboard frame: hit tags, hover, and the view seam."""

from __future__ import annotations

import io
from datetime import UTC, datetime

from rich.console import Console, Group, RenderableType

from jailbee.dashboard import hit as dhit
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.egress import EgressState
from jailbee.dashboard.overlays import Picker, PickerEntry, TextPrompt
from jailbee.dashboard.settings import open_settings
from jailbee.dashboard.tui import fleet
from jailbee.dashboard.tui import frame as tframe
from jailbee.dashboard.tui.menu_state import open_repo_menu
from tests.dashboard_fixtures import WIDE, ci, wide_group

_NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _hits(renderable: RenderableType, width: int = 120) -> list[tuple[int, int, dhit.Hit]]:
    """Every tagged cell as (x, y, hit), top-left first."""
    console = Console(width=width, file=io.StringIO(), color_system="truecolor")
    found = []
    for y, line in enumerate(console.render_lines(renderable, pad=False)):
        x = 0
        for segment in line:
            hit = dhit.Hit.of(segment.style.meta) if segment.style else None
            if hit is not None:
                found += [(x + i, y, hit) for i in range(len(segment.text))]
            x += len(segment.text)
    return found


def _kinds(renderable: RenderableType, width: int = 120) -> set[dhit.Hit]:
    return {hit for _, _, hit in _hits(renderable, width)}


def _table(groups, *, width=120, enabled=None, column_offset=0):
    """Tagged pure header/entry lines at the old frame's content width."""
    inner = width - 4
    model = fleet.table_model(
        groups,
        now=_NOW,
        enabled=enabled,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        column_offset=column_offset,
        hidden_by_preferences=False,
        width=inner,
    )
    return Group(
        fleet.header_line(model.geometry),
        *[
            fleet.entry_line(e, model.geometry, model.folded, selected=False, width=inner)
            for e in model.entries
        ],
    )


def test_hit_round_trips_through_style_meta_and_markup():
    hit = dhit.Hit("picker", (3,))
    assert dhit.Hit.of(dhit.hit_style("picker", 3).meta) == hit
    text = Console().render_str(dhit.hit_markup("[bold]x[/]", "picker", 3))
    assert dhit.Hit.of(text.spans[0].style.meta) == hit  # type: ignore[union-attr]  # markup spans carry Style objects
    assert dhit.Hit.of({}) is None
    assert dhit.Hit.of({dhit.HIT_KEY: "garbage"}) is None


def test_container_rows_headings_and_markers_are_tagged(tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    kinds = _kinds(_table([group]))
    assert dhit.Hit("row", ("alpha-one",)) in kinds
    assert dhit.Hit("repo", ("alpha",)) in kinds
    assert dhit.Hit("fold", ("alpha",)) in kinds


def test_the_fold_hit_covers_only_the_marker(tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    cells = [(x, y) for x, y, h in _hits(_table([group])) if h == dhit.Hit("fold", ("alpha",))]
    assert len(cells) == 1


def test_a_row_hit_covers_the_cell_padding_too(tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    hits = _hits(_table([group]))
    row_y = {y for _, y, h in hits if h.kind == "row"}
    assert len(row_y) == 1
    xs = sorted(x for x, y, h in hits if h.kind == "row")
    assert xs == list(range(xs[0], xs[-1] + 1))  # no untagged gap between cells


def test_scroll_marks_are_tagged_with_their_direction(tmp_path):
    group = wide_group(tmp_path)
    kinds = _kinds(
        _table([group], width=44, enabled=WIDE, column_offset=1),
        width=44,
    )
    assert dhit.Hit("scroll", (-1,)) in kinds


def test_overlay_entries_are_tagged_by_index(tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    menu = open_repo_menu([group], "alpha", frozenset())
    assert menu is not None
    assert {dhit.Hit("menu", (0,)), dhit.Hit("menu", (1,))} <= _kinds(tframe._render_overlay(menu))
    picker = Picker("p", "Pick", (PickerEntry("a", "a"), PickerEntry("b", "b")))
    assert _kinds(tframe._render_overlay(picker)) == {
        dhit.Hit("picker", (0,)),
        dhit.Hit("picker", (1,)),
    }
    prompt = TextPrompt("x", "T", "Base", suggestions=("main", "dev"))
    assert _kinds(tframe._render_overlay(prompt)) == {
        dhit.Hit("suggestion", (0,)),
        dhit.Hit("suggestion", (1,)),
    }
    settings = open_settings(
        field_names=("name", "state"),
        enabled=frozenset({"name"}),
        repo_prefixes=("alpha",),
        folded=frozenset(),
        visibility_repo_prefixes=("alpha",),
        show_empty_repos=False,
        hidden_repos=frozenset(),
    )
    kinds = _kinds(tframe._render_overlay(settings))
    assert {
        dhit.Hit("tab", ("fields",)),
        dhit.Hit("tab", ("repos",)),
        dhit.Hit("tab", ("visibility",)),
    } <= kinds
    assert {dhit.Hit("setting", (0,)), dhit.Hit("setting", (1,))} <= kinds


def test_egress_and_account_rows_are_tagged_by_index():
    from jailbee.dashboard import accounts as da
    from jailbee.egress_scope import EntryRow

    egress = EgressState(
        "alpha", None, (EntryRow("a.example", "local"), EntryRow("b.example", "local"))
    )
    assert {dhit.Hit("egress", (0,)), dhit.Hit("egress", (1,))} <= _kinds(
        tframe._render_overlay(egress)
    )
    rows = (da.AccountRow("claude", "team", "a", "live", (), ()),)
    assert dhit.Hit("account", (0,)) in _kinds(tframe._render_overlay(da.AccountsState(rows)))


def test_hover_segments_paints_only_the_matching_target():
    from rich.segment import Segment

    row = dhit.hit_style("row", "a")
    other = dhit.hit_style("row", "b")
    segment = Segment("x", row, control=("control",))
    out = dhit.hover_segments([segment, Segment("y", other), Segment("z")], dhit.Hit("row", ("a",)))

    assert out[0].style == row + dhit.HOVER_STYLE
    assert out[0].style.meta == row.meta
    assert out[0].control == segment.control
    assert out[1].style == other and out[2].style is None
    assert dhit.hover_segments([Segment("x", row)], None) == [Segment("x", row)]


def test_hover_paints_only_the_hovered_target(tmp_path):
    group = dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [ci("alpha-one", "alpha"), ci("alpha-two", "alpha")]
    )
    console = Console(width=120, file=io.StringIO(), color_system="truecolor")
    before = console.render_lines(_table([group]), pad=False)
    after = [dhit.hover_segments(line, dhit.Hit("row", ("alpha-two",))) for line in before]
    changed = [
        dhit.Hit.of(a.style.meta)
        for line_a, line_b in zip(after, before, strict=True)
        for a, b in zip(line_a, line_b, strict=True)
        if a.style != b.style
    ]
    assert changed and set(changed) == {dhit.Hit("row", ("alpha-two",))}
    assert all(
        a.style.bgcolor == dhit.HOVER_STYLE.bgcolor
        for line in after
        for a in line
        if a.style and dhit.Hit.of(a.style.meta) == dhit.Hit("row", ("alpha-two",))
    )


def test_hit_tags_do_not_change_the_rendered_text(mocker, tmp_path):
    """Meta is invisible: no escape sequence, no width change."""
    from rich.style import Style

    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    console = Console(width=100, file=io.StringIO(), color_system="truecolor", record=True)
    console.print(_table([group], width=100))
    tagged = console.export_text(styles=True)
    assert "\x1b]8" not in tagged
    mocker.patch.object(dhit, "hit_style", return_value=Style())
    console.print(_table([group], width=100))
    from rich.text import Text

    untagged = console.export_text(styles=True)
    assert "\x1b]8" not in untagged
    # Metadata can split adjacent SGR runs, but must not change visible cells.
    assert Text.from_ansi(untagged).plain == Text.from_ansi(tagged).plain
    assert Text.from_ansi(untagged).cell_len == Text.from_ansi(tagged).cell_len
