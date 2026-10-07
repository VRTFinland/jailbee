"""Every mouse rule of the Textual dashboard, on Pilot."""

from __future__ import annotations

from typing import ClassVar

from rich.style import Style
from textual.drivers.linux_driver import LinuxDriver

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.accounts import AccountRow, AccountsState
from jailbee.dashboard.actions import APPLY_NO_RESTART
from jailbee.dashboard.hit import HIT_KEY, Hit
from jailbee.dashboard.overlays import Picker, PickerEntry, TextPrompt
from jailbee.dashboard.settings import SettingsState
from jailbee.dashboard.tui import app as tapp
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.menu_state import MenuState, RepoMenuState
from tests.dashboard_fixtures import WIDE, ci, wide_group
from tests.dashboard_pilot import Click, Wheel, bare_session, drive, patch_pause, start_session

Row = dmodel.Row


def _two(tmp_path):
    return dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [ci("alpha-one", "alpha"), ci("alpha-two", "alpha")]
    )


def _bare_session(mocker, tmp_path):
    session, _terminal = bare_session(mocker, [_two(tmp_path)])
    return session


def test_click_selects_a_container_row(mocker, tmp_path):
    run = drive(mocker, [Click(Hit("row", ("alpha-two",)))], [_two(tmp_path)])
    assert run.trace[1].selected == Row("container", "alpha-two")
    assert run.trace[1].overlay is None


def test_double_click_opens_the_rows_menu(mocker, tmp_path):
    run = drive(mocker, [Click(Hit("row", ("alpha-two",)), times=2)], [_two(tmp_path)])
    assert isinstance(run.last.overlay, MenuState) and run.last.overlay.container == "alpha-two"


def test_right_click_selects_and_opens_the_menu(mocker, tmp_path):
    run = drive(mocker, [Click(Hit("row", ("alpha-one",)), button=3)], [_two(tmp_path)])
    assert run.trace[1].selected == Row("container", "alpha-one")
    assert isinstance(run.trace[1].overlay, MenuState)


def test_heading_click_selects_and_double_click_opens_the_repo_menu(mocker, tmp_path):
    run = drive(mocker, ["j", Click(Hit("repo", ("alpha",)))], [_two(tmp_path)])
    assert run.trace[2].selected == Row("repo", "alpha") and run.trace[2].overlay is None
    run = drive(mocker, [Click(Hit("repo", ("alpha",)), times=2)], [_two(tmp_path)])
    assert isinstance(run.last.overlay, RepoMenuState)


def test_the_fold_marker_toggles_once_and_persists(mocker, tmp_path):
    save = mocker.patch.object(tsession, "save_view_state")
    run = drive(mocker, [Click(Hit("fold", ("alpha",)))], [_two(tmp_path)])
    assert run.trace[1].folded == frozenset({"alpha"})
    save.assert_called_once()
    run = drive(mocker, [Click(Hit("fold", ("alpha",)), times=2)], [_two(tmp_path)])
    assert run.last.folded == frozenset({"alpha"})  # the second click of the pair is ignored


def test_scroll_marks_step_the_columns(mocker, tmp_path):
    run = drive(
        mocker,
        ["right", Click(Hit("scroll", (-1,)))],
        [wide_group(tmp_path)],
        view_state=tsession.ViewState(columns=WIDE),
        size=(44, 25),
    )
    assert run.trace[1].column_offset == 1 and run.trace[2].column_offset == 0


def test_a_menu_row_click_runs_that_entry(mocker, tmp_path):
    patch_pause(mocker)
    child = mocker.patch.object(tsession.subprocess, "run", return_value=mocker.Mock(returncode=0))
    group = _two(tmp_path)
    run = drive(mocker, ["j", "enter"], [group])
    menu = run.last.overlay
    assert isinstance(menu, MenuState)
    tmux_index = next(i for i, (_, verb) in enumerate(menu.actions) if verb == "tmux")
    run = drive(mocker, ["j", "enter", Click(Hit("menu", (tmux_index,)))], [group])
    assert any("tmux" in call.args[0] for call in child.call_args_list)
    assert run.last.overlay is None


def test_a_double_click_on_a_menu_entry_does_not_act_on_the_table_underneath(mocker, tmp_path):
    patch_pause(mocker)
    mocker.patch.object(tsession.subprocess, "run", return_value=mocker.Mock(returncode=0))
    group = _two(tmp_path)
    first = drive(mocker, ["j", "enter"], [group]).last.overlay
    assert isinstance(first, MenuState)
    tmux_index = next(i for i, (_, verb) in enumerate(first.actions) if verb == "tmux")
    run = drive(mocker, ["j", "enter", Click(Hit("menu", (tmux_index,)), times=2)], [group])
    assert run.last.overlay is None  # no second menu opened by the second click


def test_a_click_outside_a_menu_closes_it_and_selects_the_row(mocker, tmp_path):
    run = drive(mocker, ["j", "enter", Click(Hit("row", ("alpha-two",)))], [_two(tmp_path)])
    assert run.last.overlay is None and run.last.selected == Row("container", "alpha-two")


def test_a_click_outside_a_prompt_is_ignored(mocker, tmp_path):
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    run = drive(mocker, ["n", "x", Click(Hit("row", ("alpha-two",)))], [_two(tmp_path)])
    # trace[3]: `run.last` is after the padding Ctrl-C, which cancels the prompt.
    prompt = run.trace[3].overlay
    assert isinstance(prompt, TextPrompt) and prompt.text == "x"
    assert run.trace[3].selected == run.trace[2].selected


def test_a_picker_row_click_chooses_it(mocker, tmp_path):
    picker = Picker(
        "repo-apply",
        "Apply",
        (PickerEntry("Apply", "apply"), PickerEntry("Apply, no restart", APPLY_NO_RESTART)),
        target="alpha",
    )
    chosen = mocker.patch.object(tsession.DashboardSession, "submit_picker", return_value=None)
    run = drive(
        mocker,
        [lambda app: setattr(app.session, "overlay", picker), Click(Hit("picker", (1,)))],
        [_two(tmp_path)],
    )
    assert chosen.call_args.args[1] == PickerEntry("Apply, no restart", APPLY_NO_RESTART)
    assert run.last.overlay is None


def test_a_stale_index_is_ignored(mocker, tmp_path):
    session = _bare_session(mocker, tmp_path)
    session.overlay = Picker("x", "P", (PickerEntry("a", "a"),))
    chosen = mocker.patch.object(tsession.DashboardSession, "submit_picker")
    session.click(Hit("picker", (3,)))
    chosen.assert_not_called()
    assert session.overlay is not None


def test_settings_tab_and_row_clicks(mocker, tmp_path):
    mocker.patch.object(tsession, "save_view_state")
    run = drive(
        mocker, ["S", Click(Hit("tab", ("repos",))), Click(Hit("setting", (0,)))], [_two(tmp_path)]
    )
    assert isinstance(run.trace[2].overlay, SettingsState) and run.trace[2].overlay.tab == "repos"
    assert run.trace[3].folded == frozenset({"alpha"})  # row 0 of Repos is alpha, now folded


def test_wheel_moves_the_selection_or_the_open_list(mocker, tmp_path):
    run = drive(mocker, [Wheel(1), Wheel(1), "enter", Wheel(1)], [_two(tmp_path)])
    assert run.trace[2].selected == Row("container", "alpha-two")
    assert run.trace[4].overlay.index == 1  # type: ignore[union-attr]  # the menu's cursor moved


def test_shift_wheel_and_horizontal_wheel_scroll_columns(mocker, tmp_path):
    run = drive(
        mocker,
        [Wheel(1, shift=True), Wheel(-1, horizontal=True)],
        [wide_group(tmp_path)],
        view_state=tsession.ViewState(columns=WIDE),
        size=(44, 25),
    )
    assert run.trace[1].column_offset == 1 and run.trace[2].column_offset == 0


def test_hover_over_a_menu_row_moves_its_cursor(mocker, tmp_path):
    session = _bare_session(mocker, tmp_path)
    session.handle_input(b"j")
    session.tick()
    session.handle_input(b"\r")
    assert isinstance(session.overlay, MenuState)
    session.hover(Hit("menu", (2,)))
    assert session.overlay.index == 2


def test_m_toggles_mouse_reporting_and_says_so(mocker, tmp_path):
    reporting = mocker.patch.object(tapp, "_set_mouse_reporting")
    run = drive(mocker, ["m", "m"], [_two(tmp_path)])
    assert [c.args[1] for c in reporting.call_args_list] == [False, True]
    assert run.trace[1].notice == "mouse off — terminal text selection active"
    assert run.trace[2].notice == "mouse on"


def test_mouse_off_survives_a_hand_off_on_a_real_driver():
    class _Driver:
        _mouse = True
        calls: ClassVar[list[str]] = []

        def _enable_mouse_support(self):
            if self._mouse:
                self.calls.append("on")

        def _disable_mouse_support(self):
            if self._mouse:
                self.calls.append("off")

    driver = _Driver()
    tapp._set_mouse_reporting(driver, False)  # type: ignore[arg-type]  # duck-typed driver
    driver._enable_mouse_support()  # what Textual does on every resume
    assert driver.calls == ["off"]


def test_dashboard_mouse_false_starts_with_reporting_off(mocker, tmp_path):
    run = drive(mocker, [], [_two(tmp_path)], mouse=False)
    assert run.app.mouse_on is False


def _click_event(mocker, hit, *, chain=1, button=1):
    """What `DashboardApp.on_click` reads of a Textual click: the tagged style, chain, button."""
    return mocker.Mock(chain=chain, button=button, style=Style(meta={HIT_KEY: hit.meta_value()}))


def test_a_double_click_acts_only_on_the_row_its_first_click_hit(mocker, tmp_path):
    one, two = Hit("row", ("alpha-one",)), Hit("row", ("alpha-two",))
    run = drive(
        mocker,
        [
            lambda app: app.on_click(_click_event(mocker, one)),
            lambda app: app.on_click(_click_event(mocker, two, chain=2)),
        ],
        [_two(tmp_path)],
    )
    assert run.trace[2].selected == Row("container", "alpha-one")
    assert run.trace[2].overlay is None  # a pair split across two rows is no double-click
    run = drive(
        mocker,
        [
            lambda app: app.on_click(_click_event(mocker, one)),
            lambda app: app.on_click(_click_event(mocker, one, chain=2)),
        ],
        [_two(tmp_path)],
    )
    assert isinstance(run.trace[2].overlay, MenuState)


def test_the_second_click_of_a_fold_marker_pair_toggles_nothing(mocker, tmp_path):
    save = mocker.patch.object(tsession, "save_view_state")
    fold = Hit("fold", ("alpha",))
    run = drive(
        mocker,
        [
            lambda app: app.on_click(_click_event(mocker, fold)),
            lambda app: app.on_click(_click_event(mocker, fold, chain=2)),
        ],
        [_two(tmp_path)],
    )
    assert run.trace[1].folded == frozenset({"alpha"})
    assert run.trace[2].folded == frozenset({"alpha"})
    save.assert_called_once()


def test_a_triple_click_opens_the_menu_once(mocker, tmp_path):
    one = Hit("row", ("alpha-one",))
    run = drive(
        mocker,
        [
            lambda app: app.on_click(_click_event(mocker, one)),
            lambda app: app.on_click(_click_event(mocker, one, chain=2)),
            lambda app: app.session.overlay_move(1),
            lambda app: app.on_click(_click_event(mocker, one, chain=3)),
        ],
        [_two(tmp_path)],
    )
    menu = run.trace[4].overlay
    assert isinstance(menu, MenuState) and menu.index == 1  # not closed and reopened at 0


def test_the_second_click_of_a_right_click_pair_is_ignored(mocker, tmp_path):
    one = Hit("row", ("alpha-one",))
    run = drive(
        mocker,
        [
            lambda app: app.on_click(_click_event(mocker, one, button=3)),
            lambda app: app.session.overlay_move(1),
            lambda app: app.on_click(_click_event(mocker, one, chain=2, button=3)),
        ],
        [_two(tmp_path)],
    )
    menu = run.trace[3].overlay
    assert isinstance(menu, MenuState) and menu.index == 1  # not closed and reopened at 0


def test_a_double_click_on_an_accounts_row_means_enter(mocker, tmp_path):
    rows = (
        AccountRow("claude", "g", "main", "parked", (), ()),
        AccountRow("claude", "g", "side", "parked", (), ()),
    )
    state = AccountsState(rows, 0, "alpha")
    acted = mocker.patch.object(tsession.DashboardSession, "account_actions_picker")
    run = drive(
        mocker,
        [
            lambda app: setattr(app.session, "overlay", state),
            Click(Hit("account", (1,))),
            Click(Hit("account", (1,)), times=2),
        ],
        [_two(tmp_path)],
    )
    assert run.trace[2].overlay.index == 1  # type: ignore[union-attr]  # a single click only selects
    assert acted.call_count == 1  # only the pair's second click acted
    assert run.rc == 0


def test_stale_indices_are_ignored_not_acted_on(mocker, tmp_path):
    session = _bare_session(mocker, tmp_path)
    session.handle_input(b"j")
    session.tick()
    session.handle_input(b"\r")
    menu = session.overlay
    assert isinstance(menu, MenuState)
    dispatch = mocker.patch.object(tsession.DashboardSession, "dispatch")
    session.click(Hit("menu", (99,)))
    assert session.overlay == menu
    dispatch.assert_not_called()
    session.click(Hit("menu", (-1,)))
    assert session.overlay == menu
    # a stale row click keeps the open menu too: nothing was clicked
    session.click(Hit("row", ("gone",)))
    assert session.overlay == menu
    assert session.selected == Row("container", "alpha-one")


def test_stale_fold_and_repo_clicks_change_nothing(mocker, tmp_path):
    session = _bare_session(mocker, tmp_path)
    save = tsession.save_view_state
    session.click(Hit("fold", ("gone",)))
    session.click(Hit("repo", ("gone",)), double=True)
    assert session.folded == frozenset() and session.overlay is None
    save.assert_not_called()  # type: ignore[attr-defined]  # patched by bare_session


def test_a_click_outside_help_closes_it(mocker, tmp_path):
    session = _bare_session(mocker, tmp_path)
    session.handle_input(b"h")
    assert session.overlay == "help"
    session.click(Hit("row", ("alpha-two",)))
    assert session.overlay is None and session.selected == Row("container", "alpha-two")


def test_a_fold_marker_click_with_a_menu_open_only_selects_the_heading(mocker, tmp_path):
    session = _bare_session(mocker, tmp_path)
    session.handle_input(b"j")
    session.tick()
    session.handle_input(b"\r")
    session.click(Hit("fold", ("alpha",)))
    assert session.overlay is None and session.selected == Row("repo", "alpha")
    assert session.folded == frozenset()


def test_m_with_a_mouse_off_start_turns_it_on(mocker, tmp_path):
    reporting = mocker.patch.object(tapp, "_set_mouse_reporting")
    run = drive(mocker, ["m"], [_two(tmp_path)], mouse=False)
    assert reporting.call_args.args[1] is True
    assert run.trace[1].notice == "mouse on"


def test_mouse_off_survives_a_hand_off_on_textuals_own_driver_methods():
    """Textual re-enables reporting from `_mouse` on every resume; `m` off must clear it."""
    writes: list[str] = []

    class _Driver:
        _mouse = True
        _enable_mouse_support = LinuxDriver._enable_mouse_support
        _disable_mouse_support = LinuxDriver._disable_mouse_support

        def write(self, text: str) -> None:
            writes.append(text)

        def flush(self) -> None:
            pass

    driver = _Driver()
    tapp._set_mouse_reporting(driver, False)  # type: ignore[arg-type]  # duck-typed driver
    writes.clear()
    LinuxDriver._enable_mouse_support(driver)  # type: ignore[arg-type]  # what a resume runs
    assert writes == []
    tapp._set_mouse_reporting(driver, True)  # type: ignore[arg-type]  # duck-typed driver
    assert "\x1b[?1000h" in writes


def test_the_app_starts_with_the_configured_mouse_setting(mocker):
    _, _client = start_session(mocker, [], mouse=False)
    run = mocker.patch.object(tapp.DashboardApp, "run", return_value=0)
    assert tapp.run(mocker.Mock(), None) == 0
    run.assert_called_once_with(mouse=False)
