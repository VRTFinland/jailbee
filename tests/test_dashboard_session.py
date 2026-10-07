"""DashboardSession without any frontend: the Terminal seam and the view."""

from __future__ import annotations

from datetime import UTC, datetime

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.menu_state import MenuState
from jailbee.db.view_prefs import ViewState
from jailbee.state_service.protocol import Snapshot
from tests.dashboard_fixtures import ci


class _Client:
    def __init__(self, groups):
        self.groups = groups
        self.events: list[tuple] = []

    def latest(self):
        return Snapshot(1, datetime(2026, 10, 7, tzinfo=UTC), False, self.groups)

    def status(self):
        return None

    def refresh(self):
        self.events.append(("refresh",))

    def set_active(self, value):
        self.events.append(("active", value))


class _Terminal:
    width = 120

    def __init__(self):
        self.handed: list[object] = []

    def hand_off(self, fn):
        self.handed.append(fn)
        return fn()


def _session(mocker, groups, **kw):
    mocker.patch.object(tsession, "save_view_state")
    startup = tsession.Startup(mocker.Mock(), _Client(groups), ViewState(), None)  # type: ignore[arg-type]  # duck-typed client
    terminal = _Terminal()
    session = tsession.DashboardSession(
        startup, incus=mocker.Mock(), cwd_root=None, terminal=terminal, **kw
    )
    session.tick()
    return session, terminal


def test_keys_move_and_open_a_menu_without_any_terminal(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    session, _ = _session(mocker, [group])
    assert session.selected == dmodel.Row("repo", "alpha")
    session.handle_input(b"j")
    session.tick()
    assert session.selected == dmodel.Row("container", "alpha-one")
    session.handle_input(b"\r")
    assert isinstance(session.overlay, MenuState)


def test_quit_is_returned_not_raised(mocker, tmp_path):
    session, _ = _session(mocker, [])
    assert session.handle_input(b"q") == "quit"
    assert session.handle_input(b"\x03") == "quit"


def test_the_view_carries_the_render_arguments_and_whole_seconds(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    session, _ = _session(mocker, [group])
    view = session.view()
    assert view.groups == [group] and view.now.microsecond == 0
    assert session.view() == session.view()


def test_a_dispatch_goes_through_the_terminal_hand_off(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch("jailbee.dashboard.dispatch._wait_for_return")
    session, terminal = _session(mocker, [group])
    session.handle_input(b"j")
    session.tick()
    session.handle_input(b"t")  # tmux: a foreground dispatch
    assert len(terminal.handed) == 1
    child.assert_called_once()


def test_the_session_module_imports_without_textual():
    import subprocess
    import sys

    code = "import sys, jailbee.dashboard.tui.session; sys.exit('textual' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
