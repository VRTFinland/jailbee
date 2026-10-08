"""FleetTable's standalone line rendering and delivered input policy."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from textual import events
from textual.app import App, ComposeResult

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.hit import Hit
from jailbee.dashboard.tui import fleet
from jailbee.dashboard.tui.widgets import FleetTable
from tests.dashboard_fixtures import ci

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)
Row = dmodel.Row


def _model(n=50, *, now=NOW, age=False, folded=frozenset()):  # type: ignore[no-untyped-def]
    containers = [
        replace(ci(f"alpha-{i:03}", "alpha"), created_at=NOW - timedelta(seconds=59))
        for i in range(n)
    ]
    return fleet.table_model(
        [dmodel.RepoGroup("alpha", "/r", None, containers)] if n else [],
        now=now,
        enabled=("name", "created") if age else ("name",),
        folded=folded,
        column_widths={"name": 20, "created": 8},
        shown_columns=None,
        column_offset=0,
        hidden_by_preferences=False,
        width=60,
    )


class _Counting(FleetTable):
    def __init__(self):  # type: ignore[no-untyped-def]
        super().__init__(id="fleet")
        self.lines: list[int] = []

    def render_line(self, y):  # type: ignore[no-untyped-def]
        self.lines.append(y)
        return super().render_line(y)


class _Host(App[None]):
    def __init__(self):  # type: ignore[no-untyped-def]
        super().__init__(ansi_color=True)
        self.columns: list[int] = []

    def compose(self) -> ComposeResult:
        table = _Counting()
        table.styles.height = 10
        yield table

    def on_fleet_table_column_scroll(self, message: FleetTable.ColumnScroll) -> None:
        self.columns.append(message.step)


def _run(script):  # type: ignore[no-untyped-def]
    async def main():  # type: ignore[no-untyped-def]
        app = _Host()
        async with app.run_test(size=(64, 20)) as pilot:
            await script(app, app.query_one(_Counting), pilot)

    asyncio.run(main())


def _screen(app):  # type: ignore[no-untyped-def]
    return [s.text for s in app.screen._compositor.render_strips()]


def test_frozen_header_scrolled_hits_and_incremental_hover():
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        model = _model()
        table.show(model, None, None)
        await pilot.pause()
        table.scroll_to(y=10, animate=False)
        await pilot.pause()
        assert "NAME" in _screen(app)[0]
        assert "009" in _screen(app)[1]
        assert Hit.of(app.screen.get_style_at(4, 3).meta) == Hit("row", ("alpha-011",))
        table.show(model, None, Hit("row", ("alpha-011",)))
        await pilot.pause()
        table.lines.clear()
        table.show(model, None, Hit("row", ("alpha-012",)))
        await pilot.pause()
        assert sorted(set(table.lines)) == [3, 4]

    _run(script)


@pytest.mark.parametrize("age", [False, True])
def test_clock_alone_and_unchanged_selection_hover_repaint_nothing(age):
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        selected = Row("container", "alpha-001")
        hover = Hit("row", ("alpha-002",))
        table.show(_model(age=age), selected, hover)
        await pilot.pause()
        table.lines.clear()
        latest = _model(now=NOW + timedelta(milliseconds=100), age=age)
        table.show(latest, selected, hover)
        await pilot.pause()
        assert table.lines == []
        assert table._model is latest

    _run(script)


def test_age_boundary_updates_cells_and_cached_strip():
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        table.show(_model(2, age=True), None, None)
        await pilot.pause()
        before = _screen(app)[2]
        table.lines.clear()
        table.show(_model(2, now=NOW + timedelta(seconds=2), age=True), None, None)
        await pilot.pause()
        assert _screen(app)[2] != before
        assert sorted(set(table.lines)) == [2, 3]

    _run(script)


def test_header_hover_is_invalidated_at_screen_zero_when_scrolled():
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        model = fleet.table_model(
            [_model().entries[0].group],
            now=NOW,
            enabled=("name", "state", "created"),
            folded=frozenset(),
            column_widths={"name": 10, "state": 10, "created": 10},
            shown_columns=("name", "state", "created"),
            column_offset=0,
            hidden_by_preferences=False,
            width=32,
        )
        table.show(model, None, None)
        await pilot.pause()
        table.scroll_to(y=10, animate=False)
        await pilot.pause()
        table.lines.clear()
        table.show(model, None, Hit("scroll", (1,)))
        await pilot.pause()
        assert sorted(set(table.lines)) == [0]
        table.lines.clear()
        table.show(model, None, Hit("scroll", (1,)))
        await pilot.pause()
        assert table.lines == []

    _run(script)


def test_selection_repaints_only_changed_rows():
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        model = _model()
        table.show(model, Row("container", "alpha-001"), None)
        await pilot.pause()
        table.lines.clear()
        table.show(model, Row("container", "alpha-002"), None)
        await pilot.pause()
        assert sorted(set(table.lines)) == [3, 4]

    _run(script)


def test_wheel_same_selection_and_ctrl_vertical_shift_horizontal():
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        selected = Row("container", "alpha-000")
        table.show(_model(), selected, None)
        await pilot.pause()
        for _ in range(5):
            await pilot._post_mouse_events(
                [events.MouseScrollDown], widget=table, offset=(1, 1), control=True
            )
        await pilot.pause()
        assert table.scroll_y == 5
        table.show(_model(now=NOW + timedelta(seconds=1)), selected, None)
        await pilot.pause()
        assert table.scroll_y == 5
        await pilot._post_mouse_events(
            [events.MouseScrollDown], widget=table, offset=(1, 1), shift=True
        )
        await pilot._post_mouse_events([events.MouseScrollLeft], widget=table, offset=(1, 1))
        await pilot.pause()
        assert app.columns == [1, -1] and table.scroll_y == 5
        await pilot.press("down", "pagedown")
        assert table.scroll_y == 5 and app.focused is None

    _run(script)


def test_mouse_policy_blocks_wheel_and_scrollbar_but_not_programmatic_scroll():
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        table.show(_model(), None, None)
        await pilot.pause()
        table.mouse_enabled = lambda: False
        await pilot._post_mouse_events([events.MouseScrollDown], widget=table, offset=(1, 1))
        await pilot._post_mouse_events([events.MouseScrollRight], widget=table, offset=(1, 1))
        await pilot.click(table.vertical_scrollbar, offset=(0, 8))
        await pilot.pause()
        assert table.scroll_y == 0 and app.columns == []
        table.scroll_to(y=5, animate=False)
        await pilot.pause()
        assert table.scroll_y == 5

    _run(script)


def test_width_and_height_changes_redraw_and_reveal_selection():
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        table.show(_model(), Row("container", "alpha-040"), None)
        await pilot.pause()
        table.scroll_to(y=0, animate=False)
        await pilot.pause()
        table.styles.height = 6
        await pilot.resize_terminal(40, 20)
        await pilot.pause()
        assert any("040" in line for line in _screen(app)[:6])
        assert table.content_width == 39

    _run(script)


def test_short_to_long_reveals_new_selection_after_layout():
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        table.show(_model(3), None, None)
        await pilot.pause()
        assert not table.show_vertical_scrollbar
        table.show(_model(50), Row("container", "alpha-049"), None)
        await pilot.pause()
        assert table.scroll_y > 0
        assert any("049" in line for line in _screen(app)[:10])

    _run(script)


def test_updates_only_build_rich_lines_for_changed_visible_rows(mocker):
    from jailbee.dashboard.tui import widgets

    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        table.show(_model(1000), None, None)
        await pilot.pause()
        build = mocker.spy(widgets, "entry_line")
        table.show(_model(1000, now=NOW + timedelta(seconds=1)), None, None)
        await pilot.pause()
        assert build.call_count == 0
        table.show(_model(1000), None, Hit("row", ("alpha-001",)))
        await pilot.pause()
        assert build.call_count == 1
        build.reset_mock()
        table.show(_model(1000), Row("container", "alpha-002"), Hit("row", ("alpha-001",)))
        await pilot.pause()
        assert build.call_count == 1

    _run(script)


def test_scrollbar_removal_rebuilds_heading_at_current_width():
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        def long_heading(n):  # type: ignore[no-untyped-def]
            group = replace(_model(n).entries[0].group, prefix="a" * 70)
            return fleet.table_model(
                [group],
                now=NOW,
                enabled=("name",),
                folded=frozenset(),
                column_widths=None,
                shown_columns=None,
                column_offset=0,
                hidden_by_preferences=False,
                width=60,
            )

        table.show(long_heading(50), None, None)
        await pilot.pause()
        assert table.content_width == 63
        table.show(long_heading(3), None, None)
        await pilot.pause()
        assert table.content_width == 64
        assert _screen(app)[1][63] == "…"
        assert _screen(app)[1][62] == "a"

    _run(script)


def test_delivered_scrollbar_drag_is_blocked_by_mouse_policy():
    from textual.scrollbar import ScrollTo

    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        table.show(_model(), None, None)
        await pilot.pause()
        table.mouse_enabled = lambda: False
        table.post_message(ScrollTo(y=20, animate=False))
        await pilot.pause()
        assert table.scroll_y == 0

    _run(script)


def test_large_table_visible_only_selection_and_structural_changes():
    async def script(app, table, pilot):  # type: ignore[no-untyped-def]
        table.lines.clear()
        table.show(_model(1000), None, None)
        await pilot.pause()
        assert 0 < len(table.lines) <= 30
        assert table.show_vertical_scrollbar
        table.show(_model(), Row("container", "alpha-040"), None)
        await pilot.pause()
        assert any("040" in line for line in _screen(app)[:10])
        table.show(_model(3), None, None)
        await pilot.pause()
        assert not table.show_vertical_scrollbar
        table.show(_model(3, folded=frozenset({"alpha"})), None, None)
        await pilot.pause()
        assert "NAME" not in _screen(app)[0] and "alpha" in _screen(app)[0]
        table.show(_model(0), None, None)
        await pilot.pause()
        assert fleet.EMPTY_TEXT in _screen(app)[0]

    _run(script)
