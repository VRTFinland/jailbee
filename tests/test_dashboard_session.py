"""DashboardSession without any frontend: the Terminal seam and the view."""

from __future__ import annotations

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.menu_state import MenuState
from tests.dashboard_fixtures import ci, wide_group
from tests.dashboard_pilot import bare_session


def test_keys_move_and_open_a_menu_without_any_terminal(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    session, _ = bare_session(mocker, [group])
    assert session.selected == dmodel.Row("repo", "alpha")
    session.handle_key("down")
    session.tick()
    assert session.selected == dmodel.Row("container", "alpha-one")
    session.handle_key("enter")
    assert isinstance(session.overlay, MenuState)


def test_quit_is_returned_not_raised(mocker, tmp_path):
    session, _ = bare_session(mocker, [])
    assert session.handle_key("quit") == "quit"
    assert session.handle_key("interrupt") == "quit"


def test_the_view_carries_the_render_arguments_and_whole_seconds(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    session, _ = bare_session(mocker, [group])
    view = session.view()
    assert view.groups == [group] and view.now.microsecond == 0
    assert session.view() == session.view()


def test_a_dispatch_goes_through_the_terminal_hand_off(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch("jailbee.dashboard.dispatch._wait_for_return")
    session, terminal = bare_session(mocker, [group])
    session.handle_key("down")
    session.tick()
    session.handle_key("action:tmux")  # tmux: a foreground dispatch
    assert len(terminal.handed) == 1
    child.assert_called_once()


def test_the_offset_clamp_uses_the_tables_own_width(mocker, tmp_path):
    clamp = mocker.spy(tsession, "clamp_column_offset")
    session, terminal = bare_session(mocker, [wide_group(tmp_path)])
    terminal.table_width = 40
    session.scroll_columns(1)
    assert clamp.call_args.kwargs["available"] == 40


def test_the_session_module_imports_without_textual():
    import subprocess
    import sys

    code = "import sys, jailbee.dashboard.tui.session; sys.exit('textual' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
