"""Native overlay boxes: the slot, routing and the per-kind boxes (V3a)."""

from __future__ import annotations

import pytest

from jailbee.dashboard.hit import Hit
from jailbee.dashboard.tui import frame as tframe
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.menu_state import MenuState
from jailbee.dashboard.tui.overlay import NativeState, is_native, overlay_key
from tests.dashboard_fixtures import alpha_group
from tests.dashboard_pilot import NATIVE_LIST, Click, backgrounds, drive


def test_help_is_native_and_keyed():
    assert is_native("help")
    assert overlay_key("help") == ("help",)
    assert overlay_key(None) is None
    assert not is_native(None)


@pytest.mark.xfail(strict=True, reason="Task 3")
def test_overlay_key_ignores_initial_cursor_fields():
    a = MenuState("alpha-x", [("Attach tmux", "tmux")])
    b = MenuState("alpha-x", [("Attach tmux", "tmux")], start_group="Git →", start_index=2)
    assert overlay_key(a) == overlay_key(b) == ("menu", "alpha-x")


def test_help_lines_say_the_wheel_scrolls():
    text = "\n".join(tframe.help_lines())
    assert "the wheel moves" not in text
    assert "the wheel scrolls" in text
    assert "Shift-drag selects text" in text


def _help_state(app):  # type: ignore[no-untyped-def]
    return app.frame.native_state()


def test_help_opens_native_with_focus(mocker, tmp_path):
    focused = []
    run = drive(mocker, ["h", lambda app: focused.append(app.focused)], [alpha_group(tmp_path)])
    assert run.natives[1] == NativeState("help", None)
    assert focused and focused[0] is not None and focused[0].id == "native-list"


@pytest.mark.parametrize("close", ["escape", "h", "q"])
def test_help_closes_and_the_dashboard_stays(mocker, tmp_path, close):
    run = drive(mocker, ["h", close], [alpha_group(tmp_path)])
    assert run.trace[1].overlay == "help"
    assert run.trace[2].overlay is None
    assert run.steps_taken > 2  # still running: the padding Ctrl-C quit it


def test_ctrl_c_in_help_quits(mocker, tmp_path):
    run = drive(mocker, ["h", "ctrl+c"], [alpha_group(tmp_path)])
    assert run.rc == 0 and run.steps_taken == 2


def test_j_in_help_scrolls_help_not_the_table(mocker, tmp_path):
    scrolls = []
    run = drive(
        mocker,
        ["h", "j", "j", lambda app: scrolls.append(app.query_one(NATIVE_LIST).scroll_y)],
        [alpha_group(tmp_path)],
        size=(80, 20),
    )
    assert scrolls == [2]
    assert {view.selected for view in run.trace[1:4]} == {run.trace[1].selected}


def test_tab_in_help_keeps_focus(mocker, tmp_path):
    focused = []
    drive(
        mocker,
        ["h", "tab", lambda app: focused.append(app.focused and app.focused.id)],
        [alpha_group(tmp_path)],
    )
    assert focused == ["native-list"]


def test_s_from_help_opens_settings(mocker, tmp_path):
    run = drive(mocker, ["h", "S"], [alpha_group(tmp_path)])
    assert isinstance(run.trace[2].overlay, tsession.SettingsState)


def test_a_table_click_closes_help_and_selects(mocker, tmp_path):
    run = drive(mocker, ["h", Click(Hit("row", ("alpha-x",)))], [alpha_group(tmp_path)])
    assert run.trace[-1].overlay is None
    assert run.trace[-1].selected == tsession.Row("container", "alpha-x")


def test_help_paints_no_background(mocker, tmp_path):
    seen = []
    drive(mocker, ["h", lambda app: seen.append(backgrounds(app))], [alpha_group(tmp_path)])
    assert seen[0] <= {"default"}


@pytest.mark.parametrize("height", [6, 8, 10])
def test_a_tiny_terminal_keeps_the_hint_with_help_open(mocker, tmp_path, height):
    run = drive(mocker, ["h"], [alpha_group(tmp_path)], size=(80, height), screens=True)
    lines = run.screens[1].rstrip("\n").splitlines()
    assert "close" in lines[-2] and lines[-1].startswith("╰")
