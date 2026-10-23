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


def _outbox_alone_state(**over):
    from jailbee.dashboard import outbox as dob
    from tests.test_dashboard_outbox import _action, _key, _proposal, _state

    first = _proposal(
        "pr/a.json",
        actions=[_action(0, title="[red]x[/red]", body="hello"), _action(1, body="second")],
    )
    return _state(
        first,
        _proposal("issue/b.json", actions=[_action(0, kind="create", body="an issue")]),
        abilities=dob.Abilities(True, True, False, True, True, True),
        expanded=frozenset({_key("pr/a.json")}),
        marks=frozenset({_key("issue/b.json")}),
        status=("Nothing deleted.",),
        **over,
    )


def _outbox_alone(spec):
    from textual.app import App

    from jailbee.dashboard.tui.outbox_box import OutboxBox

    class Alone(App[None]):
        CSS = "Screen { background: ansi_default; }"

        def compose(self):
            yield OutboxBox(spec, mouse_enabled=lambda: True, standalone=True)

    return Alone(ansi_color=True)


def test_snapshot_outbox_box_alone(snap_compare):
    assert snap_compare(_outbox_alone(_outbox_alone_state()), terminal_size=(100, 30))


def test_snapshot_outbox_box_alone_question(snap_compare):
    from jailbee.dashboard import outbox as dob

    question = dob.Question(
        "delete",
        "Delete from the outbox?",
        ("pr/a.json (whole)", "[red]x[/red] stays literal"),
        (("No", "no"), ("Yes, delete", "yes")),
        default="no",
    )
    spec = _outbox_alone_state(question=question)
    assert snap_compare(_outbox_alone(spec), terminal_size=(100, 30))


def _outbox_in_dashboard(mocker, groups):
    from tests.test_dashboard_outbox import _action, _container, _listing, _proposal

    listing = _listing(
        _container(
            _proposal(
                "pr/a.json",
                actions=[_action(0, title="Fix the [red]x[/red] parser", body="hello"), _action(1)],
            ),
            _proposal("issue/b.json", actions=[_action(0, kind="create", title="An issue")]),
            name="alpha-one",
        )
    )
    mocker.patch.object(
        tsession.da, "run_cli_quiet", return_value=tsession.da.CliResult(True, "done", listing)
    )
    app = make_app(mocker, groups, view_state=ViewState(folded=frozenset({"beta"})))

    async def open_outbox(pilot):
        await pilot.pause()
        await pilot.press("j", "enter", "o")
        await pilot.pause()
        await pilot.press("enter")  # expand the first proposal
        await pilot.pause()
        assert isinstance(app.session.overlay, tsession.OutboxState)

    return app, open_outbox


def test_snapshot_outbox_box_wide(snap_compare, mocker, groups):
    app, open_outbox = _outbox_in_dashboard(mocker, groups)
    assert snap_compare(app, terminal_size=(120, 34), run_before=open_outbox)


def test_snapshot_outbox_box_narrow(snap_compare, mocker, groups):
    app, open_outbox = _outbox_in_dashboard(mocker, groups)
    assert snap_compare(app, terminal_size=(60, 20), run_before=open_outbox)
