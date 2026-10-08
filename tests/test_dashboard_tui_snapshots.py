"""Stable whole-app SVGs, including native frame and real overlay composition."""

import os
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import session as tsession
from jailbee.db.view_prefs import ViewState
from tests.dashboard_fixtures import WIDE, ci, fake_accounts_cli, fake_branches
from tests.dashboard_pilot import FROZEN_NOW, make_app


@pytest.fixture(autouse=True)
def fixed_timezone():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Helsinki"
    time.tzset()
    yield
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


@pytest.fixture
def groups(mocker):
    mocker.patch.object(tsession, "_now", return_value=FROZEN_NOW)
    mocker.patch.object(tsession, "save_view_state")
    return [
        dmodel.RepoGroup(
            prefix,
            f"/repos/{prefix}",
            None,
            [
                replace(
                    ci(f"{prefix}-{name}", prefix, state=state, pr_number=4, mode="mount"),
                    created_at=FROZEN_NOW - timedelta(hours=2),
                )
                for name, state in (("one", "Running"), ("two", "Stopped"))
            ],
        )
        for prefix in ("alpha", "beta")
    ]


def test_snapshot_table(snap_compare, mocker, groups):
    app = make_app(mocker, groups, view_state=ViewState(folded=frozenset({"beta"})))
    assert snap_compare(app, terminal_size=(100, 30))


def test_snapshot_menu_details(snap_compare, mocker, groups):
    app = make_app(mocker, groups, view_state=ViewState(show_details=False))

    async def open_menu(pilot):
        await pilot.pause()
        await pilot.press("j", "v", "enter")
        await pilot.pause()
        assert app.session.show_details
        assert app.session.overlay is not None

    assert snap_compare(app, terminal_size=(120, 30), run_before=open_menu)


def test_snapshot_settings(snap_compare, mocker, groups):
    app = make_app(mocker, groups)

    async def open_settings(pilot):
        await pilot.press("S")
        await pilot.pause()
        assert app.session.overlay is not None

    assert snap_compare(app, terminal_size=(100, 30), run_before=open_settings)


def test_snapshot_accounts(snap_compare, mocker, groups):
    fake_accounts_cli(mocker)
    app = make_app(mocker, groups)

    async def open_accounts(pilot):
        await pilot.press("A")
        await pilot.pause()
        assert app.session.overlay is not None

    assert snap_compare(app, terminal_size=(100, 30), run_before=open_accounts)


def test_snapshot_narrow(snap_compare, mocker, groups):
    for group in groups:
        group.containers = [
            replace(info, name=f"{info.name}-long-feature-branch-name") for info in group.containers
        ]
    app = make_app(mocker, groups, view_state=ViewState(columns=WIDE))
    app.session.column_widths = dict.fromkeys(WIDE, 24)

    async def scroll(pilot):
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert app.session.column_offset == 1

    assert snap_compare(app, terminal_size=(60, 20), run_before=scroll)


def test_snapshot_command_line(snap_compare, mocker, groups):
    mocker.patch.object(
        tsession.DashboardSession, "command_candidates", return_value=("shell", "show")
    )
    app = make_app(mocker, groups)

    async def type_command(pilot):
        await pilot.pause()
        await pilot.press("exclamation_mark", "s", "h", "tab")
        await pilot.pause()
        assert app.frame.native_state().text == "shell"

    assert snap_compare(app, terminal_size=(100, 30), run_before=type_command)


def test_snapshot_choice_prompt(snap_compare, mocker):
    mocker.patch.object(tsession, "_now", return_value=FROZEN_NOW)
    mocker.patch.object(tsession, "save_view_state")
    mocker.patch.object(tsession, "host_branches", side_effect=fake_branches)
    info = replace(
        ci("alpha-x", "alpha"), base_branch="feat/a", created_at=FROZEN_NOW - timedelta(hours=2)
    )
    app = make_app(mocker, [dmodel.RepoGroup("alpha", "/repos/alpha", None, [info])])

    async def open_prompt(pilot):
        await pilot.pause()
        await pilot.press("j", "enter", "g", "b", "down")
        await pilot.pause()
        assert app.frame.native_state().cursor == 0

    assert snap_compare(app, terminal_size=(100, 30), run_before=open_prompt)
