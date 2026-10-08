"""Native frame screen and integration contracts."""

from __future__ import annotations

import pytest
from rich.cells import cell_len

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.menu_state import MenuState
from tests.dashboard_fixtures import WIDE, ci, wide_group
from tests.dashboard_pilot import Wheel, drive, paint, view_of

Row = dmodel.Row


def _long(tmp_path, n=40):
    return dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [ci(f"row{i:02}", "alpha") for i in range(n)]
    )


def test_title_table_and_bottom_border(tmp_path):
    lines = paint(view_of([_long(tmp_path, 3)]))
    assert lines[0].startswith("╭─ 🐝 jailbee dashboard")
    assert "NAME" in lines[1] and "▾ alpha" in lines[2]
    assert lines[-1].startswith("╰")


def test_a_long_table_fills_the_screen_and_keeps_the_header(tmp_path):
    lines = paint(view_of([_long(tmp_path)], selected=Row("container", "row35")), size=(80, 20))
    assert len(lines) == 20 and "NAME" in lines[1]
    assert any("row35" in line for line in lines)


def test_a_menu_sits_beside_the_details(tmp_path):
    menu = MenuState("row01", [("Attach tmux", "tmux")])
    lines = paint(
        view_of(
            [_long(tmp_path, 3)],
            selected=Row("container", "row01"),
            show_details=True,
            overlay=menu,
        ),
        size=(120, 30),
    )
    both = [line for line in lines if "╭─ row01" in line]
    assert both and both[0].index("╭─ row01 ") < both[0].index("row01 →")


@pytest.mark.parametrize("height", [6, 8, 10])
def test_a_tiny_terminal_keeps_the_hint_and_the_bottom_border(tmp_path, height):
    menu = MenuState("row01", [(f"Entry {i}", f"v{i}") for i in range(30)])
    lines = paint(
        view_of([_long(tmp_path)], selected=Row("container", "row01"), overlay=menu),
        size=(80, height),
    )
    assert len(lines) <= height
    assert lines[-1].startswith("╰") and "Esc" in lines[-2]


def test_down_moves_the_selection_not_the_view(mocker, tmp_path):
    run = drive(mocker, ["down", "down"], [_long(tmp_path)], size=(80, 20))
    assert run.trace[2].selected == Row("container", "row01")
    assert run.app.frame.table.scroll_y == 0


def test_wheel_scrolls_the_view_and_a_tick_keeps_it(mocker, tmp_path):
    run = drive(mocker, [Wheel(1)] * 5 + [lambda app: None], [_long(tmp_path)], size=(80, 20))
    assert run.app.frame.table.scroll_y == 5
    assert run.last.selected == run.trace[0].selected


def test_fixed_coordinate_click_after_wheel_selects_the_drawn_row(mocker, tmp_path):
    from textual import events

    def click(app):
        # Screen y=4 is the third body line, after the frozen header and heading.
        style = app.screen.get_style_at(5, 4)
        app.post_message(
            events.Click(app.frame.table, 5, 4, 0, 0, 1, False, False, False, style=style)
        )

    run = drive(mocker, [Wheel(1)] * 10 + [click], [_long(tmp_path)], size=(80, 20), screens=True)
    assert "row11" in run.screens[10].splitlines()[4]
    assert run.trace[11].selected == Row("container", "row11")


def test_wheel_over_the_open_menu_moves_its_cursor(mocker, tmp_path):
    run = drive(mocker, ["j", "enter", Wheel(1, at="#overlay")], [_long(tmp_path, 3)])
    assert isinstance(run.trace[3].overlay, MenuState) and run.trace[3].overlay.index == 1
    assert run.trace[3].selected == run.trace[2].selected


def test_right_reaches_literal_last_header_with_scrollbar(mocker, tmp_path):
    group = wide_group(tmp_path)
    group.containers.extend(ci(f"x{i}", "alpha") for i in range(40))
    widths = []
    run = drive(
        mocker,
        ["right"] * 12
        + [lambda app: widths.append((app.table_width, app.frame.table.content_width))],
        [group],
        view_state=tsession.ViewState(columns=WIDE),
        size=(44, 12),
        screens=True,
    )
    table = run.app.frame.table
    assert table.show_vertical_scrollbar
    assert widths == [(39, 39)]
    header = run.screens[-1].splitlines()[1]
    # Canonical field order ends in PR; the literal mark is the rendered scroll cue.
    assert "PR" in header and "›" not in header  # noqa: RUF001
    assert all(cell_len(line) <= 44 for line in run.screens[-1].splitlines())


def test_width42_help_cue_and_unicode_cell_widths(tmp_path):
    lines = paint(view_of([_long(tmp_path, 3)]), size=(42, 12))
    assert "h/? help" in lines[0]
    assert all(cell_len(line) == 42 for line in lines)


def test_delivered_mouse_off_and_outside_wheels_do_nothing(mocker, tmp_path):
    run = drive(
        mocker,
        [Wheel(1, at="#frame"), "m", Wheel(1), Wheel(1, shift=True)],
        [_long(tmp_path)],
        size=(44, 12),
    )
    assert run.app.frame.table.scroll_y == 0
    assert run.last.selected == run.trace[0].selected and run.last.column_offset == 0


def test_advancing_clock_repaints_no_static_body(mocker, tmp_path):
    from datetime import timedelta

    from textual.widgets import Static

    from jailbee.dashboard.tui import widgets
    from tests.dashboard_pilot import FROZEN_NOW

    now = [FROZEN_NOW]
    mocker.patch.object(tsession, "_now", side_effect=lambda: now[0])
    updates = mocker.spy(Static, "update")
    lines = mocker.spy(widgets.FleetTable, "render_line")
    counts = []

    def advance(app):
        counts.append((updates.call_count, lines.call_count))
        now[0] += timedelta(seconds=1)

    drive(
        mocker,
        [advance, advance, lambda app: counts.append((updates.call_count, lines.call_count))],
        [_long(tmp_path, 3)],
    )
    assert counts[1:] == [counts[0], counts[0]]


def test_notice_crop_retains_remedy_suffix(tmp_path):
    notice = "explanation " * 100 + "FINAL REMEDY"
    lines = paint(view_of([_long(tmp_path)], notice=notice), size=(42, 6))
    assert "FINAL REMEDY" in "\n".join(lines)
    assert lines[-1].startswith("╰")


def test_overlay_sideways_ignored_and_ctrl_table_wheel_vertical(mocker, tmp_path):
    run = drive(
        mocker,
        [
            Wheel(1, ctrl=True),
            "j",
            "enter",
            Wheel(1, shift=True, at="#overlay"),
            Wheel(1, horizontal=True, at="#overlay"),
            Wheel(1, shift=True),
        ],
        [_long(tmp_path)],
        size=(80, 20),
    )
    assert run.trace[1].selected == run.trace[0].selected
    assert (
        run.trace[3].overlay == run.trace[4].overlay == run.trace[5].overlay == run.trace[6].overlay
    )
    assert all(v.column_offset == 0 for v in run.trace)
