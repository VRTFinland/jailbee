"""The terminal dashboard sorts each repo group's rows and remembers how."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import BareClient, BareTerminal
from jailbee.dashboard import hit as dhit
from jailbee.dashboard.model import RepoGroup, Row
from jailbee.dashboard.sorting import DEFAULT_SORT, SortSpec
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.keys import parse_key
from jailbee.db.view_prefs import ViewState

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _group() -> RepoGroup:
    old = dataclasses.replace(ci("p-old", "p", state="Stopped"), created_at=T0 - timedelta(days=1))
    new = dataclasses.replace(ci("p-new", "p"), created_at=T0)
    return RepoGroup("p", "/repos/p", None, [new, old])


def _names(session) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.name for g in session.groups for c in g.containers]


def _session_with(mocker, view_state: ViewState):  # type: ignore[no-untyped-def]
    save = mocker.patch.object(tsession, "save_view_state")
    startup = tsession.Startup(mocker.Mock(), BareClient([_group()]), view_state, None)  # type: ignore[arg-type]  # duck-typed client
    session = tsession.DashboardSession(
        startup, incus=mocker.Mock(), cwd_root=None, terminal=BareTerminal()
    )
    session.tick()
    return session, save


def test_keys_parse():
    assert parse_key("less_than_sign") == "sort-prev"
    assert parse_key("greater_than_sign") == "sort-next"
    assert parse_key("I") == "sort-invert"


def test_a_stored_sort_applies_from_the_first_frame(mocker):
    session, _ = _session_with(
        mocker, ViewState(columns=("name", "state"), sort_field="state", sort_desc=True)
    )
    assert _names(session) == ["p-old", "p-new"]
    assert session.view().sort == SortSpec("state", True)


def test_sort_keys_move_and_invert_and_persist(mocker):
    session, save = _session_with(mocker, ViewState(columns=("name", "state")))
    assert _names(session) == ["p-new", "p-old"]
    session.handle_key("sort-next")  # name ▲
    assert session.sort == SortSpec("name", False)
    assert _names(session) == ["p-new", "p-old"]
    session.handle_key("sort-invert")  # name ▼
    assert _names(session) == ["p-old", "p-new"]
    session.handle_key("sort-prev")  # back to the default stop
    assert session.sort == DEFAULT_SORT
    assert _names(session) == ["p-new", "p-old"]
    saved = save.call_args.args[2]
    assert (saved.sort_field, saved.sort_desc) == (None, False)
    assert "newest first" in (session.notice or "")


def test_a_header_click_sorts_and_a_second_click_flips(mocker):
    session, save = _session_with(mocker, ViewState(columns=("name", "state")))
    session.click(dhit.Hit("sort", ("state",)))
    assert session.sort == SortSpec("state", False)
    session.click(dhit.Hit("sort", ("state",)))
    assert session.sort == SortSpec("state", True)
    assert save.call_args.args[2].sort_field == "state"


def test_cursor_follows_its_container_across_a_resort(mocker):
    session, _ = _session_with(mocker, ViewState(columns=("name", "state")))
    session.select(Row("container", "p-old"))
    session.handle_key("sort-next")
    session.handle_key("sort-invert")  # p-old moves to the top
    assert session.selected == Row("container", "p-old")
    session.tick()
    assert session.selected == Row("container", "p-old")


def test_folding_keeps_the_stored_sort(mocker):
    session, save = _session_with(
        mocker, ViewState(columns=("name", "state"), sort_field="state", sort_desc=True)
    )
    session.toggle_fold("p")
    saved = save.call_args.args[2]
    assert (saved.sort_field, saved.sort_desc) == ("state", True)
    session.handle_key("details")
    saved = save.call_args.args[2]
    assert (saved.sort_field, saved.sort_desc) == ("state", True)
