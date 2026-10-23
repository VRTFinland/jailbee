"""The details panel's height: chosen with keys, kept in the stored view state."""

from __future__ import annotations

import pytest

from jailbee.dashboard.tui import keys as tkeys
from jailbee.dashboard.tui.layout import MIN_DETAILS_ROWS
from jailbee.db.view_prefs import ViewState
from tests.dashboard_fixtures import named_rows_group
from tests.dashboard_pilot import drive


def test_plus_minus_and_equals_parse_to_the_height_keys():
    assert tkeys.parse_key("plus") == "details-grow"
    assert tkeys.parse_key("minus") == "details-shrink"
    assert tkeys.parse_key("equals_sign") == "details-reset"


@pytest.fixture
def run_keys(mocker, tmp_path):
    """Drive the dashboard on a tall screen with the first container selected."""

    def go(keys, *, view_state=None):
        shown: list[tuple[int | None, int]] = []

        def note(app):
            shown.append((app.session.details_rows, app.session.details_shown))

        steps = ["j", note]
        for key in keys:
            steps += [key, note]
        run = drive(
            mocker,
            steps,
            [named_rows_group(tmp_path, 3)],
            view_state=view_state or ViewState(show_details=True),
            size=(100, 50),
        )
        run.shown = shown
        return run

    return go


def test_grow_adds_a_row_to_what_is_shown_and_remembers_it(run_keys):
    run = run_keys(["plus"])
    (_, before), (pref, after) = run.shown[0], run.shown[1]

    assert pref == after == before + 1


def test_shrink_takes_a_row_and_stops_at_the_minimum(run_keys):
    run = run_keys(["minus"] * 20)

    assert run.shown[-1] == (MIN_DETAILS_ROWS, MIN_DETAILS_ROWS)


def test_grow_stops_at_the_room_the_table_leaves(run_keys):
    run = run_keys(["plus"] * 60)

    assert run.shown[-1][1] == run.shown[-2][1] < 50  # settled well inside the frame
    assert run.shown[-1][0] == run.shown[-1][1]  # the stored value is what is shown


def test_equals_returns_to_the_built_in_height(run_keys):
    run = run_keys(["plus", "plus", "equals_sign"])

    assert run.shown[-1][0] is None
    assert run.shown[-1][1] == run.shown[0][1]


def test_the_chosen_height_is_saved_with_the_view(mocker, tmp_path):
    from jailbee.dashboard.tui import session as tsession

    saved = mocker.patch.object(tsession, "save_view_state")
    drive(
        mocker,
        ["j", "plus"],
        [named_rows_group(tmp_path, 3)],
        view_state=ViewState(show_details=True),
        size=(100, 50),
    )

    assert saved.call_args_list[0].args[2].details_rows is not None


def test_a_stored_height_is_used_on_the_next_start(mocker, tmp_path):
    rows: list[int] = []
    drive(
        mocker,
        ["j", lambda app: rows.append(app.session.details_shown)],
        [named_rows_group(tmp_path, 3)],
        view_state=ViewState(show_details=True, details_rows=5),
        size=(100, 50),
    )

    assert rows == [5]


def test_the_height_keys_do_nothing_while_the_panel_is_hidden(mocker, tmp_path):
    run = drive(
        mocker,
        ["j", "plus"],
        [named_rows_group(tmp_path, 3)],
        view_state=ViewState(show_details=False),
        size=(100, 50),
    )

    assert run.app.session.details_rows is None


def _drag_app(mocker, tmp_path):
    from tests.dashboard_pilot import make_app

    return make_app(
        mocker,
        [named_rows_group(tmp_path, 3)],
        view_state=ViewState(show_details=True),
    )


def test_dragging_the_panels_top_border_up_makes_it_taller(mocker, tmp_path):
    import asyncio

    from jailbee.dashboard.tui.widgets import DetailsPanel

    app = _drag_app(mocker, tmp_path)
    seen: dict[str, int | None] = {}

    async def go():
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            panel = app.query_one(DetailsPanel)
            seen["before"] = app.session.details_shown
            top = panel.region.y  # screen rows: the panel moves while it is dragged
            await pilot.mouse_down(panel, offset=(5, 0))
            await pilot.hover(offset=(5, top - 3))
            await pilot.pause()
            await pilot.mouse_up(offset=(5, top - 3))
            await pilot.pause()
            seen["after"] = app.session.details_shown
            seen["pref"] = app.session.details_rows

    asyncio.run(go())

    assert seen["after"] == seen["before"] + 3
    assert seen["pref"] == seen["after"]


def test_dragging_inside_the_panel_does_not_resize_it(mocker, tmp_path):
    import asyncio

    from jailbee.dashboard.tui.widgets import DetailsPanel

    app = _drag_app(mocker, tmp_path)
    seen: dict[str, int | None] = {}

    async def go():
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            panel = app.query_one(DetailsPanel)
            await pilot.mouse_down(panel, offset=(5, 2))
            await pilot.hover(panel, offset=(5, -1))
            await pilot.mouse_up(panel, offset=(5, -1))
            await pilot.pause()
            seen["pref"] = app.session.details_rows

    asyncio.run(go())

    assert seen["pref"] is None


def test_dragging_the_top_border_down_makes_it_shorter(mocker, tmp_path):
    import asyncio

    from jailbee.dashboard.tui.widgets import DetailsPanel

    app = _drag_app(mocker, tmp_path)
    seen: dict[str, int | None] = {}

    async def go():
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            panel = app.query_one(DetailsPanel)
            seen["before"] = app.session.details_shown
            top = panel.region.y
            await pilot.mouse_down(panel, offset=(5, 0))
            await pilot.hover(offset=(5, top + 2))
            await pilot.pause()
            await pilot.mouse_up(offset=(5, top + 2))
            await pilot.pause()
            seen["after"] = app.session.details_shown

    asyncio.run(go())

    assert seen["after"] == seen["before"] - 2
