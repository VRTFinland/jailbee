"""Native frame screen and integration contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest
from rich.cells import cell_len

from jailbee.accounts.models import ActivityEvent, AgentActivity
from jailbee.agent_status import AgentSummary
from jailbee.dashboard import details as dd
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.menu_state import MenuState
from jailbee.dashboard.tui.overlay import NativeState
from jailbee.db.view_prefs import ViewState
from tests.dashboard_fixtures import WIDE, ci, named_rows_group, wide_group
from tests.dashboard_pilot import NATIVE_LIST, Resize, Wheel, drive, paint, view_of

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
    assert len(lines) == 25


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
    assert len(lines) == height
    assert any(line.startswith("│ ╰") for line in lines[:-2])
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


def test_wheel_over_the_open_menu_scrolls_it_and_never_moves_its_cursor(mocker, tmp_path):
    ys = []
    run = drive(
        mocker,
        [
            "j",
            "enter",
            Wheel(1, at=NATIVE_LIST),
            lambda app: ys.append(int(app.query_one(NATIVE_LIST).scroll_y)),
        ],
        [_long(tmp_path, 3)],
        size=(80, 12),
    )
    assert ys == [1]  # one line per notch, in a menu taller than its box
    assert run.natives[3] == NativeState("menu", 0, level=None)
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

    run = drive(
        mocker,
        [advance, advance, lambda app: counts.append((updates.call_count, lines.call_count))],
        [_long(tmp_path, 3)],
        size=(120, 25),
        screens=True,
    )
    assert "12:00:05" in run.screens[0].splitlines()[0]
    assert "12:00:06" in run.screens[1].splitlines()[0]
    assert "12:00:07" in run.screens[2].splitlines()[0]
    assert counts[1:] == [counts[0], counts[0]]


def test_notice_crop_retains_remedy_suffix(tmp_path):
    notice = "explanation " * 100 + "FINAL REMEDY"
    lines = paint(view_of([_long(tmp_path)], notice=notice), size=(42, 6))
    assert "FINAL REMEDY" in "\n".join(lines)
    assert lines[-1].startswith("╰")


def test_overlay_sideways_ignored_and_ctrl_table_wheel_vertical(mocker, tmp_path):
    group = wide_group(tmp_path)
    group.containers.extend(ci(f"x{i}", "alpha") for i in range(40))
    positions = []
    run = drive(
        mocker,
        [
            Wheel(1, ctrl=True),
            lambda app: positions.append(app.frame.table.scroll_y),
            "j",
            "enter",
            Wheel(1, shift=True, at=NATIVE_LIST),
            Wheel(1, horizontal=True, at=NATIVE_LIST),
            Wheel(1, shift=True),
            "escape",
            Wheel(1, shift=True),
        ],
        [group],
        view_state=tsession.ViewState(columns=WIDE),
        size=(44, 20),
    )
    assert positions == [1]
    assert run.trace[1].selected == run.trace[0].selected
    assert (
        run.trace[4].overlay == run.trace[5].overlay == run.trace[6].overlay == run.trace[7].overlay
    )
    assert all(v.column_offset == 0 for v in run.trace[:9])
    assert run.trace[9].column_offset == 1  # same overflowing fleet scrolls once overlay closes


def _busy(tmp_path, n=3, *, events=20, message="the last message", busy=None):
    """``n`` containers; those named in ``busy`` (default: all) carry 20 recent events."""
    recent = tuple(
        ActivityEvent("tool" if i % 2 else "message", f"event{i:02d}") for i in range(events)
    )
    summary = AgentSummary(
        "claude",
        "busy",
        None,
        None,
        1,
        activity=AgentActivity("Bash  ls", message, recent=recent),
    )
    group = named_rows_group(tmp_path, n)
    return replace(
        group,
        containers=[
            replace(c, agent_status=(summary,)) if busy is None or c.name in busy else c
            for c in group.containers
        ],
    )


def _panel(lines, title="╭─ row01"):
    top = next(i for i, ln in enumerate(lines) if title in ln)
    bottom = next(i for i in range(top + 1, len(lines)) if "╰" in lines[i])
    return top, bottom


def test_the_frame_fills_the_terminal_and_the_details_show_the_whole_history(tmp_path):
    view = view_of([_busy(tmp_path)], selected=Row("container", "alpha-row01"), show_details=True)
    lines = paint(view, size=(120, 45))

    assert len(lines) == 45 and lines[-1].startswith("╰")
    top, bottom = _panel(lines)
    assert bottom == 43  # the panel's border sits on the frame's last content row
    assert bottom - top - 1 == dd.DETAILS_MAX_ROWS + 2 + 1 + 18  # grid, head, message, history
    text = "\n".join(lines)
    assert text.index("event17") < text.index("“event16”") < text.index("“event00”")
    assert lines[top - 1].strip("│ ") == ""  # the slack is blank space above the panel


def test_history_that_does_not_fit_is_cut_with_an_ellipsis_row(tmp_path):
    view = view_of([_busy(tmp_path)], selected=Row("container", "alpha-row01"), show_details=True)
    lines = paint(view, size=(120, 30))

    assert len(lines) == 30 and lines[-1].startswith("╰")
    top, bottom = _panel(lines)
    content = [ln.strip("│ ") for ln in lines[top + 1 : bottom]]
    assert content[-1] == "…" and content[-2] == "“event10”"  # even events are messages
    assert "event09" not in "\n".join(lines)


def test_without_activity_the_panel_keeps_its_size_at_the_bottom(tmp_path):
    view = view_of(
        [named_rows_group(tmp_path, 3)],
        selected=Row("container", "alpha-row01"),
        show_details=True,
    )
    lines = paint(view, size=(100, 40))

    assert len(lines) == 40
    top, bottom = _panel(lines)
    assert bottom == 38 and bottom - top - 1 == dd.DETAILS_MAX_ROWS
    assert all(ln.strip("│ ") == "" for ln in lines[7:top])  # blank between table and panel


def test_a_long_table_keeps_its_height_as_the_cursor_moves(tmp_path):
    group = _busy(tmp_path, 40, busy={"alpha-row01"})
    heights = []
    for selected in (
        Row("container", "alpha-row01"),
        Row("container", "alpha-row02"),
        Row("repo", "alpha"),
    ):
        lines = paint(view_of([group], selected=selected, show_details=True), size=(120, 40))
        heights.append(next(i for i, ln in enumerate(lines) if i and "╭" in ln))
    assert heights[0] == heights[1] == heights[2]


def test_a_huge_message_on_a_small_terminal_stays_inside_the_frame(tmp_path):
    view = view_of(
        [_busy(tmp_path, message="m" * 1000)],
        selected=Row("container", "alpha-row01"),
        show_details=True,
    )
    lines = paint(view, size=(80, 24))

    assert len(lines) == 24 and lines[-1].startswith("╰")
    _, bottom = _panel(lines)
    assert lines[bottom - 1].rstrip(" │").endswith("…")


def test_a_picker_still_sits_under_the_table(tmp_path):
    picker = tsession.Picker("x", "Pick one", (tsession.PickerEntry("Entry 0", "0"),))
    lines = paint(
        view_of(
            [named_rows_group(tmp_path, 3)],
            selected=Row("container", "alpha-row01"),
            overlay=picker,
        ),
        size=(100, 40),
    )

    assert len(lines) == 40 and lines[-1].startswith("╰")
    assert next(i for i, ln in enumerate(lines) if "Pick one" in ln) < 12


def test_resizing_keeps_the_frame_full_height(mocker, tmp_path):
    run = drive(
        mocker,
        ["j", Resize(200, 60), Resize(80, 24), Resize(120, 40)],
        [_busy(tmp_path)],
        view_state=ViewState(show_details=True),
        size=(120, 40),
        screens=True,
    )
    for screen, height in zip(run.screens[1:5], (40, 60, 24, 40), strict=True):
        lines = screen.splitlines()
        assert len(lines) == height and lines[-1].startswith("╰"), height
    assert "event00" in run.screens[2]  # 200x60: the whole history fits
    assert "event00" not in run.screens[3]  # 80x24: it does not


@pytest.mark.parametrize("height", [14, 15, 16, 17, 18, 19, 20])
def test_a_full_frame_keeps_the_panels_bottom_border(tmp_path, height):
    """A 1fr filler is never smaller than a row: none may be drawn when nothing is spare."""
    view = view_of(
        [named_rows_group(tmp_path, 3)], selected=Row("container", "alpha-row01"), show_details=True
    )
    lines = paint(view, size=(60, height))

    assert len(lines) == height and lines[-1].startswith("╰")
    assert lines[-2].startswith("│ ╰")


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("height", range(13, 26))
def test_a_full_frame_with_a_menu_beside_the_details_keeps_panel_and_hint(tmp_path, height, busy):
    """The hint's lines count against the frame: no filler may be drawn when it is full."""
    group = _busy(tmp_path, 40) if busy else _long(tmp_path)
    name = "alpha-row01" if busy else "row01"
    view = view_of(
        [group],
        selected=Row("container", name),
        show_details=True,
        overlay=MenuState(name, [("Attach tmux", "tmux")]),
    )
    lines = paint(view, size=(120, height))

    assert len(lines) == height and lines[-1].startswith("╰")
    assert lines[-3].startswith("│ ╰")  # the panel's bottom border
    assert "Esc" in lines[-2]  # the hint
