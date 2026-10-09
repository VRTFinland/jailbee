"""The terminal dashboard sorts each repo group's rows and remembers how."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from jailbee.agent_status import AgentSummary
from jailbee.dashboard import hit as dhit
from jailbee.dashboard.model import RepoGroup, Row
from jailbee.dashboard.sorting import DEFAULT_SORT, SortSpec, active_sort
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.keys import parse_key
from jailbee.db.view_prefs import ViewState
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import BareClient, BareTerminal

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _group() -> RepoGroup:
    # Names whose alphabetical order is the reverse of the default (newest first).
    old = dataclasses.replace(ci("p-a", "p", state="Stopped"), created_at=T0 - timedelta(days=1))
    new = dataclasses.replace(ci("p-b", "p"), created_at=T0)
    return RepoGroup("p", "/repos/p", None, [new, old])


def _names(session) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.name for g in session.groups for c in g.containers]


def _session_with(mocker, view_state: ViewState, group=None):  # type: ignore[no-untyped-def]
    save = mocker.patch.object(tsession, "save_view_state")
    startup = tsession.Startup(mocker.Mock(), BareClient([group or _group()]), view_state, None)  # type: ignore[arg-type]  # duck-typed client
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
    assert _names(session) == ["p-a", "p-b"]
    assert session.view().sort == SortSpec("state", True)


def test_sort_keys_move_and_invert_and_persist(mocker):
    session, save = _session_with(mocker, ViewState(columns=("name", "state")))
    assert _names(session) == ["p-b", "p-a"]
    session.handle_key("sort-next")  # name ▲
    assert session.sort == SortSpec("name", False)
    assert _names(session) == ["p-a", "p-b"]
    session.handle_key("sort-invert")  # name ▼
    assert _names(session) == ["p-b", "p-a"]
    session.handle_key("sort-prev")  # back to the default stop
    assert session.sort == DEFAULT_SORT
    assert _names(session) == ["p-b", "p-a"]
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
    target = Row("container", "p-a")
    session.select(target)
    before = session.sel_index
    session.handle_key("sort-next")  # name ▲: p-a moves to the top
    assert session.selected == target
    assert session.sel_index == session.rows.index(target)
    assert session.sel_index != before
    session.tick()
    assert session.selected == target
    assert session.sel_index == session.rows.index(target)


def test_every_tick_sorts_the_fresh_snapshot(mocker):
    session, _ = _session_with(mocker, ViewState(columns=("name", "state")))
    session.handle_key("sort-next")  # name ▲
    assert _names(session) == ["p-a", "p-b"]
    newest = dataclasses.replace(ci("p-c", "p"), created_at=T0 + timedelta(days=1))
    group = _group()
    session.client.groups = [dataclasses.replace(group, containers=[newest, *group.containers])]
    session.tick()
    assert _names(session) == ["p-a", "p-b", "p-c"]


def _toggle(session, key):  # type: ignore[no-untyped-def]
    session.handle_key("settings")
    session.setting_toggled("fields", key)


def test_disabling_the_sort_column_falls_back_at_once_and_enabling_restores(mocker):
    session, save = _session_with(
        mocker, ViewState(columns=("name", "state"), sort_field="state", sort_desc=True)
    )
    assert _names(session) == ["p-a", "p-b"]
    _toggle(session, "state")
    assert _names(session) == ["p-b", "p-a"]  # default order, no tick needed
    saved = save.call_args.args[2]
    assert (saved.sort_field, saved.sort_desc) == ("state", True)  # the choice is kept
    session.setting_toggled("fields", "state")
    assert _names(session) == ["p-a", "p-b"]


def test_a_header_click_is_ignored_while_an_overlay_is_open(mocker):
    session, save = _session_with(mocker, ViewState(columns=("name", "state")))
    session.overlay = "help"
    session.click(dhit.Hit("sort", ("state",)))
    assert session.sort == DEFAULT_SORT
    assert _names(session) == ["p-b", "p-a"]
    assert not save.called


def test_a_settings_toggle_keeps_the_stored_sort(mocker):
    session, save = _session_with(
        mocker, ViewState(columns=("name", "state", "ip"), sort_field="state", sort_desc=True)
    )
    _toggle(session, "ip")
    saved = save.call_args.args[2]
    assert (saved.sort_field, saved.sort_desc) == ("state", True)


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


_AGENT_VIEW = ViewState(
    columns=("name", "state", "agent_compact"), sort_field="agent_compact", sort_desc=False
)


def _agent_on_older(group: RepoGroup) -> None:
    older = next(c for c in group.containers if c.name == "p-a")
    older.agent_status = (AgentSummary("claude", "busy", T0, None, 1),)


def test_a_sort_column_that_is_pruned_does_not_sort_and_shows_no_mark(mocker):
    session, _ = _session_with(mocker, _AGENT_VIEW)
    assert "agent_compact" not in session.shown_columns
    assert _names(session) == ["p-b", "p-a"]  # the default order
    assert session.sort == SortSpec("agent_compact", False)  # kept for when it is shown again
    assert active_sort(session.sort, session.shown_columns, now=T0) == DEFAULT_SORT


def test_a_pruned_sort_column_sorts_again_once_it_is_shown(mocker):
    group = _group()
    session, _ = _session_with(mocker, _AGENT_VIEW, group)
    assert _names(session) == ["p-b", "p-a"]
    _agent_on_older(group)
    session.tick()
    assert _names(session) == ["p-b", "p-a"]  # shown_columns is not recomputed on a tick
    session.handle_key("optimize")
    assert "agent_compact" in session.shown_columns
    assert session.sort == SortSpec("agent_compact", False)  # kept, never cleared
    assert _names(session) == ["p-a", "p-b"]


def test_the_sort_keys_cycle_through_the_shown_columns_only(mocker):
    session, _ = _session_with(mocker, ViewState(columns=("name", "state", "agent_compact")))
    session.handle_key("sort-prev")  # wraps to the last *shown* stop
    assert session.sort == SortSpec("state", False)
    session.handle_key("sort-next")
    assert session.sort == DEFAULT_SORT
    session.handle_key("sort-next")
    assert session.sort == SortSpec("name", False)
    assert active_sort(session.sort, session.shown_columns, now=T0) == session.sort
