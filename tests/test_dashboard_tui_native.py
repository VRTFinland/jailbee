"""Native overlay boxes: the slot, routing and the per-kind boxes (V3a)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from textual.app import ComposeResult
from textual.widgets import Input
from textual.widgets.option_list import Option

from jailbee.dashboard import menus as dmenus
from jailbee.dashboard import settings as ds
from jailbee.dashboard.hit import HOVER_STYLE, Hit
from jailbee.dashboard.overlays import Picker, PickerEntry
from jailbee.dashboard.tui import app as tapp
from jailbee.dashboard.tui import frame as tframe
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui import widgets as twidgets
from jailbee.dashboard.tui.menu_state import MenuState, RepoMenuState
from jailbee.dashboard.tui.native import (
    AccountsBox,
    EgressBox,
    MenuBox,
    OverlayBox,
    OverlayList,
    PickerBox,
    SettingsBox,
)
from jailbee.dashboard.tui.overlay import NativeState, is_native, overlay_key
from jailbee.db.view_prefs import ViewState
from jailbee.egress_scope import EntryRow
from tests.dashboard_fixtures import (
    ACCOUNT_ROWS,
    alpha_group,
    cfg_group,
    ci,
    fake_accounts_cli,
    groups_listing,
    named_rows_group,
)
from tests.dashboard_pilot import (
    NATIVE_LIST,
    Click,
    HoverOption,
    Pick,
    PickTab,
    Resize,
    Wheel,
    backgrounds,
    box_text,
    container_egress_keys,
    drive,
    make_app,
    open_container_group_picker,
    option_offset,
    repo_menu_keys,
)


def test_menus_are_native_and_keyed_by_their_owner():
    assert is_native(MenuState("alpha-x", [("Attach tmux", "tmux")]))
    assert is_native(RepoMenuState("alpha", [("Fold", "fold")]))
    assert overlay_key(RepoMenuState("alpha", [("Fold", "fold")])) == ("repo-menu", "alpha")


def test_help_is_native_and_keyed():
    assert is_native("help")
    assert overlay_key("help") == ("help",)
    assert overlay_key(None) is None
    assert not is_native(None)


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
    group = named_rows_group(tmp_path, 3)
    # Control: with no overlay open, `j` does move the selection in this fixture.
    control = drive(mocker, ["j"], [group])
    assert control.trace[1].selected != control.trace[0].selected
    scrolls = []
    run = drive(
        mocker,
        ["h", "j", "j", lambda app: scrolls.append(app.query_one(NATIVE_LIST).scroll_target_y)],
        [group],
        size=(80, 20),
    )
    assert scrolls == [2]
    assert {view.selected for view in run.trace[1:4]} == {run.trace[1].selected}


def test_tab_in_help_keeps_focus(mocker, tmp_path):
    focused = []
    drive(
        mocker,
        [
            "h",
            # A second focusable widget, so Screen's tab cycling has somewhere to go.
            lambda app: app.screen.mount(Input(id="other")),
            "tab",
            lambda app: focused.append(app.focused and app.focused.id),
        ],
        [alpha_group(tmp_path)],
    )
    assert focused == ["native-list"]


def test_a_click_inside_the_help_box_leaves_it_open(mocker, tmp_path):
    app = make_app(mocker, [alpha_group(tmp_path)])

    async def main() -> None:
        async with app.run_test(size=(80, 25)) as pilot:
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            await pilot.click(NATIVE_LIST)
            await pilot.pause()

    asyncio.run(main())
    assert app.session.overlay == "help"


def test_a_refresh_keeps_the_mounted_box(mocker, tmp_path):
    boxes = []
    drive(
        mocker,
        [
            "h",
            lambda app: boxes.append(app.frame.native_box),
            lambda app: setattr(app, "_painted", None),  # the next refresh reaches `show`
            lambda app: boxes.append(app.frame.native_box),
        ],
        [alpha_group(tmp_path)],
    )
    assert boxes[0] is not None and boxes[1] is boxes[0]


def test_s_from_help_opens_settings(mocker, tmp_path):
    run = drive(mocker, ["h", "S"], [alpha_group(tmp_path)])
    assert isinstance(run.trace[2].overlay, tsession.SettingsState)


def test_a_table_click_closes_help_and_selects(mocker, tmp_path):
    run = drive(mocker, ["h", Click(Hit("row", ("alpha-x",)))], [alpha_group(tmp_path)])
    assert run.trace[-1].overlay is None
    assert run.trace[-1].selected == tsession.Row("container", "alpha-x")


def test_help_paints_no_background(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")  # else Textual strips every colour and the scan proves nothing
    seen = []
    drive(mocker, ["h", lambda app: seen.append(backgrounds(app))], [alpha_group(tmp_path)])
    assert seen[0] and seen[0] <= {"default"}  # the screen was scanned, and nothing is painted


@pytest.mark.parametrize("height", [6, 8, 10])
def test_a_tiny_terminal_keeps_the_hint_with_help_open(mocker, tmp_path, height):
    run = drive(mocker, ["h"], [alpha_group(tmp_path)], size=(80, height), screens=True)
    lines = run.screens[1].rstrip("\n").splitlines()
    assert "close" in lines[-2] and lines[-1].startswith("╰")


class _ProbeBox(OverlayBox):
    """A second native kind, so a swap between two boxes can be driven."""

    def compose(self) -> ComposeResult:
        yield OverlayList(Option("one"), mouse_enabled=self.mouse_enabled)

    def content_rows(self) -> int:
        return 1

    def state(self) -> NativeState:
        return NativeState("probe", 0)


def _with_probe_kind(mocker, box_cls=_ProbeBox) -> None:  # type: ignore[no-untyped-def]
    """Make the string overlay ``"probe"`` a native kind next to help."""
    mocker.patch.object(twidgets, "is_native", lambda o: o in ("help", "probe"))
    real_key = twidgets.overlay_key
    mocker.patch.object(
        twidgets, "overlay_key", lambda o: ("probe",) if o == "probe" else real_key(o)
    )
    real_build = twidgets.build_box
    mocker.patch.object(
        twidgets,
        "build_box",
        lambda o, *, mouse_enabled: (
            box_cls(o, mouse_enabled=mouse_enabled)
            if o == "probe"
            else real_build(o, mouse_enabled=mouse_enabled)
        ),
    )


def test_swapping_one_native_overlay_for_another_mounts_and_focuses_the_new_box(mocker, tmp_path):
    _with_probe_kind(mocker)
    seen = []

    def probe(app):  # type: ignore[no-untyped-def]
        seen.append((type(app.frame.native_box).__name__, app.frame.native_state(), app.focused))

    run = drive(
        mocker,
        [
            "h",
            lambda app: setattr(app.session, "overlay", "probe"),
            probe,
            lambda app: setattr(app.session, "overlay", "help"),
            probe,
        ],
        [alpha_group(tmp_path)],
    )
    assert run.rc == 0
    assert [s[0] for s in seen] == ["_ProbeBox", "HelpBox"]
    assert seen[0][1] == NativeState("probe", 0)
    assert seen[1][1] == NativeState("help", None)
    assert all(s[2] is not None and s[2].id == "native-list" for s in seen)


def test_rapid_native_swaps_never_duplicate_the_list_id(mocker, tmp_path):
    _with_probe_kind(mocker)

    def flip(app):  # type: ignore[no-untyped-def]
        # Several swaps before the old box's removal can run: the id must never clash.
        for o in ("probe", "help", "probe", "help"):
            app.session.overlay = o
            app.refresh_frame()

    run = drive(mocker, ["h", flip, flip], [alpha_group(tmp_path)])
    assert run.rc == 0


# --- the picker ---------------------------------------------------------------


def _apply_picker(group):  # type: ignore[no-untyped-def]
    return repo_menu_keys(group, "apply")


def _entries(*labels: str) -> tuple[PickerEntry, ...]:
    return tuple(PickerEntry(label, label.lower()) for label in labels)


def _open(overlay: object):  # type: ignore[no-untyped-def]
    return lambda app: setattr(app.session, "overlay", overlay)


def test_picker_is_native(mocker, tmp_path):
    group = cfg_group(tmp_path)
    steps = [*_apply_picker(group), "j"]
    run = drive(mocker, steps, [group])
    assert run.natives[len(steps)] == NativeState("picker", 1)  # [-1] is after the padding Ctrl-C


def test_ticks_keep_the_picker_cursor(mocker, tmp_path):
    group = cfg_group(tmp_path)
    tick = lambda app: None  # noqa: E731 - each callable step is followed by a tick
    steps = [*_apply_picker(group), "j", *[tick] * 5]
    run = drive(mocker, steps, [group])
    assert [n.cursor for n in run.natives[len(steps) - 5 : len(steps) + 1]] == [1] * 6


def test_click_on_an_option_chooses_it(mocker, tmp_path):
    group = cfg_group(tmp_path)
    chosen = mocker.patch.object(
        tsession.DashboardSession, "submit_picker", autospec=True, return_value=None
    )
    run = drive(mocker, [*_apply_picker(group), Pick(1)], [group])
    (_session, picker, entry), _ = chosen.call_args
    assert picker.purpose == "repo-apply" and entry.value == "no-restart"
    assert "Cancelled" not in run.notices()
    assert run.trace[-1].overlay is None


@pytest.mark.parametrize("key", ["ctrl+c", "q", "escape"])
def test_ctrl_c_and_q_cancel_a_picker(mocker, tmp_path, key):
    group = cfg_group(tmp_path)
    steps = [*_apply_picker(group), key]
    run = drive(mocker, steps, [group])
    assert run.steps_taken > len(steps)  # the dashboard survived the key
    assert run.trace[len(steps)].overlay is None  # an apply picker has no `back`
    assert run.trace[len(steps)].notice == "Cancelled"


def test_a_cancelled_picker_returns_to_its_panel(mocker, tmp_path):
    fake_accounts_cli(mocker)
    run = drive(mocker, ["A", "enter", "escape"], [alpha_group(tmp_path)])
    assert isinstance(run.trace[2].overlay, tsession.Picker)  # acct-action on the live login
    assert isinstance(run.trace[3].overlay, tsession.da.AccountsState)
    assert run.trace[3].notice == "Cancelled"


def test_enter_chooses_the_highlighted_entry(mocker, tmp_path):
    picker = Picker("x", "Pick", _entries("Alpha", "Beta", "Gamma"))
    chosen = mocker.patch.object(tsession.DashboardSession, "submit_picker", return_value=None)
    run = drive(mocker, [_open(picker), "j", "j", "enter"], [alpha_group(tmp_path)])
    assert chosen.call_args.args[1] == PickerEntry("Gamma", "gamma")
    assert run.trace[-1].overlay is None


def test_a_picker_shows_its_title_and_entries(mocker, tmp_path):
    lines = box_text(Picker("x", "Pick one", _entries("Alpha", "Beta")))
    text = "\n".join(lines)
    assert "Pick one" in lines[0] and "Alpha" in text and "Beta" in text


def test_a_long_label_is_cut_to_one_line(mocker, tmp_path):
    lines = box_text(Picker("x", "T", _entries("x" * 200)), size=(40, 10))
    assert len(lines) == 3  # border, one row, border
    assert "…" in lines[1]


def test_a_picker_taller_than_the_screen_scrolls_to_its_cursor(mocker, tmp_path):
    picker = Picker("x", "Pick one", _entries(*[f"Entry {i}" for i in range(30)]))
    run = drive(
        mocker, [_open(picker), "end"], [named_rows_group(tmp_path, 3)], size=(80, 20), screens=True
    )
    assert run.natives[2] == NativeState("picker", 29)
    assert len(run.screens[2].splitlines()) <= 20
    assert "Entry 29" in run.screens[2] and "Entry 0 " not in run.screens[2]


def test_an_empty_picker_chooses_nothing(mocker, tmp_path):
    chosen = mocker.patch.object(tsession.DashboardSession, "submit_picker", return_value=None)
    run = drive(
        mocker, [_open(Picker("x", "Empty", ())), "enter"], [alpha_group(tmp_path)], screens=True
    )
    chosen.assert_not_called()
    assert isinstance(run.trace[2].overlay, Picker)
    assert "(nothing to choose)" in run.screens[1]


def test_a_click_outside_a_picker_closes_it(mocker, tmp_path):
    picker = Picker("x", "Pick", _entries("Alpha", "Beta"))
    run = drive(
        mocker,
        [_open(picker), Click(Hit("row", ("alpha-x",)))],
        [alpha_group(tmp_path)],
    )
    assert run.trace[-1].overlay is None and run.trace[-1].selected is not None


def test_a_picker_step_into_another_picker_mounts_the_new_box(mocker, tmp_path):
    fake_accounts_cli(mocker)
    focused = []
    run = drive(
        mocker,
        ["A", "enter", "enter", lambda app: focused.append(app.focused and app.focused.id)],
        [alpha_group(tmp_path)],
    )
    purposes = [o.purpose for o in run.of_type(Picker)]
    assert purposes[0] == "acct-action" and "acct-use" in purposes  # `use` opens the login list
    assert run.natives[3] == NativeState("picker", 0)  # a new box, cursor back at the top
    assert focused == ["native-list"]


def test_a_picker_step_into_a_native_help_swaps_the_box(mocker, tmp_path):
    picker = Picker("x", "Pick", _entries("Alpha"))
    mocker.patch.object(tsession.DashboardSession, "submit_picker", return_value="help")
    seen = []
    run = drive(
        mocker,
        [
            _open(picker),
            "enter",
            lambda app: seen.append((type(app.frame.native_box).__name__, app.focused)),
        ],
        [alpha_group(tmp_path)],
    )
    assert run.rc == 0
    assert seen[0][0] == "HelpBox" and seen[0][1] is not None and seen[0][1].id == "native-list"


def test_a_picker_back_to_a_panel_swaps_the_boxes(mocker, tmp_path):
    fake_accounts_cli(mocker)
    seen = []
    drive(
        mocker,
        ["A", "enter", "escape", lambda app: seen.append(app.frame.native_box)],
        [alpha_group(tmp_path)],
    )
    assert isinstance(seen[0], AccountsBox)  # the picker's box gave way to the panel's


# --- the list base: hover, wheel, gated clicks -----------------------------------


def _bg(app, index: int):  # type: ignore[no-untyped-def]
    """The background colour name of option ``index``'s first cell, as composited."""
    x, y = option_offset(app, index)
    at = 0
    for segment in app.screen._compositor.render_strips()[y]:
        at += segment.cell_length
        if at > x:
            return segment.style.bgcolor.name if segment.style and segment.style.bgcolor else None
    raise AssertionError("cell is off screen")


def test_hovering_an_option_paints_it_without_moving_the_cursor(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")  # the suite sets it, and Textual then strips every colour
    picker = Picker("x", "Pick", _entries("Alpha", "Beta", "Gamma"))
    seen = {}
    run = drive(
        mocker,
        [
            _open(picker),
            HoverOption(2),
            lambda app: seen.update(hovered=[_bg(app, i) for i in range(3)]),
        ],
        [alpha_group(tmp_path)],
    )
    grey = HOVER_STYLE.bgcolor.name
    assert seen["hovered"][2] == grey
    assert seen["hovered"][0] != grey and seen["hovered"][1] != grey  # only that option
    assert run.natives[3] == NativeState("picker", 0)  # the highlight stays put


def test_the_highlighted_option_keeps_its_own_style_when_hovered(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")
    picker = Picker("x", "Pick", _entries("Alpha", "Beta"))
    seen = []
    drive(
        mocker,
        [_open(picker), HoverOption(0), lambda app: seen.append(_bg(app, 0))],
        [alpha_group(tmp_path)],
    )
    assert seen[0] != HOVER_STYLE.bgcolor.name


def _scroll_y(app) -> int:  # type: ignore[no-untyped-def]
    return int(app.query_one(NATIVE_LIST).scroll_y)


def test_one_wheel_notch_scrolls_one_line_and_never_moves_the_cursor(mocker, tmp_path):
    picker = Picker("x", "Pick", _entries(*[f"Entry {i}" for i in range(30)]))
    ys = []
    run = drive(
        mocker,
        [
            _open(picker),
            Wheel(1, at=NATIVE_LIST),
            lambda app: ys.append(_scroll_y(app)),
            Wheel(1, at=NATIVE_LIST),
            lambda app: ys.append(_scroll_y(app)),
            Wheel(-1, at=NATIVE_LIST),
            lambda app: ys.append(_scroll_y(app)),
        ],
        [named_rows_group(tmp_path, 3)],
        size=(80, 14),
    )
    assert ys == [1, 2, 1]
    assert run.natives[7] == NativeState("picker", 0)


def test_the_wheel_does_nothing_with_the_mouse_off(mocker, tmp_path):
    picker = Picker("x", "Pick", _entries(*[f"Entry {i}" for i in range(30)]))
    ys = []
    run = drive(
        mocker,
        [_open(picker), Wheel(1, at=NATIVE_LIST), lambda app: ys.append(_scroll_y(app))],
        [named_rows_group(tmp_path, 3)],
        size=(80, 14),
        mouse=False,
    )
    assert ys == [0]
    assert run.natives[3] == NativeState("picker", 0)


def test_a_click_while_the_mouse_is_off_chooses_nothing(mocker, tmp_path):
    picker = Picker("x", "Pick", _entries("Alpha", "Beta"))
    chosen = mocker.patch.object(tsession.DashboardSession, "submit_picker", return_value=None)
    run = drive(mocker, [_open(picker), Pick(1)], [alpha_group(tmp_path)], mouse=False)
    chosen.assert_not_called()
    assert isinstance(run.trace[2].overlay, Picker)
    assert run.natives[2] == NativeState("picker", 0)  # not even highlighted


class _DoubleBox(OverlayBox):
    """A list whose rows are chosen by a double click, like egress and accounts."""

    def __init__(self, spec, *, mouse_enabled):  # type: ignore[no-untyped-def]
        super().__init__(spec, mouse_enabled=mouse_enabled)
        self.chosen: list[int] = []

    def compose(self) -> ComposeResult:
        yield OverlayList(
            Option("one"),
            Option("two"),
            mouse_enabled=self.mouse_enabled,
            double_click_chooses=True,
        )

    def content_rows(self) -> int:
        return 2

    def state(self) -> NativeState:
        return NativeState("double", self.query_one(OverlayList).highlighted)

    def on_option_list_option_selected(self, event) -> None:  # type: ignore[no-untyped-def]
        event.stop()
        self.chosen.append(event.option_index)


def test_double_click_chooses_highlights_on_one_click_and_chooses_on_two(mocker, tmp_path):
    _with_probe_kind(mocker, _DoubleBox)
    seen = []
    run = drive(
        mocker,
        [
            lambda app: setattr(app.session, "overlay", "probe"),
            Pick(1),
            lambda app: seen.append(list(app.frame.native_box.chosen)),
            Pick(1, times=2),
            lambda app: seen.append(list(app.frame.native_box.chosen)),
        ],
        [alpha_group(tmp_path)],
    )
    assert run.natives[2] == NativeState("double", 1)  # one click highlights...
    assert seen == [[], [1]]  # ...and only the second chooses


def test_a_stale_chosen_message_is_ignored(mocker, tmp_path):
    """A box's message reaches the session only while its own overlay is still open."""
    first = Picker("x", "First", _entries("Alpha"))
    second = Picker("y", "Second", _entries("Beta"))
    chosen = mocker.patch.object(tsession.DashboardSession, "submit_picker", return_value=None)

    def swapped(app):  # type: ignore[no-untyped-def]
        # The user's choice is queued, then a tick replaces the overlay before it is handled.
        box = app.frame.native_box
        box.post_message(PickerBox.Chosen(box.key, first.entries[0]))
        app.session.overlay = second

    def closed(app):  # type: ignore[no-untyped-def]
        box = app.frame.native_box
        box.post_message(PickerBox.Chosen(box.key, second.entries[0]))
        app.session.overlay = None

    run = drive(mocker, [_open(first), swapped, closed], [alpha_group(tmp_path)])
    chosen.assert_not_called()
    assert run.rc == 0 and run.trace[2].overlay is second


def test_an_option_selection_does_not_leak_past_its_box(mocker, tmp_path):
    seen = []
    mocker.patch.object(
        twidgets.DashboardFrame,
        "on_option_list_option_selected",
        lambda self, event: seen.append(event),
        create=True,
    )
    mocker.patch.object(tsession.DashboardSession, "submit_picker", return_value=None)
    drive(mocker, [_open(Picker("x", "P", _entries("Alpha"))), "enter"], [alpha_group(tmp_path)])
    assert seen == []


# --- the action menus ---------------------------------------------------------------


def _group_row(menu, label: str) -> int:  # type: ignore[no-untyped-def]
    return next(
        i
        for i, item in enumerate(tmenu.menu_entries(menu))
        if isinstance(item, dmenus.MenuGroup) and item.label == label
    )


def test_menu_entries_by_level(tmp_path):
    group = alpha_group(tmp_path)
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    root = tmenu.menu_entries(menu)
    git = next(i for i in root if isinstance(i, dmenus.MenuGroup) and i.label == "Git →")
    assert tmenu.menu_entries(menu, "Git →") == git.actions
    assert tmenu.menu_title(menu, None) == "alpha-x →"
    assert tmenu.menu_title(menu, "Git →") == "alpha-x → Git"


def test_menu_width_covers_every_level(tmp_path):
    menu = tmenu.open_menu([alpha_group(tmp_path)], "alpha-x")
    assert menu is not None
    levels = (
        None,
        *(i.label for i in tmenu.menu_entries(menu) if isinstance(i, dmenus.MenuGroup)),
    )
    widest = max(
        tmenu.menu_option_text(item, key).cell_len
        for level in levels
        for item, key in zip(
            tmenu.menu_entries(menu, level),
            tmenu.menu_hotkeys(tmenu.menu_entries(menu, level)),
            strict=True,
        )
    )
    assert tmenu.menu_width(menu) >= widest
    # ...and the title too, which can be the wider of the two.
    long_title = MenuState("x" * 60, [("A", "a")])
    assert tmenu.menu_width(long_title) >= len("x" * 60 + " →") + 2


def test_a_menu_box_is_as_wide_as_its_widest_level(mocker, tmp_path):
    menu = tmenu.open_menu([alpha_group(tmp_path)], "alpha-x")
    assert menu is not None
    sizes = []
    drive(
        mocker,
        [
            "j",
            "enter",
            lambda app: sizes.append(app.frame.native_box.region.width),
            "g",
            lambda app: sizes.append(app.frame.native_box.region.width),
        ],
        [alpha_group(tmp_path)],
    )
    assert sizes[0] == sizes[1] == tmenu.menu_width(menu) + 5  # opening a level never resizes


def test_a_menu_is_native_with_focus_and_its_title(mocker, tmp_path):
    seen = []
    run = drive(
        mocker,
        [
            "j",
            "enter",
            lambda app: seen.append(
                (type(app.frame.native_box), app.focused, app.frame.native_box.border_title)
            ),
        ],
        [alpha_group(tmp_path)],
        screens=True,
    )
    box_type, focused, title = seen[0]
    assert box_type is MenuBox and focused is not None and focused.id == "native-list"
    assert "alpha-x →" in str(title)
    assert "╭─ alpha-x →" in run.screens[2]
    assert "[t] Attach tmux" in run.screens[2]


def test_j_in_a_menu_moves_only_the_menu(mocker, tmp_path):
    run = drive(mocker, ["j", "enter", "j"], [alpha_group(tmp_path)])
    assert run.natives[3] == NativeState("menu", 1, level=None)
    assert run.trace[3].selected == run.trace[2].selected


def test_ctrl_c_in_a_menu_quits_and_q_closes(mocker, tmp_path):
    quit_run = drive(mocker, ["j", "enter", "ctrl+c"], [alpha_group(tmp_path)])
    assert quit_run.rc == 0 and quit_run.steps_taken == 3
    close_run = drive(mocker, ["j", "enter", "q"], [alpha_group(tmp_path)])
    assert close_run.trace[3].overlay is None and close_run.steps_taken > 3


def test_hotkey_opens_a_group_and_escape_returns_to_its_row(mocker, tmp_path):
    group = alpha_group(tmp_path)
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    git_row = _group_row(menu, "Git →")
    run = drive(mocker, ["j", "enter", "g", "escape"], [group])
    assert run.natives[3] == NativeState("menu", 0, level="Git →")
    assert run.natives[4] == NativeState("menu", git_row, level=None)
    assert git_row > 0  # the row differs from the top, so landing on it proves the restore


def test_a_hotkey_does_not_also_reach_the_dashboard(mocker, tmp_path):
    """`g` opens Git → inside the menu; the table underneath never sees it."""
    run = drive(mocker, ["j", "enter", "g"], [alpha_group(tmp_path)])
    assert run.natives[3].level == "Git →"
    assert run.trace[3].selected == run.trace[2].selected
    assert run.trace[3].notice == run.trace[2].notice


def test_a_menu_hotkey_is_consumed_by_the_menu(mocker, tmp_path):
    """The key stops at the box: it never bubbles on to the app's own key handler."""
    reached = mocker.spy(tapp.DashboardApp, "_on_native_key")
    drive(mocker, ["j", "enter", "g"], [alpha_group(tmp_path)])
    seen = [call.args[1].key for call in reached.call_args_list]
    assert seen and "g" not in seen  # the spy sees the padding Ctrl-C, so it is wired


def test_a_group_row_opens_in_place_on_enter(mocker, tmp_path):
    group = alpha_group(tmp_path)
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    row = _group_row(menu, "Git →")
    run = drive(mocker, ["j", "enter", *["j"] * row, "enter"], [group])
    assert run.natives[2 + row + 1] == NativeState("menu", 0, level="Git →")


def test_escape_at_the_root_closes(mocker, tmp_path):
    run = drive(mocker, ["j", "enter", "escape"], [alpha_group(tmp_path)])
    assert run.trace[3].overlay is None


def test_the_level_shown_survives_a_refresh(mocker, tmp_path):
    tick = lambda app: None  # noqa: E731 - each callable step is followed by a tick
    run = drive(mocker, ["j", "enter", "g", tick, tick], [alpha_group(tmp_path)])
    assert run.natives[4] == run.natives[5] == NativeState("menu", 0, level="Git →")


def test_ticks_keep_a_menus_moved_cursor(mocker, tmp_path):
    """A cursor at the top proves nothing about a refresh: move it first."""
    tick = lambda app: None  # noqa: E731
    run = drive(mocker, ["j", "enter", "j", "j", tick, tick, tick], [alpha_group(tmp_path)])
    assert run.natives[4] == NativeState("menu", 2, level=None)
    assert {run.natives[i] for i in range(4, 8)} == {NativeState("menu", 2, level=None)}


def test_tab_in_a_menu_keeps_focus_level_and_cursor(mocker, tmp_path):
    focused = []
    run = drive(
        mocker,
        [
            "j",
            "enter",
            "j",
            lambda app: app.screen.mount(Input(id="other")),  # somewhere for tab to go
            "tab",
            lambda app: focused.append(app.focused and app.focused.id),
            "j",
        ],
        [alpha_group(tmp_path)],
    )
    assert focused == ["native-list"]
    assert run.natives[3] == NativeState("menu", 1, level=None)
    assert run.natives[5] == run.natives[6] == NativeState("menu", 1, level=None)  # tab: no change
    assert run.natives[7] == NativeState("menu", 2, level=None)  # and `j` still moves the menu


def test_egress_escape_reopens_the_network_level_at_egress(mocker, tmp_path):
    group = alpha_group(tmp_path)
    mocker.patch.object(tsession, "load_egress_rows", return_value=())
    keys = [*container_egress_keys(group), "escape"]
    run = drive(mocker, keys, [group])
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    network = next(
        i
        for i in tmenu.menu_entries(menu)
        if isinstance(i, dmenus.MenuGroup) and i.label == "Network →"
    )
    egress_row = next(i for i, (_, verb) in enumerate(network.actions) if verb == "net egress ls")
    assert egress_row > 0
    assert run.natives[len(keys)] == NativeState("menu", egress_row, level="Network →")


def test_the_menu_gives_way_to_the_egress_panel_and_back(mocker, tmp_path):
    group = alpha_group(tmp_path)
    mocker.patch.object(tsession, "load_egress_rows", return_value=())
    keys = container_egress_keys(group)
    boxes = []
    run = drive(
        mocker,
        [*keys, lambda app: boxes.append(app.frame.native_box), "escape"],
        [group],
    )
    assert isinstance(run.trace[len(keys)].overlay, tsession.EgressState)
    assert isinstance(boxes[0], EgressBox)  # the menu box gave way to the panel's
    assert run.natives[len(keys)] == NativeState("egress", None)
    assert run.natives[len(keys) + 2].kind == "menu"  # Esc brought the menu box back
    assert isinstance(run.trace[len(keys) + 2].overlay, MenuState)


def test_a_repo_menus_egress_escape_reopens_its_network_level(mocker, tmp_path):
    group = alpha_group(tmp_path)
    mocker.patch.object(tsession, "load_egress_rows", return_value=())
    keys = [*repo_menu_keys(group, "net egress ls"), "escape"]
    run = drive(mocker, keys, [group])
    menu = tmenu.open_repo_menu([group], "alpha", frozenset())
    assert menu is not None
    network_row = _group_row(menu, "Network →")
    state = run.natives[len(keys)]
    assert state == NativeState("menu", 0, level="Network →")
    assert isinstance(run.trace[len(keys)].overlay, RepoMenuState)
    assert network_row > 0
    # Esc once more goes up to the group's own row.
    again = drive(mocker, [*keys, "escape"], [group])
    assert again.natives[len(keys) + 1] == NativeState("menu", network_row, level=None)


def test_the_menu_gives_way_to_a_picker(mocker, tmp_path):
    group = alpha_group(tmp_path)
    mocker.patch.object(
        tsession.DashboardSession,
        "open_group_picker",
        return_value=Picker("container-group", "Group", _entries("Alpha"), "alpha-x"),
    )
    keys = open_container_group_picker(group)
    seen = []
    run = drive(
        mocker,
        [*keys, lambda app: seen.append((type(app.frame.native_box), app.focused))],
        [group],
    )
    assert seen[0][0] is PickerBox and seen[0][1] is not None and seen[0][1].id == "native-list"
    assert run.natives[len(keys)] == NativeState("picker", 0)


def test_a_menu_leaf_dispatches_through_the_session(mocker, tmp_path):
    group = alpha_group(tmp_path)
    dispatch = mocker.patch.object(tsession.DashboardSession, "dispatch")
    run = drive(mocker, ["j", "enter", "g", "j", "enter"], [group])
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    verb = tmenu.menu_entries(menu, "Git →")[1][1]  # type: ignore[index]  # a leaf
    dispatch.assert_called_once_with("alpha-x", verb)
    assert run.trace[5].overlay is None


def test_a_stale_menu_choice_is_ignored(mocker, tmp_path):
    """A menu's Chosen reaches the session only while its own menu is still open."""
    group = alpha_group(tmp_path)
    chosen = mocker.patch.object(tsession.DashboardSession, "menu_chosen")

    def closed(app):  # type: ignore[no-untyped-def]
        box = app.frame.native_box
        box.post_message(MenuBox.Chosen(box.key, "tmux", None, 0))
        app.session.overlay = None

    drive(mocker, ["j", "enter", closed], [group])
    chosen.assert_not_called()


def test_a_menu_click_on_a_group_opens_it(mocker, tmp_path):
    group = alpha_group(tmp_path)
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    run = drive(mocker, ["j", "enter", Pick(_group_row(menu, "Git →"))], [group])
    assert run.natives[3] == NativeState("menu", 0, level="Git →")


def test_a_click_while_the_mouse_is_off_opens_nothing(mocker, tmp_path):
    group = alpha_group(tmp_path)
    menu = tmenu.open_menu([group], "alpha-x")
    assert menu is not None
    run = drive(mocker, ["j", "enter", Pick(_group_row(menu, "Git →"))], [group], mouse=False)
    assert run.natives[3] == NativeState("menu", 0, level=None)


def test_menu_hover_paints_grey_without_moving_the_cursor(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")  # the suite sets it, and Textual then strips every colour
    seen = []
    run = drive(
        mocker,
        [
            "j",
            "enter",
            HoverOption(2),
            lambda app: seen.append((backgrounds(app), app.frame.native_state())),
        ],
        [alpha_group(tmp_path)],
    )
    colours, state = seen[0]
    grey = HOVER_STYLE.bgcolor.name
    assert grey in colours and colours <= {"default", grey}
    assert state == NativeState("menu", 0, level=None)
    assert run.natives[3] == NativeState("menu", 0, level=None)


def test_a_menu_opens_on_its_start_group_and_row(mocker, tmp_path):
    group = alpha_group(tmp_path)
    opened = tmenu.open_menu([group], "alpha-x")
    assert opened is not None
    lifecycle_row = _group_row(opened, "Lifecycle →")
    menu = tmenu.MenuState(
        opened.container, opened.actions, start_group="Lifecycle →", start_index=1
    )
    run = drive(mocker, [_open(menu), "escape"], [group])
    assert run.natives[1] == NativeState("menu", 1, level="Lifecycle →")
    assert run.natives[2] == NativeState("menu", lifecycle_row, level=None)


def test_a_start_index_past_the_end_is_clamped(mocker, tmp_path):
    menu = tmenu.MenuState("alpha-x", [("Attach tmux", "tmux"), ("Stop", "stop")], start_index=9)
    run = drive(mocker, [_open(menu)], [alpha_group(tmp_path)])
    assert run.natives[1] == NativeState("menu", 1, level=None)


def test_an_unknown_start_group_opens_at_the_root_top(mocker, tmp_path):
    menu = tmenu.MenuState(
        "alpha-x", [("Attach tmux", "tmux"), ("Stop", "stop")], start_group="Gone →", start_index=1
    )
    run = drive(mocker, [_open(menu)], [alpha_group(tmp_path)])
    assert run.natives[1] == NativeState("menu", 0, level=None)


def test_a_menu_taller_than_the_screen_scrolls_to_its_start_row(mocker, tmp_path):
    menu = tmenu.MenuState("alpha-x", [(f"Action {i}", f"v{i}") for i in range(30)], start_index=25)
    settle = lambda app: None  # noqa: E731 - lets the new box be laid out and scrolled
    run = drive(mocker, [_open(menu), settle], [alpha_group(tmp_path)], size=(80, 20), screens=True)
    assert run.natives[2] == NativeState("menu", 25, level=None)
    assert "Action 25" in run.screens[2] and "Action 0 " not in run.screens[2]


# --- settings ------------------------------------------------------------------


def test_settings_are_native_and_keyed():
    state = ds.open_settings(
        field_names=("name",), enabled=frozenset({"name"}), repo_prefixes=(), folded=frozenset()
    )
    assert is_native(state)
    assert overlay_key(state) == ("settings",)


def test_settings_open_on_the_fields_tab_with_focus(mocker, tmp_path):
    seen = []
    run = drive(
        mocker,
        [
            "S",
            lambda app: seen.append(
                (type(app.frame.native_box), app.focused, app.frame.native_box.border_title)
            ),
        ],
        [alpha_group(tmp_path)],
        screens=True,
    )
    assert run.natives[1] == NativeState("settings", 0, tab="fields")
    box_type, focused, title = seen[0]
    assert box_type is SettingsBox and focused is not None and focused.id == "native-list"
    assert title == "settings"
    assert "Fields" in run.screens[1] and "Repos" in run.screens[1]
    assert "Visibility" in run.screens[1]


def test_tab_switches_tab_and_keeps_focus(mocker, tmp_path):
    focused = []
    run = drive(
        mocker,
        ["S", "j", "tab", "j", lambda app: focused.append(app.focused and app.focused.id)],
        [alpha_group(tmp_path)],
    )
    assert run.natives[2] == NativeState("settings", 1, tab="fields")
    assert run.natives[3] == NativeState("settings", 0, tab="repos")  # the cursor resets
    assert run.natives[4] == NativeState("settings", 0, tab="repos")  # one repo: j stays
    assert focused == ["native-list"]


def test_tab_cycles_through_all_three_tabs_and_back(mocker, tmp_path):
    run = drive(mocker, ["S", "tab", "tab", "tab"], [alpha_group(tmp_path)])
    assert [run.natives[i].tab for i in range(1, 5)] == ["fields", "repos", "visibility", "fields"]


def test_space_toggles_a_column_and_persists(mocker, tmp_path):
    save = mocker.patch.object(tsession, "save_view_state")
    run = drive(mocker, ["S", "j", "space"], [alpha_group(tmp_path)])
    settings = run.trace[3].overlay
    assert isinstance(settings, tsession.SettingsState)
    field = settings.field_names[1]
    assert (field in settings.enabled) != (field in run.trace[2].overlay.enabled)
    save.assert_called()


def test_space_toggles_the_row_under_the_cursor_on_each_tab(mocker, tmp_path):
    save = mocker.patch.object(tsession, "save_view_state")
    run = drive(mocker, ["S", "tab", "space", "tab", "space"], [alpha_group(tmp_path)])
    assert run.trace[3].folded == frozenset({"alpha"})  # Repos row 0
    assert run.trace[5].overlay.show_empty_repos is False  # Visibility row 0
    assert save.call_count == 2


def test_a_refused_toggle_shows_the_checkbox_back_on(mocker, tmp_path):
    mocker.patch.object(tsession, "save_view_state")
    view_state = ViewState(columns=("name",))
    checked = []
    run = drive(
        mocker,
        ["S", "space", lambda app: checked.append(list(app.query_one(NATIVE_LIST).selected))],
        [alpha_group(tmp_path)],
        view_state=view_state,
    )
    assert checked == [["name"]]
    assert run.trace[2].overlay.enabled == frozenset({"name"})


def test_the_checkboxes_follow_the_state_they_show(mocker, tmp_path):
    selected = []
    drive(
        mocker,
        [
            "S",
            lambda app: selected.append(list(app.query_one(NATIVE_LIST).selected)),
            "tab",
            lambda app: selected.append(list(app.query_one(NATIVE_LIST).selected)),
            "tab",
            lambda app: selected.append(list(app.query_one(NATIVE_LIST).selected)),
        ],
        [alpha_group(tmp_path)],
        view_state=ViewState(
            columns=("name", "state"),
            folded=frozenset({"alpha"}),
            show_empty_repos=False,
            hidden_repos=frozenset({"gone"}),
        ),
    )
    assert selected[0] == ["name", "state"]
    assert selected[1] == []  # alpha is folded: unchecked
    assert selected[2] == ["alpha"]  # visible; "Show empty repos" is off


def test_a_toggle_moves_no_cursor_and_ticks_keep_the_state(mocker, tmp_path):
    mocker.patch.object(tsession, "save_view_state")
    tick = lambda app: None  # noqa: E731
    run = drive(mocker, ["S", "j", "j", "space", tick, tick], [alpha_group(tmp_path)])
    assert {run.natives[i] for i in range(3, 7)} == {NativeState("settings", 2, tab="fields")}


def test_clicks_toggle_a_row_and_switch_a_tab(mocker, tmp_path):
    mocker.patch.object(tsession, "save_view_state")
    run = drive(mocker, ["S", PickTab("visibility"), Pick(0)], [alpha_group(tmp_path)])
    assert run.natives[2].tab == "visibility"
    assert run.trace[3].overlay.show_empty_repos != run.trace[2].overlay.show_empty_repos


def test_a_row_click_toggles_that_row_and_highlights_it(mocker, tmp_path):
    mocker.patch.object(tsession, "save_view_state")
    run = drive(mocker, ["S", Pick(2)], [alpha_group(tmp_path)])
    field = run.trace[1].overlay.field_names[2]
    assert (field in run.trace[2].overlay.enabled) != (field in run.trace[1].overlay.enabled)
    assert run.natives[2] == NativeState("settings", 2, tab="fields")


def test_clicking_the_active_tab_changes_nothing(mocker, tmp_path):
    run = drive(mocker, ["S", "j", PickTab("fields")], [alpha_group(tmp_path)])
    assert run.natives[3] == NativeState("settings", 1, tab="fields")


def test_a_tab_click_keeps_the_list_focused(mocker, tmp_path):
    focused = []
    drive(
        mocker,
        ["S", PickTab("repos"), lambda app: focused.append(app.focused and app.focused.id)],
        [alpha_group(tmp_path)],
    )
    assert focused == ["native-list"]


def test_a_tab_click_switches_with_the_mouse_on_and_not_with_it_off(mocker, tmp_path):
    on = drive(mocker, ["S", PickTab("repos")], [alpha_group(tmp_path)])
    assert on.natives[2] == NativeState("settings", 0, tab="repos")  # control
    off = drive(mocker, ["S", PickTab("repos")], [alpha_group(tmp_path)], mouse=False)
    assert off.natives[2] == NativeState("settings", 0, tab="fields")


def test_a_click_on_the_tab_underline_with_the_mouse_off_switches_nothing(mocker, tmp_path):
    def click_underline(app):  # type: ignore[no-untyped-def]
        from jailbee.dashboard.tui.native import Underline

        underline = app.query_one(Underline)
        underline.post_message(Underline.Clicked(underline.region.x + 70))  # far right: last tab

    off = drive(mocker, ["S", click_underline], [alpha_group(tmp_path)], mouse=False)
    assert off.natives[2] == NativeState("settings", 0, tab="fields")


def test_a_click_while_the_mouse_is_off_toggles_nothing(mocker, tmp_path):
    save = mocker.patch.object(tsession, "save_view_state")
    run = drive(mocker, ["S", Pick(1)], [alpha_group(tmp_path)], mouse=False)
    save.assert_not_called()
    assert run.trace[2].overlay == run.trace[1].overlay
    assert run.natives[2] == NativeState("settings", 0, tab="fields")  # not even highlighted


def test_settings_survive_ticks(mocker, tmp_path):
    tick = lambda app: None  # noqa: E731
    run = drive(mocker, ["S", "tab", "tab", "j", *[tick] * 3], [alpha_group(tmp_path)])
    assert {run.natives[i] for i in range(4, 8)} == {NativeState("settings", 1, tab="visibility")}


def test_enter_in_settings_does_nothing(mocker, tmp_path):
    run = drive(mocker, ["S", "enter"], [alpha_group(tmp_path)])
    assert run.trace[2].overlay == run.trace[1].overlay
    assert run.natives[2] == run.natives[1]


def test_escape_closes_settings_and_s_toggles_them_shut(mocker, tmp_path):
    run = drive(mocker, ["S", "escape", "S", "S"], [alpha_group(tmp_path)])
    assert run.trace[2].overlay is None
    assert isinstance(run.trace[3].overlay, tsession.SettingsState)
    assert run.trace[4].overlay is None


def test_ctrl_c_in_settings_quits_and_q_closes(mocker, tmp_path):
    quit_run = drive(mocker, ["S", "ctrl+c"], [alpha_group(tmp_path)])
    assert quit_run.rc == 0 and quit_run.steps_taken == 2
    close_run = drive(mocker, ["S", "q"], [alpha_group(tmp_path)])
    assert close_run.trace[2].overlay is None and close_run.steps_taken > 2


def test_tab_in_settings_never_cycles_the_screens_focus(mocker, tmp_path):
    focused = []
    drive(
        mocker,
        [
            "S",
            lambda app: app.screen.mount(Input(id="other")),
            "tab",
            "tab",
            "tab",
            lambda app: focused.append(app.focused and app.focused.id),
        ],
        [alpha_group(tmp_path)],
    )
    assert focused == ["native-list"]


def test_j_in_settings_moves_only_the_settings_list(mocker, tmp_path):
    run = drive(mocker, ["S", "j", "j"], [named_rows_group(tmp_path, 3)])
    assert run.natives[3] == NativeState("settings", 2, tab="fields")
    assert {view.selected for view in run.trace[1:4]} == {run.trace[1].selected}


def test_settings_swap_in_from_help_and_menu_and_out_to_help(mocker, tmp_path):
    from_help = drive(mocker, ["h", "S", "h"], [alpha_group(tmp_path)])
    assert from_help.natives[2] == NativeState("settings", 0, tab="fields")
    assert from_help.natives[3] == NativeState("help", None)
    from_menu = drive(mocker, ["j", "enter", "S", "j"], [alpha_group(tmp_path)])
    assert from_menu.natives[3] == NativeState("settings", 0, tab="fields")
    assert from_menu.natives[4] == NativeState("settings", 1, tab="fields")  # focus moved over


def test_a_new_settings_overlay_starts_on_the_fields_tab_again(mocker, tmp_path):
    run = drive(mocker, ["S", "tab", "escape", "S"], [alpha_group(tmp_path)])
    assert run.natives[2].tab == "repos"
    assert run.natives[4] == NativeState("settings", 0, tab="fields")


def test_a_stale_toggled_message_is_ignored(mocker, tmp_path):
    toggled = mocker.patch.object(tsession.DashboardSession, "setting_toggled")

    def closed(app):  # type: ignore[no-untyped-def]
        box = app.frame.native_box
        box.post_message(SettingsBox.Toggled(box.key, "fields", "name"))
        app.session.overlay = None

    drive(mocker, ["S", closed], [alpha_group(tmp_path)])
    toggled.assert_not_called()


def test_a_tick_resyncs_a_checkbox_changed_behind_the_box(mocker, tmp_path):
    """`show` re-syncs both ways: a state that turned a column on or off redraws it."""
    mocker.patch.object(tsession, "save_view_state")
    selected = []

    def flip(app):  # type: ignore[no-untyped-def]
        overlay = app.session.overlay
        app.session.overlay = ds.toggle_setting(overlay, "fields", overlay.field_names[0])
        app._painted = None

    drive(
        mocker,
        ["S", flip, lambda app: selected.append(list(app.query_one(NATIVE_LIST).selected))],
        [alpha_group(tmp_path)],
        view_state=ViewState(columns=("name", "state")),
    )
    assert selected[0] == ["state"]


def test_settings_paint_no_background_but_the_hover(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")
    seen = []
    drive(mocker, ["S", lambda app: seen.append(backgrounds(app))], [alpha_group(tmp_path)])
    assert seen[0] and seen[0] <= {"default"}


def _fg(app, text: str):  # type: ignore[no-untyped-def]
    """The foreground ANSI colour number and weight of the first segment holding ``text``."""
    region = app.frame.native_box.region
    for strip in app.screen._compositor.render_strips()[region.y : region.bottom]:
        for segment in strip:
            if text in segment.text and segment.style is not None:
                color = segment.style.color
                return (color.number if color else None), bool(segment.style.bold)
    raise AssertionError(f"{text!r} is not on screen")


def test_the_settings_cursor_row_is_bold_magenta(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")
    seen = []
    drive(
        mocker,
        [
            "S",
            lambda app: seen.append(_fg(app, "name")),
            "j",
            lambda app: seen.append((_fg(app, "name"), _fg(app, "full_name"))),
        ],
        [alpha_group(tmp_path)],
    )
    assert seen[0] == (5, True)  # row 0 is under the cursor
    assert seen[1][0] != (5, True)  # it left row 0 ...
    assert seen[1][1] == (5, True)  # ... and the cursor row is the new one


def test_settings_hover_paints_grey_without_moving_the_cursor(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")
    seen = []
    run = drive(
        mocker,
        ["S", HoverOption(2), lambda app: seen.append([_bg(app, i) for i in range(4)])],
        [alpha_group(tmp_path)],
    )
    grey = HOVER_STYLE.bgcolor.name
    assert seen[0][2] == grey
    assert seen[0][0] != grey and seen[0][1] != grey and seen[0][3] != grey
    assert run.natives[2] == NativeState("settings", 0, tab="fields")


def test_the_highlighted_settings_row_keeps_its_own_style_when_hovered(
    mocker, tmp_path, monkeypatch
):
    monkeypatch.delenv("NO_COLOR")
    seen = []
    drive(
        mocker,
        ["S", HoverOption(0), lambda app: seen.append(_bg(app, 0))],
        [alpha_group(tmp_path)],
    )
    assert seen[0] != HOVER_STYLE.bgcolor.name


def _many_fields(count: int = 40) -> tuple[str, ...]:
    return tuple(f"field{i}" for i in range(count))


def _open_many(app):  # type: ignore[no-untyped-def]
    app.session.overlay = ds.open_settings(
        field_names=_many_fields(),
        enabled=frozenset({"field0"}),
        repo_prefixes=(),
        folded=frozenset(),
    )


def test_one_wheel_notch_scrolls_settings_one_line_and_never_moves_the_cursor(mocker, tmp_path):
    ys = []
    run = drive(
        mocker,
        [
            _open_many,
            Wheel(1, at=NATIVE_LIST),
            lambda app: ys.append(_scroll_y(app)),
            Wheel(1, at=NATIVE_LIST),
            lambda app: ys.append(_scroll_y(app)),
            Wheel(-1, at=NATIVE_LIST),
            lambda app: ys.append(_scroll_y(app)),
        ],
        [named_rows_group(tmp_path, 3)],
        size=(80, 14),
    )
    assert ys == [1, 2, 1]
    assert run.natives[7] == NativeState("settings", 0, tab="fields")


def test_the_settings_wheel_does_nothing_with_the_mouse_off(mocker, tmp_path):
    ys = []
    drive(
        mocker,
        [_open_many, Wheel(1, at=NATIVE_LIST), lambda app: ys.append(_scroll_y(app))],
        [named_rows_group(tmp_path, 3)],
        size=(80, 14),
        mouse=False,
    )
    assert ys == [0]


def test_a_settings_list_taller_than_the_screen_follows_its_cursor(mocker, tmp_path):
    run = drive(
        mocker,
        [_open_many, *["j"] * 30],
        [named_rows_group(tmp_path, 3)],
        size=(80, 16),
        screens=True,
    )
    last = len(run.screens) - 1
    assert run.natives[last] == NativeState("settings", 30, tab="fields")
    assert len(run.screens[last].splitlines()) <= 16
    assert "field30" in run.screens[last] and "field0 " not in run.screens[last]


def test_an_unchecked_setting_is_an_empty_box_even_without_colour(mocker, tmp_path):
    """The toggle button draws an X and tells its state by colour alone; NO_COLOR is on here."""
    run = drive(
        mocker,
        ["S"],
        [alpha_group(tmp_path)],
        view_state=ViewState(columns=("name", "state")),
        screens=True,
    )
    lines = run.screens[1].splitlines()
    name = next(line for line in lines if " name " in line and "▐" in line)
    network = next(line for line in lines if " network " in line and "▐" in line)
    assert "▐X▌ name" in name
    assert "▐ ▌ network" in network


FIRST = (
    EntryRow("from-config.example", "config"),
    EntryRow("container-only.example", "container"),
)


def _egress(mocker, tmp_path, steps, rows=FIRST, **kw):
    """Open the first container's Egress panel over ``rows``, then ``steps``; (run, keys used)."""
    group = alpha_group(tmp_path)
    mocker.patch.object(tsession, "load_egress_rows", return_value=rows)
    opened = container_egress_keys(group)
    return drive(mocker, [*opened, *steps], [group], **kw), len(opened)


def test_the_egress_panel_is_native_and_keyed_by_its_scope():
    assert is_native(tsession.EgressState("alpha", None, ()))
    assert overlay_key(tsession.EgressState("alpha", "alpha-x", FIRST)) == (
        "egress",
        "alpha",
        "alpha-x",
    )
    assert overlay_key(tsession.EgressState("alpha", None, FIRST, start_index=1)) == (
        "egress",
        "alpha",
        None,
    )


def test_the_egress_box_shows_its_scope_rows_and_notes():
    rows = (EntryRow("*.x.io", "local"), EntryRow("*.x.io", "db (legacy)", redundant=True))
    out = "\n".join(box_text(tsession.EgressState("alpha", None, rows), size=(100, 12)))
    assert "Egress · repo" in out
    assert "*.x.io  [local; removes both repo copies]  [proxy]" in out
    assert "(redundant)" in out
    ctr = "\n".join(box_text(tsession.EgressState("alpha", "alpha-x", FIRST), size=(100, 12)))
    assert "Egress · container alpha-x" in ctr and "[container]" in ctr


def test_an_empty_egress_scope_says_so_and_ignores_r(mocker, tmp_path):
    run, n = _egress(mocker, tmp_path, ["r", "j"], rows=())
    assert "No egress entries in this scope." in "\n".join(
        box_text(tsession.EgressState("alpha", None, ()))
    )
    assert run.natives[n] == NativeState("egress", None)
    assert run.natives[n + 2] == NativeState("egress", None)
    assert "Select a removable override first" not in run.notices()


def test_the_egress_cursor_opens_on_the_first_row(mocker, tmp_path):
    run, n = _egress(mocker, tmp_path, [])
    assert run.natives[n] == NativeState("egress", 0)


def test_tab_in_egress_keeps_focus(mocker, tmp_path):
    run, n = _egress(mocker, tmp_path, ["tab", "j"])
    assert run.natives[n + 2] == NativeState("egress", 1)


def test_enter_on_an_egress_row_does_nothing(mocker, tmp_path):
    child = mocker.patch.object(tsession.subprocess, "run")
    run, n = _egress(mocker, tmp_path, ["j", "enter"])
    child.assert_not_called()
    assert run.natives[n + 2] == NativeState("egress", 1)
    assert isinstance(run.trace[n + 2].overlay, tsession.EgressState)


def test_r_removes_the_highlighted_override(mocker, tmp_path):
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    _egress(mocker, tmp_path, ["j", "r"])
    assert child.call_args.args[0] == [
        "jailbee",
        "net",
        "egress",
        "rm",
        "container-only.example",
        "alpha-x",
    ]


def test_r_on_a_config_row_removes_nothing(mocker, tmp_path):
    child = mocker.patch.object(tsession.subprocess, "run")
    run, n = _egress(mocker, tmp_path, ["r"])
    child.assert_not_called()
    assert "Select a removable override first" in run.notices()
    assert isinstance(run.trace[n + 1].overlay, tsession.EgressState)  # the panel stays


def test_a_failed_change_reloads_rows_and_keeps_the_entry(mocker, tmp_path):
    group = alpha_group(tmp_path)
    second = (EntryRow("new-top.example", "config"), *FIRST)
    mocker.patch.object(tsession, "load_egress_rows", side_effect=[FIRST, second, second])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 1
    opened = container_egress_keys(group)
    steps = [*opened, "j", "r", lambda app: None]  # the last delivers the job's result
    run = drive(mocker, steps, [group])
    panel = run.trace[len(steps)].overlay
    assert isinstance(panel, tsession.EgressState) and panel.rows == second
    assert run.natives[len(steps)] == NativeState("egress", 2)  # still container-only.example


def test_a_reload_that_drops_the_entry_clamps_the_cursor(mocker, tmp_path):
    group = alpha_group(tmp_path)
    gone = (FIRST[0],)
    mocker.patch.object(tsession, "load_egress_rows", side_effect=[FIRST, gone, gone])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 1
    steps = [*container_egress_keys(group), "j", "r", lambda app: None]
    run = drive(mocker, steps, [group])
    assert run.natives[len(steps)] == NativeState("egress", 0)


def test_a_reload_to_no_rows_shows_the_empty_note(mocker, tmp_path):
    group = alpha_group(tmp_path)
    mocker.patch.object(tsession, "load_egress_rows", side_effect=[FIRST, (), ()])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 1
    seen: list[list[str]] = []
    steps = [
        *container_egress_keys(group),
        "j",
        "r",
        lambda app: seen.append(
            [str(o.prompt) for o in app.frame.native_box.query_one(NATIVE_LIST)._options]
        ),
    ]
    run = drive(mocker, steps, [group])
    assert seen == [["No egress entries in this scope."]]
    assert run.natives[len(steps)] == NativeState("egress", None)


def test_a_opens_the_question_and_escape_returns_to_the_same_row(mocker, tmp_path):
    run, n = _egress(mocker, tmp_path, ["j", "a", "escape"])
    prompt = run.trace[n + 2].overlay
    assert isinstance(prompt, tsession.TextPrompt) and prompt.purpose == "egress-add"
    assert run.natives[n + 2] is None  # the question is not a box: the panel's is gone
    assert run.natives[n + 3] == NativeState("egress", 1)  # and a new one opens on row 1


def test_a_on_an_empty_scope_still_asks(mocker, tmp_path):
    run, n = _egress(mocker, tmp_path, ["a", "escape"], rows=())
    assert isinstance(run.trace[n + 1].overlay, tsession.TextPrompt)
    assert run.natives[n + 2] == NativeState("egress", None)


def test_egress_hint_names_only_permitted_actions():
    both = tframe._hint_line(tsession.EgressState("alpha", "alpha-x", FIRST))
    assert "add" in both and "remove" in both
    no_add = tframe._hint_line(tsession.EgressState("alpha", "alpha-x", FIRST, can_add=False))
    assert "add" not in no_add and "remove" in no_add
    no_rm = tframe._hint_line(tsession.EgressState("alpha", "alpha-x", FIRST, can_rm=False))
    assert "add" in no_rm and "remove" not in no_rm


def test_one_click_highlights_an_egress_row_and_runs_nothing(mocker, tmp_path):
    child = mocker.patch.object(tsession.subprocess, "run")
    run, n = _egress(mocker, tmp_path, [Pick(1)])
    child.assert_not_called()
    assert run.natives[n + 1] == NativeState("egress", 1)
    assert isinstance(run.trace[n + 1].overlay, tsession.EgressState)


def test_a_double_click_on_an_egress_row_is_a_no_op_enter(mocker, tmp_path):
    child = mocker.patch.object(tsession.subprocess, "run")
    run, n = _egress(mocker, tmp_path, [Pick(1, times=2)])
    child.assert_not_called()
    assert run.natives[n + 1] == NativeState("egress", 1)
    assert isinstance(run.trace[n + 1].overlay, tsession.EgressState)  # no question, no menu


def test_the_egress_list_ignores_clicks_with_the_mouse_off(mocker, tmp_path):
    run, n = _egress(mocker, tmp_path, [Pick(1)], mouse=False)
    assert run.natives[n + 1] == NativeState("egress", 0)


def test_the_egress_box_has_nothing_clickable_but_its_list(mocker, tmp_path):
    seen: list[list[str]] = []
    _egress(
        mocker,
        tmp_path,
        [
            lambda app: seen.append(
                sorted(type(w).__name__ for w in app.frame.native_box.query("*"))
            )
        ],
    )
    assert seen == [["OverlayList"]]


# --- the accounts panel ----------------------------------------------------------


def _accounts_state(rows_json: str = ACCOUNT_ROWS, **kw):  # type: ignore[no-untyped-def]
    return tsession.da.AccountsState(tsession.da.parse_account_rows(rows_json), **kw)


def _many_accounts(count: int) -> str:
    return (
        "["
        + ",".join(
            f'{{"agent": "claude", "group": "g{i}", "account": null, "state": "empty",'
            ' "repos": [], "containers": []}'
            for i in range(count)
        )
        + "]"
    )


def test_accounts_is_native_and_keyed_by_its_repo():
    assert is_native(_accounts_state())
    assert overlay_key(_accounts_state(prefix="alpha")) == ("accounts", "alpha")
    assert overlay_key(_accounts_state(start_index=2, prefix="alpha")) == ("accounts", "alpha")


def test_accounts_is_native_and_enter_opens_actions_for_the_row(mocker, tmp_path):
    fake_accounts_cli(mocker)
    run = drive(mocker, ["A", "j", "enter"], [alpha_group(tmp_path)])
    assert run.natives[2] == NativeState("accounts", 1)
    picker = run.trace[3].overlay
    assert isinstance(picker, tsession.Picker) and picker.purpose == "acct-action"
    assert picker.title == "Login b@x.io~2 (claude)"
    assert picker.back.start_index == 1  # a cancel returns to this row


def test_cancelled_actions_return_to_the_same_row(mocker, tmp_path):
    fake_accounts_cli(mocker)
    run = drive(mocker, ["A", "j", "enter", "escape"], [alpha_group(tmp_path)])
    assert run.natives[4] == NativeState("accounts", 1)


def test_n_asks_for_a_group_and_escape_returns_to_the_row(mocker, tmp_path):
    fake_accounts_cli(mocker)
    run = drive(mocker, ["A", "j", "j", "n", "escape"], [alpha_group(tmp_path)])
    prompt = run.trace[4].overlay
    assert isinstance(prompt, tsession.TextPrompt) and prompt.back.start_index == 2
    assert run.natives[4] is None  # the question is not a box
    assert run.natives[5] == NativeState("accounts", 2)


def test_one_click_selects_and_a_double_click_opens_actions(mocker, tmp_path):
    fake_accounts_cli(mocker)
    run = drive(mocker, ["A", Pick(2), Pick(1, times=2)], [alpha_group(tmp_path)])
    assert run.natives[2] == NativeState("accounts", 2)  # one click only highlights
    assert isinstance(run.trace[2].overlay, tsession.da.AccountsState)
    assert isinstance(run.trace[3].overlay, tsession.Picker)
    assert run.trace[3].overlay.title == "Login b@x.io~2 (claude)"  # row 1, as double-clicked


def test_accounts_clicks_do_nothing_with_the_mouse_off(mocker, tmp_path):
    fake_accounts_cli(mocker)
    run = drive(mocker, ["A", Pick(2), Pick(1, times=2)], [alpha_group(tmp_path)], mouse=False)
    assert run.natives[3] == NativeState("accounts", 0)
    assert isinstance(run.trace[3].overlay, tsession.da.AccountsState)


def test_the_header_stays_while_the_list_scrolls(mocker, tmp_path):
    fake_accounts_cli(mocker, listing=groups_listing(_many_accounts(30)))
    steps = ["A", *["j"] * 25]
    run = drive(mocker, steps, [alpha_group(tmp_path)], size=(100, 24), screens=True)
    assert run.natives[len(steps)] == NativeState("accounts", 25)
    out = run.screens[len(steps)]
    assert "GROUP" in out and "g25" in out  # the header and the cursor row, both on screen
    assert "g0 " not in out  # the list scrolled


def test_a_short_terminal_clips_the_list_not_the_header_or_the_cursor(mocker, tmp_path):
    fake_accounts_cli(mocker, listing=groups_listing(_many_accounts(10)))
    steps = ["A", *["j"] * 9]
    run = drive(mocker, steps, [alpha_group(tmp_path)], size=(100, 14), screens=True)
    out = run.screens[len(steps)]
    assert "GROUP" in out and "g9" in out
    assert "credential groups and logins" in out


def test_the_accounts_box_draws_a_header_over_one_line_per_row():
    out = box_text(_accounts_state(), size=(100, 14))
    assert "credential groups and logins" in out[0]
    assert out[1].split()[1:7] == ["GROUP", "AGENT", "ACCOUNT", "STATE", "USED", "BY"]
    assert "a@x.io#org12345" in out[3] and "alpha, alpha-x" in out[3]
    assert "b@x.io~2" in out[4] and "spare" in out[5]
    assert not out[6].strip("│ ")  # nothing below the three rows


def test_the_accounts_box_fits_a_narrow_terminal():
    out = box_text(_accounts_state(), size=(44, 14))
    assert all(len(line) <= 44 for line in out)
    assert "GROUP" in out[1] and "a@x" in out[3] and "spare" in out[5]


def test_an_empty_accounts_box_says_so_without_a_header():
    out = "\n".join(box_text(_accounts_state("[]"), size=(80, 8)))
    assert "(no logins or groups on this host)" in out
    assert "GROUP" not in out
    lines = box_text(_accounts_state("[]"), size=(80, 8))
    assert "(no logins or groups on this host)" in lines[1]  # right under the border: no header gap


def test_accounts_opens_on_the_remembered_row(mocker, tmp_path):
    fake_accounts_cli(mocker)
    run = drive(mocker, ["A", "j", "j", "n", "escape", "k"], [alpha_group(tmp_path)])
    assert run.natives[6] == NativeState("accounts", 1)  # reopened on row 2, then k


def test_new_rows_from_a_reload_are_drawn_and_keep_the_cursor_on_its_login(mocker, tmp_path):
    fake_accounts_cli(mocker)

    def reload(app):  # type: ignore[no-untyped-def]
        state = app.session.overlay
        fresh = tsession.da.AccountRow("claude", "fresh", None, "empty", (), ())
        # a new first row pushes the highlighted login (b@x.io~2) down by one
        app.session.overlay = replace(state, rows=(fresh, *state.rows))

    steps = ["A", "j", reload]
    run = drive(mocker, steps, [alpha_group(tmp_path)], size=(100, 20), screens=True)
    assert run.natives[3] == NativeState("accounts", 2)  # still on b@x.io~2
    assert "fresh" in run.screens[3]


def test_an_accounts_reload_to_no_rows_shows_the_empty_note(mocker, tmp_path):
    fake_accounts_cli(mocker)

    def clear(app):  # type: ignore[no-untyped-def]
        app.session.overlay = replace(app.session.overlay, rows=())

    run = drive(mocker, ["A", "j", clear], [alpha_group(tmp_path)], size=(100, 20), screens=True)
    assert run.natives[3] == NativeState("accounts", None)
    assert "(no logins or groups on this host)" in run.screens[3]
    assert "GROUP" not in run.screens[3]
    below = run.screens[3].splitlines()
    title = next(i for i, line in enumerate(below) if "credential groups and logins" in line)
    assert "(no logins or groups on this host)" in below[title + 1]


def test_rows_arriving_in_an_empty_accounts_panel_bring_the_header_back(mocker, tmp_path):
    fake_accounts_cli(mocker, listing=groups_listing("[]"))
    fresh = tsession.da.AccountRow("claude", "fresh", None, "empty", (), ())

    def fill(app):  # type: ignore[no-untyped-def]
        app.session.overlay = replace(app.session.overlay, rows=(fresh,))

    run = drive(mocker, ["A", fill], [alpha_group(tmp_path)], size=(100, 20), screens=True)
    assert run.natives[2] == NativeState("accounts", 0)
    assert "GROUP" in run.screens[2] and "fresh" in run.screens[2]
    assert "(no logins or groups on this host)" not in run.screens[2]


def test_a_reload_that_drops_the_login_clamps_the_cursor(mocker, tmp_path):
    fake_accounts_cli(mocker)

    def drop_last_two(app):  # type: ignore[no-untyped-def]
        state = app.session.overlay
        app.session.overlay = replace(state, rows=state.rows[:1])

    run = drive(mocker, ["A", "j", "j", drop_last_two], [alpha_group(tmp_path)])
    assert run.natives[4] == NativeState("accounts", 0)


def test_a_resize_relays_the_rows_out_and_keeps_the_cursor(mocker, tmp_path):
    fake_accounts_cli(mocker)
    steps = ["A", "j", Resize(60, 20), Resize(120, 20)]
    run = drive(mocker, steps, [alpha_group(tmp_path)], screens=True)
    assert run.natives[3] == NativeState("accounts", 1)
    assert run.natives[4] == NativeState("accounts", 1)
    assert "alpha, alpha-x" not in run.screens[3]  # cut short at 60 columns
    assert "alpha, alpha-x" in run.screens[4]  # ...and whole again at 120


def test_the_accounts_header_and_list_paint_no_background_of_their_own(
    mocker, tmp_path, monkeypatch
):
    monkeypatch.delenv("NO_COLOR")
    fake_accounts_cli(mocker)
    seen: list[set[str]] = []
    drive(
        mocker,
        ["A", lambda app: seen.append(backgrounds(app))],
        [alpha_group(tmp_path)],
    )
    assert seen == [{"default"}]


def test_hovering_an_account_row_paints_it_without_moving_the_cursor(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")
    fake_accounts_cli(mocker)
    seen = {}
    run = drive(
        mocker,
        [
            "A",
            HoverOption(2),
            lambda app: seen.update(hovered=[_bg(app, i) for i in range(3)]),
        ],
        [alpha_group(tmp_path)],
    )
    grey = HOVER_STYLE.bgcolor.name
    assert seen["hovered"][2] == grey
    assert grey not in (seen["hovered"][0], seen["hovered"][1])
    assert run.natives[3] == NativeState("accounts", 0)


def test_the_accounts_box_has_nothing_clickable_but_its_list(mocker, tmp_path):
    fake_accounts_cli(mocker)
    seen: list[list[str]] = []
    drive(
        mocker,
        [
            "A",
            lambda app: seen.append(
                sorted(
                    type(w).__name__
                    for w in app.frame.native_box.query("*")
                    if type(w).__name__ != "Static"
                )
            ),
        ],
        [alpha_group(tmp_path)],
    )
    assert seen == [["OverlayList"]]


def test_escape_closes_the_accounts_panel_to_the_table(mocker, tmp_path):
    fake_accounts_cli(mocker)
    run = drive(mocker, ["A", "escape"], [alpha_group(tmp_path)])
    assert run.natives[2] is None and run.trace[2].overlay is None


def test_n_posts_the_highlighted_row_as_the_index(mocker, tmp_path):
    fake_accounts_cli(mocker)
    run = drive(mocker, ["A", "j", "n"], [alpha_group(tmp_path)])
    assert run.trace[3].overlay.back.start_index == 1


def _open_each_box(group):  # type: ignore[no-untyped-def]
    return {
        "help": ["h"],
        "menu": ["j", "enter"],
        "picker": repo_menu_keys(group, "apply"),
        "settings": ["S"],
        "egress": container_egress_keys(group),
        "accounts": ["A"],
    }


@pytest.mark.parametrize("kind", ["help", "menu", "picker", "settings", "egress", "accounts"])
def test_every_native_box_paints_no_background(mocker, tmp_path, monkeypatch, kind):
    monkeypatch.delenv("NO_COLOR")  # else Textual strips every colour and the scan proves nothing
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    fake_accounts_cli(mocker)
    mocker.patch.object(tsession, "load_egress_rows", return_value=FIRST)
    expected_box = {
        "help": "HelpBox",
        "menu": "MenuBox",
        "picker": "PickerBox",
        "settings": "SettingsBox",
        "egress": "EgressBox",
        "accounts": "AccountsBox",
    }[kind]
    seen: list[tuple[set[str], str | None]] = []
    run = drive(
        mocker,
        [
            *_open_each_box(group)[kind],
            lambda app: seen.append(
                (
                    backgrounds(app),
                    None if app.frame.native_box is None else type(app.frame.native_box).__name__,
                )
            ),
        ],
        [group],
    )
    assert run.natives  # the app ran
    scanned, box_kind = seen[0]
    assert box_kind == expected_box  # the right native box was open when the screen was scanned
    assert scanned and scanned <= {"default"}


def test_the_active_settings_tab_is_reversed_and_the_others_are_not(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")
    seen: dict[str, bool | None] = {}

    def probe(app):  # type: ignore[no-untyped-def]
        for strip in app.screen._compositor.render_strips():
            for segment in strip:
                if segment.text.strip() in ("Fields", "Repos", "Visibility"):
                    seen[segment.text.strip()] = segment.style.reverse if segment.style else None

    drive(mocker, ["S", probe], [alpha_group(tmp_path)])
    assert seen["Fields"] is True  # a terminal attribute, not a painted background
    assert not seen["Repos"] and not seen["Visibility"]
