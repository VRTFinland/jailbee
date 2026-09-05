import pytest

pytest.importorskip("PySide6")

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt

from jailbee.dashboard import RepoGroup
from jailbee.lifecycle import ContainerInfo
from jailbee.qtui.window import MainWindow


def _groups():
    running = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip="10.0.0.5", memory_limit="2GB", repo="p"
    )
    stopped = ContainerInfo(
        name="p-bar", state="Stopped", network=None, ip=None, memory_limit=None, repo="p"
    )
    return [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [running, stopped])]


def test_set_groups_populates_tree_with_group_and_containers(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    # One top-level group row with two child container rows.
    root = win.tree.invisibleRootItem()
    assert root.childCount() == 1
    assert root.child(0).childCount() == 2


def test_set_groups_forwards_a_non_default_columns_to_headers(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups(
        _groups(),
        now=datetime.now().astimezone(),
        columns=["name", "created"],
    )
    headers = [win.tree.headerItem().text(i) for i in range(win.tree.columnCount())]
    assert headers == ["NAME", "CREATED"]


def test_menu_labels_match_menu_actions_for_running(qtbot):
    from jailbee.dashboard import MenuContext, RepoGroup, menu_actions

    running = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip="10.0.0.5", memory_limit="2GB", repo="p"
    )
    stopped = ContainerInfo(
        name="p-bar", state="Stopped", network=None, ip=None, memory_limit=None, repo="p"
    )
    # ide_enabled/chrome_enabled differ deliberately so this test proves the
    # window actually threads the group's flags through to menu_actions
    # rather than merely matching two hardcoded defaults.
    groups = [
        RepoGroup(
            "p",
            "/repo",
            Path("/repo/.gie/config.yaml"),
            [running, stopped],
            ide_enabled=True,
            chrome_enabled=False,
        )
    ]

    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups(groups, now=datetime.now().astimezone())
    expected = [
        label
        for label, _ in menu_actions(
            MenuContext(
                state="Running",
                has_repo=True,
                ide_enabled=True,
                chrome_enabled=False,
                current_network="strict",
            )
        )
    ]
    assert win.menu_labels_for("p-foo") == expected
    assert "Launch IDE" in expected
    assert "Launch Chrome" not in expected
    assert "Network: loose" in expected
    assert "Network: strict" not in expected


def test_menu_labels_empty_for_unknown_container(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    assert win.menu_labels_for("does-not-exist") == []


def test_context_menu_on_a_view_only_row_explains_itself(qtbot):
    """The table view must match the card view: an orphan row's right-click
    opens a menu stating why there is nothing to do, instead of nothing."""
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtWidgets import QApplication

    orphan = ContainerInfo(
        name="gamma-x", state="Running", network="strict", ip=None, memory_limit=None, repo="gamma"
    )
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups([RepoGroup("gamma", None, None, [orphan])], now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.invisibleRootItem().child(0).child(0))

    seen: list[tuple[str, bool]] = []

    def interact():
        popup = QApplication.activePopupWidget()
        if popup is None:
            return
        seen.extend((a.text(), a.isEnabled()) for a in popup.actions())
        popup.close()

    QTimer.singleShot(0, interact)
    win._on_context_menu(QPoint(0, 0))

    assert len(seen) == 1
    text, enabled = seen[0]
    assert "gamma" in text
    assert enabled is False


def test_set_groups_colors_state_column_not_name_column(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    root = win.tree.invisibleRootItem()
    running_row = root.child(0).child(0)
    fields_headers = [win.tree.headerItem().text(i) for i in range(win.tree.columnCount())]
    state_col = fields_headers.index("STATE")
    # The NAME column (0) must be left uncoloured; the STATE column carries
    # the state-derived foreground colour.
    assert running_row.foreground(0).color().name() == "#000000"
    assert running_row.foreground(state_col).color().name() != "#000000"


def test_set_refresh_ok_shows_time_and_interval_without_no_git_marker(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    at = datetime(2026, 7, 16, 12, 34, 56).astimezone()
    win.set_refresh_ok(at=at, interval=3.0)
    msg = win.statusBar().currentMessage()
    assert "12:34:56" in msg
    assert "3s" in msg
    assert "no-git" not in msg


def test_set_refresh_ok_shows_no_git_marker_when_git_disabled(qtbot):
    win = MainWindow(git_enabled=False, interval=3.0)
    qtbot.addWidget(win)
    win.set_refresh_ok(at=datetime.now().astimezone(), interval=3.0)
    assert "no-git" in win.statusBar().currentMessage()


def test_set_refresh_ok_shows_manual_when_paused(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    at = datetime(2026, 7, 16, 12, 34, 56).astimezone()
    win.set_refresh_ok(at=at, interval=3.0, paused=True)
    msg = win.statusBar().currentMessage()
    assert "12:34:56" in msg
    assert "manual" in msg
    assert "3s" not in msg


def test_set_refresh_failed_shows_non_modal_status(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_refresh_failed("boom")
    msg = win.statusBar().currentMessage()
    assert "boom" in msg
    assert "failed" in msg.lower()


def test_window_title_stays_constant(qtbot):
    """The Qt window has no row selection to name, so the title is constant —
    it only has to be recognisable in a taskbar."""
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    assert win.windowTitle() == "\N{HONEYBEE} JailBee dashboard"
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.set_refresh_ok(at=datetime.now().astimezone(), interval=3.0)
    win.set_refresh_failed("boom")
    assert win.windowTitle() == "\N{HONEYBEE} JailBee dashboard"


def test_refresh_menu_has_expected_actions(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    labels = [a.text().replace("&", "") for a in win.refresh_menu.actions() if not a.isSeparator()]
    assert labels == ["Refresh now", "1s", "2s", "3s", "5s", "10s", "30s", "Off (manual)"]


def test_refresh_menu_checks_matching_launch_interval(qtbot):
    win = MainWindow(git_enabled=True, interval=5.0)
    qtbot.addWidget(win)
    checked = [a.text() for a in win.refresh_menu.actions() if a.isCheckable() and a.isChecked()]
    assert checked == ["5s"]


def test_refresh_now_action_emits_refresh_requested(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    action = next(a for a in win.refresh_menu.actions() if a.text() == "Refresh now")
    assert action.shortcut().toString() == "F5"
    with qtbot.waitSignal(win.refreshRequested, timeout=1000):
        action.trigger()


def test_preset_action_emits_interval_changed(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    action = next(a for a in win.refresh_menu.actions() if a.text() == "10s")
    with qtbot.waitSignal(win.intervalChanged, timeout=1000) as blocker:
        action.trigger()
    assert blocker.args == [10.0]


def test_off_manual_action_emits_auto_refresh_disabled(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    action = next(a for a in win.refresh_menu.actions() if a.text() == "Off (manual)")
    with qtbot.waitSignal(win.autoRefreshDisabled, timeout=1000):
        action.trigger()


def test_default_layout_is_cards_and_stack_shows_card_view(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    assert win.current_layout() == "cards"
    assert win.stack.currentWidget() is win.card_view


def test_initial_layout_table_selected(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0, layout="table")
    qtbot.addWidget(win)
    assert win.current_layout() == "table"
    assert win.stack.currentWidget() is win.tree


def test_view_menu_switch_emits_and_changes_stack(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0, layout="cards")
    qtbot.addWidget(win)
    table_action = next(a for a in win.view_menu.actions() if a.text() == "Table")
    with qtbot.waitSignal(win.layoutChanged, timeout=1000) as blocker:
        table_action.trigger()
    assert blocker.args == ["table"]
    assert win.current_layout() == "table"
    assert win.stack.currentWidget() is win.tree


def test_card_style_lives_in_its_own_menu_not_the_view_menu(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)

    view_labels = {a.text() for a in win.view_menu.actions()}
    assert "Compact" not in view_labels and "Grid" not in view_labels

    style_labels = {a.text() for a in win.card_style_menu.actions()}
    assert "Compact" in style_labels and "Grid" in style_labels


def test_card_style_menu_emits_and_switches(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)

    acts = {a.text(): a for a in win.card_style_menu.actions()}
    with qtbot.waitSignal(win.cardStyleChanged, timeout=1000) as blocker:
        acts["Grid"].trigger()
    assert blocker.args == ["grid"]
    assert win.current_card_style() == "grid"
    assert win.card_view.card_style() == "grid"


def test_card_style_menu_is_hidden_in_table_view(qtbot):
    # Starts hidden when the initial layout is the table.
    win = MainWindow(git_enabled=True, interval=3.0, layout="table")
    qtbot.addWidget(win)
    assert not win.card_style_menu.menuAction().isVisible()

    # Becomes visible when switching to cards, hidden again on switch back.
    cards_action = next(a for a in win.view_menu.actions() if a.text() == "Cards")
    cards_action.trigger()
    assert win.card_style_menu.menuAction().isVisible()

    table_action = next(a for a in win.view_menu.actions() if a.text() == "Table")
    table_action.trigger()
    assert not win.card_style_menu.menuAction().isVisible()


def test_card_style_menu_visible_when_initial_layout_is_cards(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0, layout="cards")
    qtbot.addWidget(win)
    assert win.card_style_menu.menuAction().isVisible()


def test_set_groups_populates_both_views(qtbot):
    from jailbee.qtui.cards import _Card

    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    # Tree still populated (group + 2 children) ...
    root = win.tree.invisibleRootItem()
    assert root.child(0).childCount() == 2
    # ... and the card view has one card per container.
    assert len(win.card_view.findChildren(_Card)) == 2


def test_header_state_round_trips(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    saved = win.table_header_state()
    assert isinstance(saved, str) and saved

    # A fresh window given that state restores it after its first set_groups.
    win2 = MainWindow(git_enabled=True, interval=3.0, header_state=saved)
    qtbot.addWidget(win2)
    win2.set_groups(_groups(), now=datetime.now().astimezone())
    assert win2.table_header_state() == saved


def test_table_header_state_returns_pending_before_first_set_groups(qtbot):
    """If _persist() runs before the first set_groups (e.g. the user
    switches to Cards or closes the window immediately after launch), a
    restored header_state must not be clobbered by the tree's live
    (still-default) header state."""
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    saved = win.table_header_state()

    win2 = MainWindow(git_enabled=True, interval=3.0, header_state=saved)
    qtbot.addWidget(win2)
    # No set_groups yet: table_header_state() must return the pending
    # (restored-but-not-yet-applied) value, not the tree's live default.
    assert win2.table_header_state() == saved


def test_paused_checks_off_manual_in_refresh_menu(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0, paused=True)
    qtbot.addWidget(win)
    checked = [a.text() for a in win.refresh_menu.actions() if a.isCheckable() and a.isChecked()]
    assert checked == ["Off (manual)"]


def test_columns_menu_reflects_the_enabled_set(qtbot):
    from jailbee.dashboard import all_column_names, dynamic_column_names

    win = MainWindow(git_enabled=True, interval=3.0, enabled_columns=("name", "state"))
    qtbot.addWidget(win)
    checked = {a.text() for a in win.columns_menu.actions() if a.isChecked()}

    assert checked == {"name", "state"}
    dynamic = dynamic_column_names()
    expected_labels = {
        f"{name} (shown only when it applies)" if name in dynamic else name
        for name in all_column_names()
    }
    assert {a.text() for a in win.columns_menu.actions()} == expected_labels


def test_columns_menu_marks_the_dynamic_columns(qtbot):
    """A GUI user who ticks `pr` with no PR container must see why nothing
    appeared — mirroring the TUI overlay's `(shown only when it applies)`
    suffix, driven by the same `dynamic_column_names()` this
    branch's TUI already uses. Fails if the menu goes back to a bare
    `menu.addAction(name)` per column."""
    from jailbee.dashboard import dynamic_column_names

    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    labels_by_name = {}
    for name in dynamic_column_names():
        act = next(a for a in win.columns_menu.actions() if a.text().startswith(name))
        labels_by_name[name] = act.text()

    for name, label in labels_by_name.items():
        assert label != name  # not a bare, unmarked action
        assert "when it applies" in label

    # enabled_columns() must still return bare names — nothing downstream
    # (view_prefs, visible_fields) should ever see the decorated text.
    assert all("(shown only" not in n for n in win.enabled_columns())


def test_toggling_a_columns_action_emits_and_updates(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0, enabled_columns=("name", "state"))
    qtbot.addWidget(win)
    seen: list[int] = []
    win.columnsChanged.connect(lambda: seen.append(1))

    act = next(a for a in win.columns_menu.actions() if a.text() == "ip")
    # `act.trigger()` toggles the checkable action AND emits `triggered(checked)`
    # in one call — the same effect `setChecked` + `triggered.emit(bool)` would
    # have, but the latter is unusable here: this PySide6 build (6.11.1)
    # resolves a bare `.emit(True)` on an overloaded `triggered` signal to its
    # zero-arg overload and raises (`triggered() only accepts 0 argument(s)`),
    # confirmed with a bare `QAction` outside any of this module's code.
    act.trigger()

    assert "ip" in win.enabled_columns()
    assert seen  # the controller is told, so it can persist


def test_the_last_column_cannot_be_unchecked(qtbot):
    """Same rule as the TUI overlay: a table with no columns looks broken."""
    win = MainWindow(git_enabled=True, interval=3.0, enabled_columns=("name",))
    qtbot.addWidget(win)
    act = next(a for a in win.columns_menu.actions() if a.text() == "name")
    act.trigger()  # see the note in test_toggling_a_columns_action_emits_and_updates

    assert win.enabled_columns() == ("name",)


def test_a_stale_persisted_column_name_cannot_reach_zero_columns(qtbot):
    """A persisted set can contain a name from a renamed/removed column
    (``decode_names`` only validates JSON shape, not column vocabulary).
    Unfiltered, that phantom would inflate the stored length past 1 without
    the last-column guard noticing, and then get dropped by `_toggle_column`'s
    own filtering anyway — reaching zero real columns from a single toggle.
    The window must filter it out at construction instead, so only the one
    real name remains and the ordinary last-column guard protects it."""
    win = MainWindow(git_enabled=True, interval=3.0, enabled_columns=("name", "old_removed_col"))
    qtbot.addWidget(win)
    act = next(a for a in win.columns_menu.actions() if a.text() == "name")
    act.trigger()

    # Written as an equality against the real survivor, not a truthiness or
    # membership check, so it fails on the empty-tuple outcome specifically —
    # not merely on the phantom name still being present somewhere.
    assert win.enabled_columns() == ("name",)
    assert act.isChecked() is True  # the action snaps back


def test_selected_prefix_of_a_group_row(qtbot):
    # Table mode: the default is cards, which reads the card view's own
    # selection instead — see test_selected_prefix_cards_mode_* below.
    win = MainWindow(git_enabled=True, interval=3.0, layout="table")
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0))
    assert win._selected_prefix() == "p"


def test_selected_prefix_of_a_container_row_is_its_parents(qtbot):
    """A container row carries a name, not a prefix — the repo is the
    parent's, and creating alongside a container must still find it."""
    win = MainWindow(git_enabled=True, interval=3.0, layout="table")
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0).child(0))
    assert win._selected_prefix() == "p"


def test_selected_prefix_is_none_without_a_selection_and_two_groups(qtbot):
    """No selection at all, and more than one configured group: the
    single-repo fallback must not kick in and guess wrong."""
    win = MainWindow(git_enabled=True, interval=3.0, layout="table")
    qtbot.addWidget(win)
    groups = [*_groups(), RepoGroup("q", "/repo2", Path("/repo2/.gie/config.yaml"), [])]
    win.set_groups(groups, now=datetime.now().astimezone())
    win.tree.setCurrentItem(None)
    assert win._selected_prefix() is None


def test_selected_prefix_falls_back_to_the_sole_configured_group(qtbot):
    """No selection at all, but exactly one configured group: a single-repo
    user must never hit an unsatisfiable "select a repo" prompt."""
    win = MainWindow(git_enabled=True, interval=3.0, layout="table")
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(None)
    assert win._selected_prefix() == "p"


def test_selected_prefix_falls_back_to_a_sole_scratch_group(qtbot):
    """A repo with no config file is still addressable — it is a real root the
    child can run in — so the single-repo fallback must resolve to it."""
    win = MainWindow(git_enabled=True, interval=3.0, layout="table")
    qtbot.addWidget(win)
    win.set_groups([RepoGroup("s", "/scratch", None, [])], now=datetime.now().astimezone())
    win.tree.setCurrentItem(None)
    assert win._selected_prefix() == "s"


def test_selected_prefix_ignores_a_sole_orphan_group(qtbot):
    """An orphan has no repo root at all, so there is nothing to create
    against and the fallback must stay silent."""
    win = MainWindow(git_enabled=True, interval=3.0, layout="table")
    qtbot.addWidget(win)
    win.set_groups([RepoGroup("gamma", None, None, [])], now=datetime.now().astimezone())
    win.tree.setCurrentItem(None)
    assert win._selected_prefix() is None


def test_selected_prefix_cards_mode_resolves_the_selected_card(qtbot):
    """The bug this fix closes: cards is the default layout, and the tree
    carries no selection there at all — Ctrl+N must resolve from the card
    view's own selection instead."""
    win = MainWindow(git_enabled=True, interval=3.0, layout="cards")
    qtbot.addWidget(win)
    groups = [*_groups(), RepoGroup("q", "/repo2", Path("/repo2/.gie/config.yaml"), [])]
    win.set_groups(groups, now=datetime.now().astimezone())
    card = win.card_view._cards["p-bar"]
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert win.card_view.selected_name() == "p-bar"
    assert win._selected_prefix() == "p"


def test_selected_prefix_cards_mode_none_selected_two_groups(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0, layout="cards")
    qtbot.addWidget(win)
    groups = [*_groups(), RepoGroup("q", "/repo2", Path("/repo2/.gie/config.yaml"), [])]
    win.set_groups(groups, now=datetime.now().astimezone())
    assert win._selected_prefix() is None


def test_selected_prefix_cards_mode_none_selected_one_group_falls_back(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0, layout="cards")
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    assert win._selected_prefix() == "p"


def test_container_menu_offers_new(qtbot):
    """Read the menu off the window, never via `menuBar().actions()` ->
    `QAction.menu()`: that wrapper dies with the loop-local QAction (see
    `_build_refresh_menu`'s docstring)."""
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    assert win.container_menu.title() == "&Container"
    assert win.new_container_action.text() == "&New…"


def test_container_menu_new_emits_the_selected_prefix(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0).child(0))

    with qtbot.waitSignal(win.newContainerRequested, timeout=1000) as blocker:
        win.new_container_action.trigger()

    assert blocker.args == ["p"]


def test_container_menu_new_emits_empty_string_without_a_selection(qtbot):
    """The window reports what it knows; the controller owns the message."""
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)

    with qtbot.waitSignal(win.newContainerRequested, timeout=1000) as blocker:
        win.new_container_action.trigger()

    assert blocker.args == [""]


def test_group_row_context_menu_offers_new_container(qtbot):
    """The group-row branch of `_on_context_menu` is the other entry point
    into container creation (alongside the &Container menu) — right-clicking
    a repo header must offer the same action and emit the same signal.

    `QMenu.exec` is modal (blocks pumping a real event loop), so — following
    `test_context_menu_on_a_view_only_row_explains_itself`'s established
    pattern in this file — a zero-delay `QTimer` fires once the offscreen
    popup is up, reads its actions, and triggers the one we want, which lets
    `exec` return instead of hanging. Patching `QMenu.exec` directly does not
    work here: PySide6's compiled binding does not honor a class-level
    monkeypatch of it, so the call would go through to the real (blocking)
    implementation regardless.
    """
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtWidgets import QApplication

    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0))
    assert win._selected_name() is None
    assert win._selected_prefix() == "p"

    seen: list[str] = []

    def interact() -> None:
        popup = QApplication.activePopupWidget()
        if popup is None:
            return
        actions = popup.actions()
        seen.extend(a.text() for a in actions)
        actions[0].trigger()
        # trigger() alone doesn't dismiss the offscreen-platform modal popup
        # (unlike a real click), so exec() would otherwise never return.
        popup.close()

    with qtbot.waitSignal(win.newContainerRequested, timeout=1000) as blocker:
        QTimer.singleShot(0, interact)
        win._on_context_menu(QPoint(0, 0))

    assert seen == ["New container…"]
    assert blocker.args == ["p"]


def test_config_menu_emits_the_selected_prefix(qtbot):
    win = MainWindow(git_enabled=True, interval=3.0)
    qtbot.addWidget(win)
    received: list[tuple[str, bool]] = []
    win.configEditRequested.connect(lambda p, g: received.append((p, g)))

    win.edit_repo_config_action.trigger()
    win.edit_global_config_action.trigger()

    assert [g for _p, g in received] == [False, True]
