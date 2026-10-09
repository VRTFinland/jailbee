import pytest

pytest.importorskip("PySide6")

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, Qt

from jailbee.dashboard.model import RepoGroup
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
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    # One top-level group row with two child container rows.
    root = win.tree.invisibleRootItem()
    assert root.childCount() == 1
    assert root.child(0).childCount() == 2


def test_set_groups_forwards_a_non_default_columns_to_headers(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(
        _groups(),
        now=datetime.now().astimezone(),
        columns=["name", "created"],
    )
    headers = [win.tree.headerItem().text(i) for i in range(win.tree.columnCount())]
    assert headers == ["NAME", "AGE"]


def test_menu_labels_match_menu_actions_for_running(qtbot):
    from jailbee.dashboard.menus import MenuContext, MenuGroup, group_menu_actions, menu_actions
    from jailbee.dashboard.model import AppMenuEntry, RepoGroup

    running = ContainerInfo(
        name="p-foo", state="Running", network="strict", ip="10.0.0.5", memory_limit="2GB", repo="p"
    )
    stopped = ContainerInfo(
        name="p-bar", state="Stopped", network=None, ip=None, memory_limit=None, repo="p"
    )
    # apps carries only "ide" (not "chrome") deliberately so this test proves
    # the window actually threads the group's apps through to menu_actions
    # rather than merely matching two hardcoded defaults.
    ide_entry = AppMenuEntry("ide", "JetBrains idea")
    groups = [
        RepoGroup(
            "p",
            "/repo",
            Path("/repo/.gie/config.yaml"),
            [running, stopped],
            apps=[ide_entry],
        )
    ]

    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(groups, now=datetime.now().astimezone())
    expected = [
        item.label if isinstance(item, MenuGroup) else item[0]
        for item in group_menu_actions(
            menu_actions(
                MenuContext(
                    state="Running",
                    has_repo=True,
                    apps=[ide_entry],
                    current_network="strict",
                )
            )
        )
    ]
    assert win.menu_labels_for("p-foo") == expected
    assert expected[:6] == ["Attach tmux", "Open shell", "Outbox", "Launch →", "PR →", "Git →"]
    assert "Launch chrome" not in expected
    assert "Network: loose" in expected
    assert "Network: strict" not in expected


@pytest.mark.parametrize("config_path", [Path("/repo/.jailbee/config.yaml"), None])
def test_outbox_browse_launches_in_terminal_with_repo_target(config_path):
    from jailbee.dashboard.model import RepoTarget
    from jailbee.qtui.actions import (
        TerminalNotFoundError,
        build_action,
        launch_mode,
        resolve_launch,
    )
    from jailbee.qtui.terminal import TerminalSpec

    target = RepoTarget(Path("/repo"), config_path)
    action = build_action("outbox browse", "p-foo", target)
    expected = ["jailbee", "outbox", "browse", "p-foo"]
    if config_path is not None:
        expected += ["--config", str(config_path)]

    assert launch_mode("outbox browse") == "terminal"
    assert action.launch == "terminal"
    assert action.argv == expected
    assert action.cwd == Path("/repo")
    assert action.verb == "outbox browse"
    assert action.confirm is False
    assert resolve_launch(action, TerminalSpec("xterm", ["-e"])) == ["xterm", "-e", *expected]
    with pytest.raises(TerminalNotFoundError):
        resolve_launch(action, None)


def test_menu_labels_empty_for_unknown_container(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    assert win.menu_labels_for("does-not-exist") == []


def test_table_context_menu_submenus_dispatch_leaf_verb(qtbot):
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtWidgets import QApplication, QMenu

    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0).child(0))
    seen = []
    win.actionRequested.connect(lambda verb, name: seen.append((verb, name)))
    root_labels = []
    git_labels = []

    def interact():
        popup = QApplication.activePopupWidget()
        if not isinstance(popup, QMenu):
            return
        root = popup.actions()
        root_labels.extend(action.text() for action in root)
        outbox = next(action for action in root if action.text() == "Outbox")
        assert outbox.menu() is None
        outbox.trigger()
        git_action = next((action for action in root if action.text() == "Git →"), None)
        if git_action is not None and (git := git_action.menu()) is not None:
            git_labels.extend(action.text() for action in git.actions())
            git.actions()[0].trigger()
        popup.close()

    QTimer.singleShot(0, interact)
    win._on_context_menu(QPoint(0, 0))
    assert root_labels[:5] == ["Attach tmux", "Open shell", "Outbox", "PR →", "Git →"]
    assert git_labels[:2] == ["Merge into…", "Send commits to host (git pull)"]
    assert seen == [("outbox browse", "p-foo"), ("merge", "p-foo")]


def test_context_menu_on_a_view_only_row_explains_itself(qtbot):
    """The table view must match the card view: an orphan row's right-click
    opens a menu stating why there is nothing to do, instead of nothing."""
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtWidgets import QApplication

    orphan = ContainerInfo(
        name="gamma-x", state="Running", network="strict", ip=None, memory_limit=None, repo="gamma"
    )
    win = MainWindow()
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
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    root = win.tree.invisibleRootItem()
    running_row = root.child(0).child(0)
    fields_headers = [win.tree.headerItem().text(i) for i in range(win.tree.columnCount())]
    state_col = fields_headers.index("ST")
    assert running_row.text(state_col) == "▶"
    # The NAME column (0) must be left uncoloured; the ST column carries
    # the state-derived foreground colour.
    assert running_row.foreground(0).color().name() == "#000000"
    assert running_row.foreground(state_col).color().name() != "#000000"


def test_set_refresh_ok_shows_time_without_no_git_marker(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    at = datetime(2026, 7, 16, 12, 34, 56).astimezone()
    win.set_refresh_ok(at=at, git_enabled=True)
    assert win.statusBar().currentMessage() == "Last refresh 12:34:56"


def test_set_refresh_ok_shows_no_git_marker_when_git_disabled(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    at = datetime(2026, 7, 16, 12, 34, 56).astimezone()
    win.set_refresh_ok(at=at, git_enabled=False)
    assert win.statusBar().currentMessage() == "Last refresh 12:34:56  ·  (no-git)"


def test_set_refresh_failed_shows_non_modal_status(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_refresh_failed("refresh failed: boom")
    assert win.statusBar().currentMessage() == "refresh failed: boom"


def test_window_title_stays_constant(qtbot):
    """The Qt window has no row selection to name, so the title is constant —
    it only has to be recognisable in a taskbar."""
    win = MainWindow()
    qtbot.addWidget(win)
    assert win.windowTitle() == "\N{HONEYBEE} JailBee dashboard"
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.set_refresh_ok(at=datetime.now().astimezone(), git_enabled=True)
    win.set_refresh_failed("boom")
    assert win.windowTitle() == "\N{HONEYBEE} JailBee dashboard"


def test_refresh_menu_offers_only_refresh_now(qtbot):
    """The cadence is the state service's (`dashboard.refresh` in the global
    config), so the menu has no presets and no pause any more."""
    win = MainWindow()
    qtbot.addWidget(win)
    assert [a.text() for a in win.refresh_menu.actions()] == ["Refresh now"]


def test_refresh_now_action_emits_refresh_requested(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    action = next(a for a in win.refresh_menu.actions() if a.text() == "Refresh now")
    assert action.shortcut().toString() == "F5"
    with qtbot.waitSignal(win.refreshRequested, timeout=1000):
        action.trigger()


def test_minimising_the_window_emits_inactive(qtbot, mocker):
    win = MainWindow()
    qtbot.addWidget(win)
    mocker.patch.object(win, "isMinimized", return_value=True)
    with qtbot.waitSignal(win.activeChanged, timeout=1000) as blocker:
        win.changeEvent(QEvent(QEvent.Type.WindowStateChange))
    assert blocker.args == [False]


def test_restoring_the_window_emits_active(qtbot, mocker):
    win = MainWindow()
    qtbot.addWidget(win)
    mocker.patch.object(win, "isMinimized", return_value=False)
    with qtbot.waitSignal(win.activeChanged, timeout=1000) as blocker:
        win.changeEvent(QEvent(QEvent.Type.WindowStateChange))
    assert blocker.args == [True]


def test_other_change_events_emit_nothing(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    with qtbot.assertNotEmitted(win.activeChanged, wait=100):
        win.changeEvent(QEvent(QEvent.Type.WindowTitleChange))


def test_default_layout_is_cards_and_stack_shows_card_view(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    assert win.current_layout() == "cards"
    assert win.stack.currentWidget() is win.card_view


def test_initial_layout_table_selected(qtbot):
    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    assert win.current_layout() == "table"
    assert win.stack.currentWidget() is win.tree


def test_view_menu_switch_emits_and_changes_stack(qtbot):
    win = MainWindow(layout="cards")
    qtbot.addWidget(win)
    table_action = next(a for a in win.view_menu.actions() if a.text() == "Table")
    with qtbot.waitSignal(win.layoutChanged, timeout=1000) as blocker:
        table_action.trigger()
    assert blocker.args == ["table"]
    assert win.current_layout() == "table"
    assert win.stack.currentWidget() is win.tree


def test_card_style_lives_in_its_own_menu_not_the_view_menu(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)

    view_labels = {a.text() for a in win.view_menu.actions()}
    assert "Compact" not in view_labels and "Grid" not in view_labels

    style_labels = {a.text() for a in win.card_style_menu.actions()}
    assert "Compact" in style_labels and "Grid" in style_labels


def test_card_style_menu_emits_and_switches(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)

    acts = {a.text(): a for a in win.card_style_menu.actions()}
    with qtbot.waitSignal(win.cardStyleChanged, timeout=1000) as blocker:
        acts["Grid"].trigger()
    assert blocker.args == ["grid"]
    assert win.current_card_style() == "grid"
    assert win.card_view.card_style() == "grid"


def test_card_style_menu_is_hidden_in_table_view(qtbot):
    # Starts hidden when the initial layout is the table.
    win = MainWindow(layout="table")
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
    win = MainWindow(layout="cards")
    qtbot.addWidget(win)
    assert win.card_style_menu.menuAction().isVisible()


def test_set_groups_populates_both_views(qtbot):
    from jailbee.qtui.cards import _Card

    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    # Tree still populated (group + 2 children) ...
    root = win.tree.invisibleRootItem()
    assert root.child(0).childCount() == 2
    # ... and the card view has one card per container.
    assert len(win.card_view.findChildren(_Card)) == 2


def test_header_state_round_trips(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    saved = win.table_header_state()
    assert isinstance(saved, str) and saved

    # A fresh window given that state restores it after its first set_groups.
    win2 = MainWindow(header_state=saved)
    qtbot.addWidget(win2)
    win2.set_groups(_groups(), now=datetime.now().astimezone())
    assert win2.table_header_state() == saved


def test_table_header_state_returns_pending_before_first_set_groups(qtbot):
    """If _persist() runs before the first set_groups (e.g. the user
    switches to Cards or closes the window immediately after launch), a
    restored header_state must not be clobbered by the tree's live
    (still-default) header state."""
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    saved = win.table_header_state()

    win2 = MainWindow(header_state=saved)
    qtbot.addWidget(win2)
    # No set_groups yet: table_header_state() must return the pending
    # (restored-but-not-yet-applied) value, not the tree's live default.
    assert win2.table_header_state() == saved


def test_columns_menu_reflects_the_enabled_set(qtbot):
    from jailbee.dashboard.columns import all_column_names, dynamic_column_names

    win = MainWindow(enabled_columns=("name", "state"))
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
    from jailbee.dashboard.columns import dynamic_column_names

    win = MainWindow()
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
    win = MainWindow(enabled_columns=("name", "state"))
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


def test_toggling_a_column_on_appends_it(qtbot):
    win = MainWindow(enabled_columns=("name", "state", "created"))
    qtbot.addWidget(win)
    # "mode" sits canonically before "state": a canonical re-sort would put it second.
    act = next(a for a in win.columns_menu.actions() if a.text().startswith("mode"))
    act.trigger()  # see the note in test_toggling_a_columns_action_emits_and_updates
    assert win.enabled_columns() == ("name", "state", "created", "mode")


def test_a_restored_column_order_is_kept(qtbot):
    win = MainWindow(enabled_columns=("name", "created", "state"))
    qtbot.addWidget(win)
    assert win.enabled_columns() == ("name", "created", "state")


def test_a_restored_order_without_name_first_gets_name_first_and_keeps_the_rest(qtbot):
    win = MainWindow(enabled_columns=("created", "state", "name", "mode"))
    qtbot.addWidget(win)
    assert win.enabled_columns() == ("name", "created", "state", "mode")


def test_the_last_column_cannot_be_unchecked(qtbot):
    """Same rule as the TUI overlay: a table with no columns looks broken."""
    win = MainWindow(enabled_columns=("name",))
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
    win = MainWindow(enabled_columns=("name", "old_removed_col"))
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
    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0))
    assert win._selected_prefix() == "p"


def test_selected_prefix_of_a_container_row_is_its_parents(qtbot):
    """A container row carries a name, not a prefix — the repo is the
    parent's, and creating alongside a container must still find it."""
    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0).child(0))
    assert win._selected_prefix() == "p"


def test_selected_prefix_is_none_without_a_selection_and_two_groups(qtbot):
    """No selection at all, and more than one configured group: the
    single-repo fallback must not kick in and guess wrong."""
    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    groups = [*_groups(), RepoGroup("q", "/repo2", Path("/repo2/.gie/config.yaml"), [])]
    win.set_groups(groups, now=datetime.now().astimezone())
    win.tree.setCurrentItem(None)
    assert win._selected_prefix() is None


def test_selected_prefix_falls_back_to_the_sole_configured_group(qtbot):
    """No selection at all, but exactly one configured group: a single-repo
    user must never hit an unsatisfiable "select a repo" prompt."""
    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(None)
    assert win._selected_prefix() == "p"


def test_selected_prefix_falls_back_to_a_sole_scratch_group(qtbot):
    """A repo with no config file is still addressable — it is a real root the
    child can run in — so the single-repo fallback must resolve to it."""
    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups([RepoGroup("s", "/scratch", None, [])], now=datetime.now().astimezone())
    win.tree.setCurrentItem(None)
    assert win._selected_prefix() == "s"


def test_selected_prefix_ignores_a_sole_orphan_group(qtbot):
    """An orphan has no repo root at all, so there is nothing to create
    against and the fallback must stay silent."""
    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups([RepoGroup("gamma", None, None, [])], now=datetime.now().astimezone())
    win.tree.setCurrentItem(None)
    assert win._selected_prefix() is None


def test_selected_prefix_cards_mode_resolves_the_selected_card(qtbot):
    """The bug this fix closes: cards is the default layout, and the tree
    carries no selection there at all — Ctrl+N must resolve from the card
    view's own selection instead."""
    win = MainWindow(layout="cards")
    qtbot.addWidget(win)
    groups = [*_groups(), RepoGroup("q", "/repo2", Path("/repo2/.gie/config.yaml"), [])]
    win.set_groups(groups, now=datetime.now().astimezone())
    card = win.card_view._cards["p-bar"]
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton)
    assert win.card_view.selected_name() == "p-bar"
    assert win._selected_prefix() == "p"


def test_selected_prefix_cards_mode_none_selected_two_groups(qtbot):
    win = MainWindow(layout="cards")
    qtbot.addWidget(win)
    groups = [*_groups(), RepoGroup("q", "/repo2", Path("/repo2/.gie/config.yaml"), [])]
    win.set_groups(groups, now=datetime.now().astimezone())
    assert win._selected_prefix() is None


def test_selected_prefix_cards_mode_none_selected_one_group_falls_back(qtbot):
    win = MainWindow(layout="cards")
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    assert win._selected_prefix() == "p"


def test_container_menu_offers_new(qtbot):
    """Read the menu off the window, never via `menuBar().actions()` ->
    `QAction.menu()`: that wrapper dies with the loop-local QAction (see
    `_build_refresh_menu`'s docstring)."""
    win = MainWindow()
    qtbot.addWidget(win)
    assert win.container_menu.title() == "&Container"
    assert win.new_container_action.text() == "&New…"
    assert win.new_pr_container_action.text() == "New from &PR…"


def test_container_menu_new_emits_the_selected_prefix(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0).child(0))

    with qtbot.waitSignal(win.newContainerRequested, timeout=1000) as blocker:
        win.new_container_action.trigger()

    assert blocker.args == ["p"]


def test_container_menu_new_from_pr_emits_the_selected_prefix(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0))

    with qtbot.waitSignal(win.newPrContainerRequested, timeout=1000) as blocker:
        win.new_pr_container_action.trigger()

    assert blocker.args == ["p"]


def test_container_menu_new_emits_empty_string_without_a_selection(qtbot):
    """The window reports what it knows; the controller owns the message."""
    win = MainWindow()
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

    win = MainWindow()
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

    assert seen == ["New container…", "New from PR…"]
    assert blocker.args == ["p"]


def test_group_row_context_menu_creates_pr_container(qtbot):
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtWidgets import QApplication

    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0))

    def choose_pr() -> None:
        popup = QApplication.activePopupWidget()
        if popup is not None:
            popup.actions()[1].trigger()
            popup.close()

    with qtbot.waitSignal(win.newPrContainerRequested, timeout=1000) as blocker:
        QTimer.singleShot(0, choose_pr)
        win._on_context_menu(QPoint(0, 0))

    assert blocker.args == ["p"]


def test_orphan_group_context_menu_does_not_offer_new_container(qtbot):
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtWidgets import QApplication

    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups([RepoGroup("gamma", None, None, [])], now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0))
    seen = []

    def inspect_popup():
        popup = QApplication.activePopupWidget()
        if popup is not None:
            seen.extend(action.text() for action in popup.actions())
            popup.close()

    QTimer.singleShot(0, inspect_popup)
    win._on_context_menu(QPoint(0, 0))
    assert seen == []


@pytest.mark.parametrize("layout", ["table", "cards"])
def test_filtered_all_repositories_show_visibility_guidance(qtbot, layout):
    win = MainWindow(layout=layout, hidden_repos=frozenset({"p"}))
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    assert win.empty_state_label.text() == (
        "No repositories are visible. Change visibility in View > Repositories."
    )
    assert win.stack.currentWidget() is win.empty_state_label


def test_no_gathered_repositories_shows_ordinary_empty_state(qtbot):
    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups([], now=datetime.now().astimezone())
    assert win.empty_state_label.text() == "No repositories found."
    assert win.stack.currentWidget() is win.empty_state_label


def test_empty_repo_header_shows_zero_container_count(qtbot):
    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups([RepoGroup("empty", "/empty", None, [])], now=datetime.now().astimezone())
    assert "0 containers" in win.tree.topLevelItem(0).text(0)


def test_config_menu_emits_the_selected_prefix(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(_groups(), now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0).child(0))
    received: list[tuple[str, bool]] = []
    win.configEditRequested.connect(lambda p, g: received.append((p, g)))

    win.edit_repo_config_action.trigger()
    win.edit_global_config_action.trigger()

    assert received == [("p", False), ("p", True)]


def test_config_menu_emits_empty_string_without_a_selection(qtbot):
    """The window reports what it knows; the controller owns the message."""
    win = MainWindow()
    qtbot.addWidget(win)
    received: list[tuple[str, bool]] = []
    win.configEditRequested.connect(lambda p, g: received.append((p, g)))

    win.edit_repo_config_action.trigger()
    win.edit_global_config_action.trigger()

    assert received == [("", False), ("", True)]


def test_repository_visibility_menu_filters_and_tracks_registered_prefixes(qtbot):
    win = MainWindow()
    qtbot.addWidget(win)
    empty = RepoGroup("empty", "/empty", None, [])
    win.set_groups([*_groups(), empty], now=datetime.now().astimezone())
    actions = {a.text(): a for a in win.repositories_menu.actions()}
    assert actions["Show empty repos"].isChecked()
    assert actions["empty"].isChecked()
    assert win.tree.topLevelItem(1).childCount() == 0

    with qtbot.waitSignal(win.repoVisibilityChanged, timeout=1000):
        actions["empty"].trigger()
    assert win.hidden_repos() == frozenset({"empty"})
    assert win.tree.topLevelItemCount() == 1

    win.set_groups(
        [*_groups(), empty, RepoGroup("later", "/later", None, [])], now=datetime.now().astimezone()
    )
    actions = {a.text(): a for a in win.repositories_menu.actions()}
    assert "later" in actions and actions["empty"].isChecked() is False
    win.set_groups(_groups(), now=datetime.now().astimezone())
    assert "empty" in {a.text() for a in win.repositories_menu.actions()}

    with qtbot.waitSignal(win.repoVisibilityChanged, timeout=1000):
        actions["Show empty repos"].trigger()
    assert not win.show_empty_repos()
    assert win.tree.topLevelItemCount() == 1


def test_synthetic_config_only_repo_remains_selectable_for_new(qtbot):
    win = MainWindow(layout="table")
    qtbot.addWidget(win)
    win.set_groups([RepoGroup("scratch", "/scratch", None, [])], now=datetime.now().astimezone())
    win.tree.setCurrentItem(win.tree.topLevelItem(0))
    assert win._selected_prefix() == "scratch"


def test_compact_table_headers_and_cells_explain_values(qtbot):
    from datetime import UTC, timedelta

    from jailbee.agent_status import AgentSummary

    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    groups = _groups()
    c = groups[0].containers[0]
    c.created_at = now - timedelta(hours=3)
    c.network = "loose"
    c.loose_until = now + timedelta(minutes=12)
    c.agent_status = (AgentSummary("claude", "waiting", now, "permission", 1),)
    win = MainWindow()
    qtbot.addWidget(win)
    win.set_groups(
        groups,
        now=now,
        columns=["state", "created", "network", "agent_compact", "target_diff", "local_diff"],
    )
    headers = win.tree.headerItem()
    group_item = win.tree.topLevelItem(0)
    # The rows are in the default sort order now, not the input order: find ours.
    row = next(
        group_item.child(i)
        for i in range(group_item.childCount())
        if group_item.child(i).data(0, int(Qt.ItemDataRole.UserRole)) == c.name
    )
    columns = {headers.text(i): i for i in range(win.tree.columnCount())}
    assert "Running" in row.toolTip(columns["ST"])
    assert c.created_at.isoformat() in row.toolTip(columns["AGE"])
    assert row.text(columns["LOOSE"]) == "● 12m"
    assert c.loose_until.isoformat() in row.toolTip(columns["LOOSE"])
    assert "strict" in headers.toolTip(columns["LOOSE"])
    assert "waiting" in headers.toolTip(columns["AI"])
    assert "claude" in row.toolTip(columns["AI"])
    assert "permission" in row.toolTip(columns["AI"])
    assert "target" in headers.toolTip(columns["DIFF"])
    assert "host" in headers.toolTip(columns["L DIFF"])


def _sortable_groups():
    from dataclasses import replace
    from datetime import UTC, timedelta

    t0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    new = ContainerInfo(
        name="p-new", state="Running", network="strict", ip=None, memory_limit=None, repo="p"
    )
    old = ContainerInfo(
        name="p-old", state="Stopped", network="strict", ip=None, memory_limit=None, repo="p"
    )
    return [
        RepoGroup(
            "p",
            "/repo",
            Path("/repo/.jailbee/config.yaml"),
            [replace(new, created_at=t0), replace(old, created_at=t0 - timedelta(days=1))],
        )
    ]


def _child_names(win):  # type: ignore[no-untyped-def]
    group = win.tree.invisibleRootItem().child(0)
    return [group.child(i).text(0) for i in range(group.childCount())]


def test_a_header_click_sorts_and_flips(qtbot):
    from jailbee.dashboard.sorting import SortSpec

    win = MainWindow(enabled_columns=("name", "state"))
    qtbot.addWidget(win)
    seen: list[int] = []
    win.sortChanged.connect(lambda: seen.append(1))
    win.set_groups(_sortable_groups(), now=datetime.now().astimezone())
    assert _child_names(win)[0].endswith("new")  # default order: newest first

    win.tree.header().sectionClicked.emit(1)  # ST
    assert win.sort_spec() == SortSpec("state", False)
    win.tree.header().sectionClicked.emit(1)
    assert win.sort_spec() == SortSpec("state", True)
    assert _child_names(win)[0].endswith("old")
    assert seen == [1, 1]
    assert win.tree.header().sortIndicatorSection() == 1


def test_a_stored_sort_sorts_the_first_render(qtbot):
    from jailbee.dashboard.sorting import SortSpec

    win = MainWindow(enabled_columns=("name", "state"), sort=SortSpec("state", True))
    qtbot.addWidget(win)
    win.set_groups(_sortable_groups(), now=datetime.now().astimezone())
    assert _child_names(win)[0].endswith("old")


def test_dragging_a_header_section_reorders_the_columns(qtbot):
    win = MainWindow(enabled_columns=("name", "state", "created"))
    qtbot.addWidget(win)
    seen: list[int] = []
    win.columnsChanged.connect(lambda: seen.append(1))
    win.set_groups(_sortable_groups(), now=datetime.now().astimezone())
    header = win.tree.header()

    header.moveSection(2, 1)  # drag "created" before "state"

    assert win.enabled_columns() == ("name", "created", "state")
    assert seen == [1]
    win.set_groups(_sortable_groups(), now=datetime.now().astimezone())
    # Rebuilt in logical order: no second, visual-only order left in the header.
    assert [header.logicalIndex(v) for v in range(header.count())] == list(range(header.count()))
    labels = [win.tree.headerItem().text(i) for i in range(win.tree.columnCount())]
    assert labels[:3] == ["NAME", "AGE", "ST"]


def test_name_section_cannot_move(qtbot):
    win = MainWindow(enabled_columns=("name", "state"))
    qtbot.addWidget(win)
    assert win.tree.header().sectionsMovable()
    assert not win.tree.header().isFirstSectionMovable()


def test_columns_menu_lists_the_stored_order_first(qtbot):
    win = MainWindow(enabled_columns=("name", "state", "created"))
    qtbot.addWidget(win)
    labels = [a.text().split(" ")[0] for a in win.columns_menu.actions()]
    assert labels[:3] == ["name", "state", "created"]
