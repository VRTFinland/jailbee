import logging
import threading

import pytest

pytest.importorskip("PySide6")

from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from jailbee.dashboard.model import RepoGroup
from jailbee.git_status import GitStatus
from jailbee.qtui import app as qapp
from jailbee.qtui.window import MainWindow
from jailbee.state_service import StateServiceUnavailable
from jailbee.state_service.protocol import Snapshot


def test_preflight_returns_none_when_no_configs(mocker):
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[])
    assert qapp.preflight(None) is None


def test_preflight_returns_paths_when_present(mocker):
    paths = [Path("/repo/.gie/config.yaml")]
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=paths)
    assert qapp.preflight(Path("/repo/.gie/config.yaml")) == paths


def test_run_returns_1_when_no_configs(mocker):
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[])
    rc = qapp.run(None)
    assert rc == 1


def test_the_launch_guard_message_is_the_tuis_own(mocker):
    """The TUI, the Qt window and `cli`'s pre-detach check all print the same
    sentence — one constant rather than three copies that drift apart."""
    from jailbee.dashboard import model as dmodel

    assert qapp.NOTHING_TO_SHOW is dmodel.NOTHING_TO_SHOW


def test_on_groups_updates_tree_and_status_bar(mocker):
    groups = [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [])]
    window = mocker.Mock()
    controller = qapp.AppController(window, mocker.Mock())

    controller.on_groups(groups)

    window.set_groups.assert_called_once()
    window.set_refresh_ok.assert_called_once()
    assert window.set_refresh_ok.call_args.kwargs["git_enabled"] is True


def test_on_failed_updates_status_bar_not_a_modal(mocker):
    """A service failure must not pop a QMessageBox — the client keeps
    reconnecting, so a modal per failure would spam the user."""
    window = mocker.Mock()
    controller = qapp.AppController(window, mocker.Mock())
    critical = mocker.patch("jailbee.qtui.app.QMessageBox.critical")

    controller.on_failed("boom")

    window.set_refresh_failed.assert_called_once_with("boom")
    critical.assert_not_called()


def _snapshot(groups=None, *, git_enabled=True):
    return Snapshot(
        1,
        datetime(2026, 10, 4, tzinfo=UTC),
        git_enabled,
        groups if groups is not None else [RepoGroup("p", "/repo", None, [])],
    )


def test_on_snapshot_shows_the_presented_groups(mocker):
    """The window shows this window's own view of the service's groups —
    `present` with its cwd pin — never the raw snapshot."""
    window = mocker.Mock()
    presented = [RepoGroup("q", "/q", None, [])]
    present = mocker.patch("jailbee.qtui.app.present", return_value=presented)
    controller = qapp.AppController(window, mocker.Mock(), cwd_root=Path("/repo"))
    snapshot = _snapshot()

    controller.on_snapshot(snapshot)

    present.assert_called_once_with(snapshot.groups, Path("/repo"))
    assert window.set_groups.call_args.args[0] is presented


def test_on_snapshot_marks_no_git_from_the_snapshot(mocker):
    window = mocker.Mock()
    controller = qapp.AppController(window, mocker.Mock())

    controller.on_snapshot(_snapshot(git_enabled=False))

    assert window.set_refresh_ok.call_args.kwargs["git_enabled"] is False


def test_minimising_tells_the_client_it_is_inactive(mocker):
    client = mocker.Mock()
    controller = qapp.AppController(mocker.Mock(), client)

    controller.on_window_active(False)

    client.set_active.assert_called_once_with(False)
    client.refresh.assert_not_called()


def test_restoring_tells_the_client_it_is_active_and_refreshes(mocker):
    client = mocker.Mock()
    controller = qapp.AppController(mocker.Mock(), client)
    controller.on_window_active(False)
    client.reset_mock()

    controller.on_window_active(True)

    client.set_active.assert_called_once_with(True)
    client.refresh.assert_called_once()


def test_a_repeated_active_state_is_not_resent(mocker):
    """Maximise and fullscreen also arrive as `activeChanged(True)`."""
    client = mocker.Mock()
    controller = qapp.AppController(mocker.Mock(), client)

    controller.on_window_active(True)

    client.set_active.assert_not_called()
    client.refresh.assert_not_called()


def test_refresh_now_asks_the_client_for_a_refresh(mocker):
    client = mocker.Mock()
    controller = qapp.AppController(mocker.Mock(), client)

    controller.on_refresh_requested()

    client.refresh.assert_called_once()


def test_controller_without_a_client_ignores_refresh_and_active(mocker):
    controller = qapp.AppController(mocker.Mock())
    controller.on_refresh_requested()
    controller.on_window_active(False)


def _mock_state_client(mocker, *, first=None, unavailable=None):
    """Patch `run()`'s `StateClient`; return the instance `run()` will get."""
    client = mocker.patch("jailbee.qtui.app.StateClient").return_value
    if unavailable is not None:
        client.wait_first_snapshot.side_effect = StateServiceUnavailable(unavailable)
    else:
        client.wait_first_snapshot.return_value = first if first is not None else _snapshot()
    return client


def _patch_run(mocker, *, first=None, unavailable=None):
    """Patch `run()`'s collaborators; return the `StateClient` mock instance."""
    mocker.patch("jailbee.qtui.app.QApplication")
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[Path("/x")])
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState

    mocker.patch("jailbee.qtui.app.seed_view_state", return_value=ViewState())
    mocker.patch("jailbee.db.gui_state.load_gui_state", return_value=GuiState())
    # persist_on_close() (in run()'s `finally`) also calls save_gui_state —
    # patch it too so it doesn't try to open a real session on the sentinel.
    mocker.patch("jailbee.db.gui_state.save_gui_state")
    return _mock_state_client(mocker, first=first, unavailable=unavailable)


def test_run_wires_bridge_and_window_signals_to_the_controller(mocker):
    """The bridge's signals and the window's go to controller slots; the
    client is driven only from those slots, never wired to a signal."""
    window = mocker.patch("jailbee.qtui.app.MainWindow").return_value
    bridge = mocker.patch("jailbee.qtui.app.StateBridge").return_value
    client = _patch_run(mocker)

    qapp.run(None)

    def targets(signal):
        return [c.args[0] for c in signal.connect.call_args_list]

    assert [t.__name__ for t in targets(bridge.snapshotReady)] == ["on_snapshot"]
    assert [t.__name__ for t in targets(bridge.failed)] == ["on_failed"]
    assert [t.__name__ for t in targets(window.activeChanged)] == ["on_window_active"]
    assert [t.__name__ for t in targets(window.refreshRequested)] == ["on_refresh_requested"]
    assert client.refresh not in targets(window.refreshRequested)
    for signal in (
        window.layoutChanged,
        window.cardStyleChanged,
        window.card_view.collapsedChanged,
        window.columnsChanged,
    ):
        assert len(targets(signal)) == 1
    # The client reports to the bridge, and the bridge reads the client.
    assert qapp.StateClient.call_args.kwargs["on_update"] == bridge.publish
    bridge.attach.assert_called_once_with(client)


def test_a_snapshot_from_the_reader_thread_is_handled_on_the_gui_thread(qtbot, mocker):
    """`StateClient` calls `publish` from its reader thread; the wiring must
    deliver `on_snapshot` on the GUI thread, where widgets may be touched.

    A real `StateBridge`, a real `AppController` and `app._wire` — the same
    helper `run()` uses — with the publish made from a real other thread.
    """
    app = QApplication.instance()
    window = MainWindow()
    qtbot.addWidget(window)
    snapshot = _snapshot()
    client = mocker.Mock()
    client.status.return_value = None
    client.latest.return_value = snapshot
    bridge = qapp.StateBridge()
    bridge.attach(client)
    controller = qapp.AppController(window, client)
    handled_on: list[object] = []
    mocker.patch.object(
        controller, "on_groups", side_effect=lambda _g: handled_on.append(QThread.currentThread())
    )
    qapp._wire(window, bridge, controller)

    publisher = threading.Thread(target=bridge.publish)
    publisher.start()
    publisher.join()
    qtbot.waitUntil(lambda: len(handled_on) == 1, timeout=3000)

    assert handled_on[0] is app.thread()


def test_controller_persists_on_layout_change(mocker):
    save = mocker.patch("jailbee.db.gui_state.save_gui_state")
    window = mocker.Mock()
    window.current_layout.return_value = "table"
    window.table_header_state.return_value = "Zm9v"
    window.current_card_style.return_value = "compact"
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)
    controller.on_layout_changed("table")
    save.assert_called_once()
    engine_arg, state_arg = save.call_args.args
    assert engine_arg is mocker.sentinel.engine
    assert state_arg.layout == "table"
    assert state_arg.table_header_state == "Zm9v"


def test_controller_persist_is_noop_without_engine(mocker):
    save = mocker.patch("jailbee.db.gui_state.save_gui_state")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_layout_changed("cards")
    controller.persist_on_close()
    save.assert_not_called()


def test_on_collapsed_changed_persists_view_state(mocker):
    """A fold change must write `view_prefs`, columns and folded set alike —
    not `gui_state`, which is `_persist`'s row."""
    save_view = mocker.patch("jailbee.db.view_prefs.save_view_state")
    save_gui = mocker.patch("jailbee.db.gui_state.save_gui_state")
    window = mocker.Mock()
    window.enabled_columns.return_value = ("name", "state")
    window.collapsed_repos.return_value = {"p", "q"}
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)

    controller.on_collapsed_changed()

    save_view.assert_called_once()
    engine_arg, frontend_arg, state_arg = save_view.call_args.args
    assert engine_arg is mocker.sentinel.engine
    assert frontend_arg == "qt"
    assert state_arg.columns == ("name", "state")
    assert state_arg.folded == frozenset({"p", "q"})
    # The two writers must never clobber each other's row.
    save_gui.assert_not_called()


def test_persist_view_state_carries_the_sort(mocker):
    from jailbee.dashboard.sorting import SortSpec

    save_view = mocker.patch("jailbee.db.view_prefs.save_view_state")
    window = mocker.Mock()
    window.enabled_columns.return_value = ("name",)
    window.collapsed_repos.return_value = set()
    window.sort_spec.return_value = SortSpec("cpu", True)
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)

    controller.on_collapsed_changed()  # an unrelated save
    state = save_view.call_args.args[2]
    assert (state.sort_field, state.sort_desc) == ("cpu", True)

    save_view.reset_mock()
    controller.on_sort_changed()
    state = save_view.call_args.args[2]
    assert (state.sort_field, state.sort_desc) == ("cpu", True)


def test_run_restores_the_sort(mocker):
    mocker.patch("jailbee.qtui.app.QApplication")
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[Path("/x")])
    mock_window_cls = mocker.patch("jailbee.qtui.app.MainWindow")
    _mock_state_client(mocker)
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    from jailbee.dashboard.sorting import SortSpec
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState

    mocker.patch(
        "jailbee.qtui.app.seed_view_state",
        return_value=ViewState(sort_field="cpu", sort_desc=True),
    )
    mocker.patch("jailbee.db.gui_state.load_gui_state", return_value=GuiState())
    mocker.patch("jailbee.db.gui_state.save_gui_state")

    qapp.run(None)

    _args, kwargs = mock_window_cls.call_args
    assert kwargs["sort"] == SortSpec("cpu", True)


def test_on_columns_changed_persists_view_state(mocker):
    """The Columns menu's toggle must also land in `view_prefs`, carrying
    whatever the window's own folded set currently is."""
    save_view = mocker.patch("jailbee.db.view_prefs.save_view_state")
    window = mocker.Mock()
    window.enabled_columns.return_value = ("name", "ip")
    window.collapsed_repos.return_value = set()
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)

    controller.on_columns_changed()

    save_view.assert_called_once()
    _engine_arg, frontend_arg, state_arg = save_view.call_args.args
    assert frontend_arg == "qt"
    assert state_arg.columns == ("name", "ip")
    assert state_arg.folded == frozenset()


def test_on_columns_changed_repaints_immediately(mocker):
    """A column toggle must reach the table right away, not on whatever the
    next snapshot happens to push — that one may be a whole refresh interval
    away, and the Columns menu would look completely inert for it.
    This fails if on_columns_changed goes back to only persisting."""
    mocker.patch("jailbee.db.view_prefs.save_view_state")
    groups = [RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [])]
    window = mocker.Mock()
    window.enabled_columns.return_value = ("name", "ip")
    window.collapsed_repos.return_value = set()
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)
    controller.on_groups(groups)  # populate self._latest, as a real refresh would
    window.set_groups.reset_mock()

    controller.on_columns_changed()

    window.set_groups.assert_called_once()
    assert window.set_groups.call_args.args[0] == groups


def test_on_columns_changed_does_not_repaint_before_any_refresh(mocker):
    """Before the first `on_groups`, `_latest` is empty — nothing to repaint,
    and `window.set_groups` must not be called with a bogus empty snapshot."""
    mocker.patch("jailbee.db.view_prefs.save_view_state")
    window = mocker.Mock()
    window.enabled_columns.return_value = ("name",)
    window.collapsed_repos.return_value = set()
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)

    controller.on_columns_changed()

    window.set_groups.assert_not_called()


def test_persist_view_state_is_noop_without_engine(mocker):
    save_view = mocker.patch("jailbee.db.view_prefs.save_view_state")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())

    controller.on_collapsed_changed()
    controller.on_columns_changed()

    save_view.assert_not_called()


def test_on_layout_changed_does_not_touch_view_prefs(mocker):
    """The mirror of `test_on_collapsed_changed_persists_view_state`: window
    layout persistence must never write the `view_prefs` row."""
    mocker.patch("jailbee.db.gui_state.save_gui_state")
    save_view = mocker.patch("jailbee.db.view_prefs.save_view_state")
    window = mocker.Mock()
    window.current_layout.return_value = "table"
    window.table_header_state.return_value = None
    window.current_card_style.return_value = "compact"
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)

    controller.on_layout_changed("table")

    save_view.assert_not_called()


def test_persist_on_close_writes_snapshot(mocker):
    save = mocker.patch("jailbee.db.gui_state.save_gui_state")
    window = mocker.Mock()
    window.current_layout.return_value = "cards"
    window.table_header_state.return_value = "AAAA"
    window.current_card_style.return_value = "grid"
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)
    controller.persist_on_close()
    _engine, state = save.call_args.args
    assert state.layout == "cards"
    assert state.table_header_state == "AAAA"
    assert state.card_style == "grid"


def test_persist_writes_card_style(qtbot, mocker):
    """Round-trips through a real in-memory engine (not a mocked
    save_gui_state) to exercise the actual GuiState(...) construction in
    _persist, mirroring test_db_gui_state.py's _engine() helper."""
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.gui_state import load_gui_state

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    window = MainWindow()
    qtbot.addWidget(window)
    controller = qapp.AppController(window, mocker.Mock(), engine=engine)

    window._switch_card_style("grid")
    controller.on_card_style_changed("grid")

    saved = load_gui_state(engine)
    assert saved.card_style == "grid"


def test_on_card_style_changed_persists(mocker):
    save = mocker.patch("jailbee.db.gui_state.save_gui_state")
    window = mocker.Mock()
    window.current_card_style.return_value = "grid"
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)

    controller.on_card_style_changed("grid")

    save.assert_called_once()
    _engine_arg, state_arg = save.call_args.args
    assert state_arg.card_style == "grid"


def test_run_restores_card_style(mocker):
    mocker.patch("jailbee.qtui.app.QApplication")
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[Path("/x")])
    mock_window_cls = mocker.patch("jailbee.qtui.app.MainWindow")
    _mock_state_client(mocker)
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState

    mocker.patch("jailbee.qtui.app.seed_view_state", return_value=ViewState())
    mocker.patch(
        "jailbee.db.gui_state.load_gui_state",
        return_value=GuiState(
            layout="cards",
            card_style="grid",
        ),
    )
    mocker.patch("jailbee.db.gui_state.save_gui_state")

    qapp.run(None)

    _args, kwargs = mock_window_cls.call_args
    assert kwargs["card_style"] == "grid"


def test_run_restores_enabled_columns_and_folded_repos(mocker):
    """`run()` must seed the window's Columns menu and the card view's fold
    state from the Qt front-end's own `view_prefs` row — not from the TUI's."""
    mocker.patch("jailbee.qtui.app.QApplication")
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[Path("/x")])
    mock_window_cls = mocker.patch("jailbee.qtui.app.MainWindow")
    window = mock_window_cls.return_value
    _mock_state_client(mocker)
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState

    mocker.patch(
        "jailbee.qtui.app.seed_view_state",
        return_value=ViewState(columns=("name", "ip"), folded=frozenset({"repo-a"})),
    )
    mocker.patch("jailbee.db.gui_state.load_gui_state", return_value=GuiState())
    mocker.patch("jailbee.db.gui_state.save_gui_state")

    qapp.run(None)

    _args, kwargs = mock_window_cls.call_args
    assert kwargs["enabled_columns"] == ("name", "ip")
    window.card_view.set_collapsed.assert_called_once_with({"repo-a"})


def test_run_restores_qt_visibility_without_changing_tui_state(mocker):
    mocker.patch("jailbee.qtui.app.QApplication")
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[Path("/x")])
    mock_window_cls = mocker.patch("jailbee.qtui.app.MainWindow")
    _mock_state_client(mocker)
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState

    mocker.patch(
        "jailbee.qtui.app.seed_view_state",
        return_value=ViewState(show_empty_repos=False, hidden_repos=frozenset({"alpha"})),
    )
    mocker.patch("jailbee.db.gui_state.load_gui_state", return_value=GuiState())
    mocker.patch("jailbee.db.gui_state.save_gui_state")

    qapp.run(None)

    _args, kwargs = mock_window_cls.call_args
    assert kwargs["show_empty_repos"] is False
    assert kwargs["hidden_repos"] == frozenset({"alpha"})


def test_visibility_persistence_saves_complete_qt_view_and_survives_write_failure(mocker, caplog):
    import logging

    save = mocker.patch("jailbee.db.view_prefs.save_view_state", side_effect=OSError("disk full"))
    window = mocker.Mock()
    window.enabled_columns.return_value = ("name", "state")
    window.collapsed_repos.return_value = {"beta"}
    window.show_empty_repos.return_value = False
    window.hidden_repos.return_value = {"alpha"}
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)

    with caplog.at_level(logging.WARNING):
        controller.on_repo_visibility_changed()

    state = save.call_args.args[2]
    assert state.columns == ("name", "state")
    assert state.folded == frozenset({"beta"})
    assert state.show_empty_repos is False
    assert state.hidden_repos == frozenset({"alpha"})
    window.set_status.assert_called_once()
    assert "disk full" in window.set_status.call_args.args[0]


def test_on_groups_keeps_new_and_hidden_repositories_in_menu_snapshot(mocker):
    groups = [
        RepoGroup("alpha", "/alpha", None, []),
        RepoGroup("beta", "/beta", None, []),
    ]
    window = mocker.Mock()
    controller = qapp.AppController(window, mocker.Mock())

    controller.on_groups(groups)

    assert controller._latest == groups
    window.set_groups.assert_called_once_with(groups, now=mocker.ANY)


def test_on_groups_refresh_keeps_hidden_prefix_available_in_repository_menu(qtbot, mocker):

    window = MainWindow(
        show_empty_repos=False,
        hidden_repos=frozenset({"alpha"}),
    )
    qtbot.addWidget(window)
    controller = qapp.AppController(window, mocker.Mock())

    controller.on_groups(
        [RepoGroup("alpha", "/alpha", None, []), RepoGroup("beta", "/beta", None, [])]
    )

    actions = {action.text(): action for action in window.repositories_menu.actions()}
    assert "alpha" in actions
    assert not actions["alpha"].isChecked()
    assert "beta" in actions
    assert controller._latest[0].prefix == "alpha"


def test_on_action_does_not_dispatch_for_a_hidden_container(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path)
    controller._window.hidden_repos.return_value = frozenset({"p"})
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("start", "p-foo")

    popen.assert_not_called()


def _controller_with_group(
    mocker,
    tmp_path,
    *,
    loose_ttl_default="5m",
    push_action_default="ask",
    push_source_default="base",
    base_branch=None,
    pr_number=None,
    with_config=True,
):
    """An AppController holding one snapshot row, so on_action can resolve a config.

    ``with_config=False`` builds the scratch case: a real repo root with no
    config file, whose ``RepoGroup.config_path`` is None.
    """
    from jailbee.dashboard.model import RepoGroup
    from jailbee.lifecycle import ContainerInfo

    window = mocker.Mock()
    client = mocker.Mock()
    controller = qapp.AppController(window, client)
    ci = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        base_branch=base_branch,
        pr_number=pr_number,
    )
    config_path = tmp_path / ".jailbee" / "config.yaml"
    if with_config:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        # container_prefix must be set explicitly: tmp_path's own basename (a
        # pytest-generated test name) contains underscores and would otherwise
        # fail load_config's prefix validation — the destroy guard loads this
        # file for real (destroy_guard.assess needs a Config), so it must parse.
        config_path.write_text("container_prefix: p\n")
    # load_config() also shells out to git — `git remote` to resolve the
    # upstream remote, then `git symbolic-ref` for the default branch. tmp_path
    # isn't a git repo, so those calls would fail harmlessly on their own — but
    # destroy tests mock `subprocess.Popen` (to assert the destroy launch didn't
    # happen), which intercepts these unrelated internal calls too. Stub both so
    # load_config stays a pure in-memory parse in tests.
    mocker.patch("jailbee.config.loader.detect_upstream_remote", return_value="origin")
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    # RepoGroup.repo_root is `str | None`, not a Path.
    controller._latest = [
        RepoGroup(
            prefix="p",
            repo_root=str(tmp_path),
            config_path=config_path if with_config else None,
            containers=[ci],
            loose_ttl_default=loose_ttl_default,
            push_action_default=push_action_default,
            push_source_default=push_source_default,
        )
    ]
    return controller


def test_outbox_action_opens_native_not_terminal(mocker, tmp_path):
    from jailbee.dashboard.model import RepoTarget

    controller = _controller_with_group(mocker, tmp_path)
    native = mocker.patch.object(controller, "_open_outbox", create=True)
    terminal = mocker.patch("jailbee.qtui.app.detect_terminal")
    controller.on_action("outbox browse", "p-foo")
    native.assert_called_once_with(
        RepoTarget(tmp_path, tmp_path / ".jailbee" / "config.yaml"), "p-foo"
    )
    terminal.assert_not_called()


@pytest.mark.parametrize("marker", ["JAILBEE_SSH_SESSION", "JAILBEE_REMOTE_SSH"])
def test_outbox_over_any_ssh_keeps_terminal_browser(mocker, tmp_path, monkeypatch, marker):
    from jailbee.qtui.terminal import TerminalSpec

    monkeypatch.setenv(marker, "1")
    controller = _controller_with_group(mocker, tmp_path)
    native = mocker.patch.object(controller, "_open_outbox", create=True)
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=TerminalSpec("xterm", ["-e"]))
    spawn = mocker.Mock()
    mocker.patch.object(qapp, "subprocess", spawn)
    controller.on_action("outbox browse", "p-foo")
    native.assert_not_called()
    assert spawn.Popen.call_args.args[0][:6] == [
        "xterm",
        "-e",
        "jailbee",
        "outbox",
        "browse",
        "p-foo",
    ]


def test_outbox_publish_command_and_dialog_lifetime(qtbot, mocker, tmp_path):
    from PySide6.QtCore import Signal

    from jailbee.dashboard.model import RepoTarget
    from jailbee.qtui.terminal import TerminalSpec

    class Dialog(QDialog):
        changed = Signal()
        publishRequested = Signal(str, str)  # noqa: N815 - mirrors the Qt signal contract
        retired = Signal()
        closing = False

        def __init__(self, *args, **kwargs):
            super().__init__()

        def publication_started(self):
            self.started = True

    controller = _controller_with_group(mocker, tmp_path)
    factory = mocker.patch.object(qapp, "OutboxDialog", side_effect=Dialog, create=True)
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=TerminalSpec("xterm", ["-e"]))
    spawn = mocker.Mock()
    mocker.patch.object(qapp, "subprocess", spawn)
    target = RepoTarget(tmp_path, None)
    controller._open_outbox(target, "p-foo")
    dialog = next(iter(controller._outboxes.values()))
    qtbot.addWidget(dialog)
    controller._open_outbox(target, "p-foo")
    assert factory.call_count == 1
    dialog.publishRequested.emit("issue/001.json", "a" * 64)
    assert spawn.Popen.call_args.args[0] == [
        "xterm",
        "-e",
        "jailbee",
        "outbox",
        "apply",
        "p-foo",
        "issue/001.json",
        "--revision",
        "a" * 64,
    ]
    assert spawn.Popen.call_args.kwargs == {"start_new_session": True, "cwd": tmp_path}
    assert dialog.started
    controller._client.refresh.assert_not_called()  # Launch is not a receipt.
    dialog.changed.emit()
    controller._client.refresh.assert_called_once()
    dialog.retired.emit()
    assert not controller._outboxes


@pytest.mark.parametrize("with_config", [True, False])
def test_outbox_publish_builder_validates_tokens_and_preserves_target(tmp_path, with_config):
    from jailbee.dashboard.model import RepoTarget
    from jailbee.qtui.actions import build_outbox_publish

    target = RepoTarget(tmp_path, tmp_path / "config.yaml" if with_config else None)
    action = build_outbox_publish("p-foo", "issue/001.json", "a" * 64, target)
    assert action.argv == [
        "jailbee",
        "outbox",
        "apply",
        "p-foo",
        "issue/001.json",
        "--revision",
        "a" * 64,
        *(["--config", str(tmp_path / "config.yaml")] if with_config else []),
    ]
    assert action.launch == "terminal" and not action.confirm
    assert action.cwd == tmp_path
    for proposal, revision in [("../bad", "a" * 64), ("issue/001.json", "--yes")]:
        with pytest.raises(ValueError):
            build_outbox_publish("p-foo", proposal, revision, target)


def test_outbox_publish_missing_terminal_warns_without_launch(mocker, tmp_path):
    from jailbee.dashboard.model import RepoTarget

    controller = _controller_with_group(mocker, tmp_path)
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    warning = mocker.patch.object(QMessageBox, "warning")
    spawn = mocker.Mock()
    mocker.patch.object(qapp, "subprocess", spawn)
    controller._publish_outbox(RepoTarget(tmp_path, None), "p-foo", "issue/001.json", "a" * 64)
    warning.assert_called_once()
    spawn.Popen.assert_not_called()


def test_controller_retains_closing_dialog_until_blocked_delete_completes(
    qtbot, mocker, make_cfg, tmp_path
):
    from threading import Event

    from jailbee.dashboard.model import RepoTarget
    from jailbee.outbox_io import JournalStore
    from jailbee.qtui import outbox
    from tests.outbox_support import IDENTITY
    from tests.test_qtui_outbox import select, views

    cfg = make_cfg(tmp_path)
    mocker.patch.object(outbox.config_api, "load_repo_config", return_value=cfg)
    mocker.patch.object(outbox.commands, "resolve_target", return_value=(cfg, IDENTITY.full_name))
    mocker.patch.object(outbox, "Incus", return_value=mocker.Mock())
    mocker.patch.object(outbox, "JournalStore", return_value=JournalStore(tmp_path / "journals"))
    mocker.patch.object(outbox.service, "load_container", return_value=views(tmp_path))
    entered, release = Event(), Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return ()

    mocker.patch.object(outbox.service, "execute_delete", side_effect=blocked)
    mocker.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes)
    window = MainWindow()
    qtbot.addWidget(window)
    client = mocker.Mock()
    controller = qapp.AppController(window, client)
    target = RepoTarget(cfg.repo_root, None)
    controller._open_outbox(target, IDENTITY.full_name)
    dialog = next(iter(controller._outboxes.values()))
    qtbot.waitUntil(lambda: not dialog.busy)
    select(dialog, action=0, comment=0)
    try:
        dialog.delete_selected()
        qtbot.waitUntil(entered.is_set)
        dialog.close()
        controller._open_outbox(target, IDENTITY.full_name)
        assert list(controller._outboxes.values()) == [dialog]
        release.set()
        qtbot.waitUntil(lambda: not controller._outboxes)
        client.refresh.assert_called_once()
        controller._open_outbox(target, IDENTITY.full_name)
        new = next(iter(controller._outboxes.values()))
        assert new is not dialog
        qtbot.waitUntil(lambda: not new.busy)
        new.close()
    finally:
        release.set()
        qtbot.waitUntil(lambda: not dialog.busy)


def test_on_action_retarget_passes_the_dialog_answer_after_a_separator(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path, base_branch="main")
    hb = mocker.patch("jailbee.qtui.app.host_branches", return_value=("develop",))
    dialog = mocker.Mock()
    dialog.exec.return_value = QDialog.DialogCode.Accepted
    dialog.answer.return_value = "develop"
    mocker.patch("jailbee.qtui.app.RetargetDialog", return_value=dialog)
    output = mocker.patch.object(controller, "_open_output")
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("git retarget", "p-foo")

    assert hb.call_args.kwargs["exclude"] == "main"
    output.assert_called_once()
    assert output.call_args.args[0][-2:] == ["--", "develop"]
    popen.assert_not_called()


def test_on_action_retarget_cancelled_dialog_launches_nothing(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path, base_branch="main")
    mocker.patch("jailbee.qtui.app.host_branches", return_value=("develop",))
    dialog = mocker.Mock()
    dialog.exec.return_value = QDialog.DialogCode.Rejected
    mocker.patch("jailbee.qtui.app.RetargetDialog", return_value=dialog)
    output = mocker.patch.object(controller, "_open_output")
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("git retarget", "p-foo")

    output.assert_not_called()
    popen.assert_not_called()


def test_on_action_net_loose_asks_for_a_duration_and_passes_it(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path)
    mocker.patch(
        "jailbee.qtui.app.QInputDialog.getItem",
        return_value=("2h", True),
    )
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net loose", "p-foo")

    argv = popen.call_args.args[0]
    assert argv[-2:] == ["--for", "2h"]


def test_on_action_net_loose_cancelled_dialog_launches_nothing(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path)
    mocker.patch(
        "jailbee.qtui.app.QInputDialog.getItem",
        return_value=("", False),
    )
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net loose", "p-foo")

    popen.assert_not_called()


def test_on_action_net_loose_dialog_preselects_the_repo_default(mocker, tmp_path):
    """A repo configured with `after: 45m` must get 45m offered *and*
    pre-selected — not the hard-coded first preset (5m)."""
    from jailbee.config import LOOSE_TTL_PRESETS

    controller = _controller_with_group(mocker, tmp_path, loose_ttl_default="45m")
    get_item = mocker.patch(
        "jailbee.qtui.app.QInputDialog.getItem",
        return_value=("45m", True),
    )
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net loose", "p-foo")

    items = get_item.call_args.args[3]
    current = get_item.call_args.args[4]
    assert "45m" in items
    assert items[current] == "45m"
    assert "never" in items
    assert set(LOOSE_TTL_PRESETS) <= set(items)


def test_on_action_net_loose_dialog_preselects_a_preset_default(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path, loose_ttl_default="2h")
    get_item = mocker.patch(
        "jailbee.qtui.app.QInputDialog.getItem",
        return_value=("2h", True),
    )
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net loose", "p-foo")

    items = get_item.call_args.args[3]
    current = get_item.call_args.args[4]
    assert items[current] == "2h"
    assert items.count("2h") == 1  # not inserted twice


def test_on_action_net_loose_skips_the_dialog_when_policy_disabled(mocker, tmp_path):
    """`loose_ttl_default is None` means auto-revert is off: there is no TTL to
    schedule, so asking would be misleading — dispatch without `--for`, which
    is exactly what the CLI does in the same situation."""
    controller = _controller_with_group(mocker, tmp_path, loose_ttl_default=None)
    get_item = mocker.patch("jailbee.qtui.app.QInputDialog.getItem")
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net loose", "p-foo")

    get_item.assert_not_called()
    argv = popen.call_args.args[0]
    assert "--for" not in argv


def test_on_action_net_loose_rejects_an_unparseable_typed_duration(mocker, tmp_path):
    """The combo box is editable, so a typo like `2 hours` is possible. The
    action runs as a detached Popen with no terminal, so an unvalidated value
    would exit 2 out of sight — warn and re-ask instead."""
    controller = _controller_with_group(mocker, tmp_path)
    mocker.patch(
        "jailbee.qtui.app.QInputDialog.getItem",
        side_effect=[("2 hours", True), ("2h", True)],
    )
    warning = mocker.patch("jailbee.qtui.app.QMessageBox.warning")
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net loose", "p-foo")

    warning.assert_called_once()
    argv = popen.call_args.args[0]
    assert argv[-2:] == ["--for", "2h"]


def test_on_action_net_loose_rejects_an_over_cap_typed_duration(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path)
    mocker.patch(
        "jailbee.qtui.app.QInputDialog.getItem",
        side_effect=[("25h", True), ("", False)],
    )
    warning = mocker.patch("jailbee.qtui.app.QMessageBox.warning")
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net loose", "p-foo")

    warning.assert_called_once()
    assert "24h" in str(warning.call_args.args[2])
    popen.assert_not_called()


def test_on_action_net_loose_accepts_never(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path)
    mocker.patch(
        "jailbee.qtui.app.QInputDialog.getItem",
        return_value=("never", True),
    )
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net loose", "p-foo")

    argv = popen.call_args.args[0]
    assert argv[-2:] == ["--for", "never"]


def test_on_action_net_strict_does_not_open_a_duration_dialog(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path)
    get_item = mocker.patch("jailbee.qtui.app.QInputDialog.getItem")
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net strict", "p-foo")

    get_item.assert_not_called()


def test_on_action_launches_a_configured_repo_in_its_repo_root(mocker, tmp_path):
    """The cwd is set for both kinds of repo — one code path — while
    `--config` still addresses the configured one exactly as before."""
    controller = _controller_with_group(mocker, tmp_path)
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net strict", "p-foo")

    assert popen.call_args.args[0] == [
        "jailbee",
        "net",
        "strict",
        "p-foo",
        "--config",
        str(tmp_path / ".jailbee" / "config.yaml"),
    ]
    assert popen.call_args.kwargs["cwd"] == tmp_path


def test_on_action_launches_a_scratch_repo_in_its_repo_root(mocker, tmp_path):
    """No `--config` to pass, so the child's cwd is the only thing that says
    which repo the action belongs to."""
    controller = _controller_with_group(mocker, tmp_path, with_config=False)
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("net strict", "p-foo")

    assert popen.call_args.args[0] == ["jailbee", "net", "strict", "p-foo"]
    assert popen.call_args.kwargs["cwd"] == tmp_path


def test_on_action_gives_the_output_window_the_repo_root(mocker, tmp_path):
    """The printing verbs run under a QProcess rather than a Popen, so the cwd
    has to reach that path too — otherwise `git diff` in a scratch repo
    resolves whichever directory the GUI itself was started in."""
    controller = _controller_with_group(mocker, tmp_path, with_config=False)
    open_output = mocker.patch.object(qapp.AppController, "_open_output")

    controller.on_action("git diff", "p-foo")

    argv = open_output.call_args.args[0]
    assert argv == ["jailbee", "git", "diff", "p-foo"]
    assert open_output.call_args.args[2] == tmp_path


def test_destroy_guard_reads_a_scratch_repos_config_from_its_root(mocker, tmp_path):
    """The guard used to load `group.config_path`, which a scratch repo has
    none of. Loading from the repo root synthesizes the same config the
    dashboard is already displaying, so the risk line survives."""
    controller = _controller_with_group(mocker, tmp_path, with_config=False)
    controller._latest[0].containers[0].git_status = GitStatus(
        wt="+12 -3", ahead_diff="clean", ahead_count="0", conflict="ok"
    )
    load = mocker.patch("jailbee.config.load_repo_config")
    mocker.patch("jailbee.destroy_guard.assess", return_value=None)
    mocker.patch(
        "jailbee.qtui.app.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    )

    controller.on_action("destroy", "p-foo")

    load.assert_called_once_with(tmp_path)


def test_net_egress_ls_opens_the_qt_output_window(mocker, tmp_path):
    """The shared Egress action prints its table, which must stay visible in Qt."""
    controller = _controller_with_group(mocker, tmp_path)
    open_output = mocker.patch.object(qapp.AppController, "_open_output")
    popen = mocker.patch.object(qapp.subprocess, "Popen")

    controller.on_action("net egress ls", "p-foo")

    open_output.assert_called_once()
    assert open_output.call_args.args[0][:4] == ["jailbee", "net", "egress", "ls"]
    assert open_output.call_args.args[1] == "jailbee net egress ls p-foo"
    assert open_output.call_args.args[2] == tmp_path
    popen.assert_not_called()


def test_on_action_git_diff_opens_an_output_window_instead_of_spawning(mocker, tmp_path):
    """`git diff` exists for the text it prints: a detached Popen would throw
    that away, so the verb must go to the output window instead."""
    controller = _controller_with_group(mocker, tmp_path)
    open_output = mocker.patch.object(qapp.AppController, "_open_output")
    popen = mocker.patch.object(qapp.subprocess, "Popen")

    controller.on_action("git diff", "p-foo")

    popen.assert_not_called()
    argv = open_output.call_args.args[0]
    assert argv[:4] == ["jailbee", "git", "diff", "p-foo"]
    assert open_output.call_args.args[1] == "jailbee git diff p-foo"


def _stub_dialog(mocker, attr, answers, *, accepted=True):
    """Patch a prompt dialog class in app's namespace; return the class mock."""
    cls = mocker.patch(f"jailbee.qtui.app.{attr}")
    cls.return_value.exec.return_value = (
        QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected
    )
    cls.return_value.answers.return_value = answers
    return cls


def test_on_action_git_push_with_pinned_config_asks_nothing(mocker, tmp_path):
    """A repo that pinned both `push:` defaults has already answered. Asking
    anyway — and passing the answer as a flag — would override its policy."""
    controller = _controller_with_group(
        mocker, tmp_path, push_action_default="merge", push_source_default="base"
    )
    dialog = mocker.patch("jailbee.qtui.app.PushOptionsDialog")
    open_output = mocker.patch.object(qapp.AppController, "_open_output")

    controller.on_action("git push", "p-foo")

    dialog.assert_not_called()
    argv = open_output.call_args.args[0]
    assert not {"--merge", "--rebase", "--plain", "--from", "--current"} & set(argv)


def test_on_action_git_push_asks_when_the_config_says_ask(mocker, tmp_path):
    """`push.default_action` defaults to 'ask', and the detached child has no
    stdin to answer with — so the GUI asks and passes the answer as a flag."""
    from jailbee.qtui.prompts import PushAnswers

    controller = _controller_with_group(
        mocker, tmp_path, push_action_default="ask", base_branch="main"
    )
    dialog = _stub_dialog(mocker, "PushOptionsDialog", PushAnswers(action="rebase", source=None))
    open_output = mocker.patch.object(qapp.AppController, "_open_output")

    controller.on_action("git push", "p-foo")

    assert dialog.call_args.kwargs["ask_action"] is True
    assert dialog.call_args.kwargs["ask_source"] is False
    assert dialog.call_args.kwargs["base_branch"] == "main"
    assert open_output.call_args.args[0][-1] == "--rebase"


def test_on_action_git_push_cancelled_dispatches_nothing(mocker, tmp_path):
    from jailbee.qtui.prompts import PushAnswers

    controller = _controller_with_group(mocker, tmp_path, push_action_default="ask")
    _stub_dialog(
        mocker, "PushOptionsDialog", PushAnswers(action="merge", source=None), accepted=False
    )
    open_output = mocker.patch.object(qapp.AppController, "_open_output")
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("git push", "p-foo")

    open_output.assert_not_called()
    popen.assert_not_called()


def test_on_action_pr_refresh_never_asks_for_a_source(mocker, tmp_path):
    """`--pr` fixes the source to the PR head and the CLI rejects `--from` /
    `--current` alongside it, so answering that question would be a usage
    error — even in a repo whose `push.default_source` is 'ask'."""
    from jailbee.qtui.prompts import PushAnswers

    controller = _controller_with_group(
        mocker,
        tmp_path,
        push_action_default="ask",
        push_source_default="ask",
        base_branch="main",
        pr_number=42,
    )
    dialog = _stub_dialog(mocker, "PushOptionsDialog", PushAnswers(action="rebase", source=None))
    open_output = mocker.patch.object(qapp.AppController, "_open_output")

    controller.on_action("git push --pr", "p-foo")

    assert dialog.call_args.kwargs["ask_action"] is True
    assert dialog.call_args.kwargs["ask_source"] is False
    assert dialog.call_args.kwargs["title"] == "Refresh 'p-foo' from PR #42"
    argv = open_output.call_args.args[0]
    assert argv[:5] == ["jailbee", "git", "push", "--pr", "p-foo"]
    assert not {"--from", "--current"} & set(argv)
    assert argv[-1] == "--rebase"


def test_on_action_pr_refresh_with_a_pinned_action_asks_nothing(mocker, tmp_path):
    """The source question is the only one 'ask' would still have left open,
    and `--pr` has already answered it — so no dialog is worth showing."""
    controller = _controller_with_group(
        mocker,
        tmp_path,
        push_action_default="merge",
        push_source_default="ask",
        pr_number=42,
    )
    dialog = mocker.patch("jailbee.qtui.app.PushOptionsDialog")
    open_output = mocker.patch.object(qapp.AppController, "_open_output")

    controller.on_action("git push --pr", "p-foo")

    dialog.assert_not_called()
    argv = open_output.call_args.args[0]
    assert not {"--merge", "--rebase", "--plain", "--from", "--current"} & set(argv)


def test_on_action_pr_refresh_without_a_known_number_stays_honest(mocker, tmp_path):
    """The menu only offers this on a container with a PR, but the dispatch is
    by verb string — an unknown number must not become a fabricated one."""
    from jailbee.qtui.prompts import PushAnswers

    controller = _controller_with_group(mocker, tmp_path, push_action_default="ask")
    dialog = _stub_dialog(mocker, "PushOptionsDialog", PushAnswers(action="merge", source=None))
    mocker.patch.object(qapp.AppController, "_open_output")

    controller.on_action("git push --pr", "p-foo")

    assert dialog.call_args.kwargs["title"] == "Refresh 'p-foo' from its PR head"


def test_on_action_pr_asks_and_passes_the_flags(mocker, tmp_path):
    from jailbee.qtui.prompts import PrAnswers

    controller = _controller_with_group(mocker, tmp_path)
    dialog = _stub_dialog(
        mocker,
        "PrOptionsDialog",
        PrAnswers(ready=True, regenerate=False, confirm_foreign=True),
    )
    open_output = mocker.patch.object(qapp.AppController, "_open_output")

    controller.on_action("pr", "p-foo")

    dialog.assert_called_once()
    assert open_output.call_args.args[0][-2:] == ["--ready", "--yes"]


def test_on_action_pr_cancelled_dispatches_nothing(mocker, tmp_path):
    from jailbee.qtui.prompts import PrAnswers

    controller = _controller_with_group(mocker, tmp_path)
    _stub_dialog(
        mocker,
        "PrOptionsDialog",
        PrAnswers(ready=None, regenerate=False, confirm_foreign=False),
        accepted=False,
    )
    open_output = mocker.patch.object(qapp.AppController, "_open_output")
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("pr", "p-foo")

    open_output.assert_not_called()
    popen.assert_not_called()


def test_git_pull_confirmation_names_the_host_branch_and_not_destruction(mocker, tmp_path):
    """`git pull` writes to the *host* repo, which a menu entry does not convey
    — but it destroys nothing, so it must not inherit destroy's wording."""
    controller = _controller_with_group(mocker, tmp_path, base_branch="main")
    question = mocker.patch(
        "jailbee.qtui.app.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    )
    open_output = mocker.patch.object(qapp.AppController, "_open_output")

    controller.on_action("git pull", "p-foo")

    open_output.assert_not_called()  # declined
    text = question.call_args.args[2]
    assert "p-foo" in text
    assert "main" in text
    assert "destroy" not in text.lower()
    assert question.call_args.args[4] == QMessageBox.StandardButton.No  # default button


def test_git_pull_confirmation_accepted_opens_the_output_window(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path, base_branch="main")
    mocker.patch(
        "jailbee.qtui.app.QMessageBox.question",
        return_value=QMessageBox.StandardButton.Yes,
    )
    open_output = mocker.patch.object(qapp.AppController, "_open_output")

    controller.on_action("git pull", "p-foo")

    argv = open_output.call_args.args[0]
    assert argv[:4] == ["jailbee", "git", "pull", "p-foo"]
    assert "--force" not in argv


def test_destroy_at_risk_shows_the_summary_with_cancel_defaulted(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path)
    controller._latest[0].containers[0].git_status = GitStatus(
        wt="+12 -3", ahead_diff="clean", ahead_count="0", conflict="ok"
    )
    question = mocker.patch(
        "jailbee.qtui.app.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    )
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("destroy", "p-foo")

    popen.assert_not_called()
    text = question.call_args.args[2]
    assert "working tree +12 -3" in text
    assert question.call_args.args[4] == QMessageBox.StandardButton.No  # default button


def test_destroy_at_risk_accepted_launches(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path)
    controller._latest[0].containers[0].git_status = GitStatus(
        wt="+12 -3", ahead_diff="clean", ahead_count="0", conflict="ok"
    )
    mocker.patch(
        "jailbee.qtui.app.QMessageBox.question",
        return_value=QMessageBox.StandardButton.Yes,
    )
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("destroy", "p-foo")

    popen.assert_called_once()


def test_destroy_clean_container_gets_the_plain_dialog(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path)
    controller._latest[0].containers[0].git_status = GitStatus(
        wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok"
    )
    question = mocker.patch(
        "jailbee.qtui.app.QMessageBox.question",
        return_value=QMessageBox.StandardButton.Yes,
    )
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("destroy", "p-foo")

    assert "Destroying loses this" not in question.call_args.args[2]


def test_destroy_guard_assess_failure_is_logged_at_debug(mocker, tmp_path, caplog):
    """A malformed `.jailbee/config.yaml` must not block the destroy guard —
    the outcome stays "no risk shown", same as before — but the failure must be
    discoverable rather than vanishing into a bare `except Exception: pass`."""
    controller = _controller_with_group(mocker, tmp_path)
    controller._latest[0].containers[0].git_status = GitStatus(
        wt="+12 -3", ahead_diff="clean", ahead_count="0", conflict="ok"
    )
    mocker.patch("jailbee.config.load_repo_config", side_effect=ValueError("boom"))
    question = mocker.patch(
        "jailbee.qtui.app.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    )

    with caplog.at_level(logging.DEBUG, logger="jailbee.qtui.app"):
        controller.on_action("destroy", "p-foo")

    # Outcome unchanged: the guard degrades to no risk shown, not a refusal.
    assert "Destroying loses this" not in question.call_args.args[2]
    # But the failure is now discoverable at debug level, traceback included.
    assert "boom" in caplog.text
    assert any(r.levelno == logging.DEBUG for r in caplog.records)


def test_destroy_unknown_git_status_gets_a_note(mocker, tmp_path):
    """git_status is None (base-tier refresh): say so rather than stay silent."""
    controller = _controller_with_group(mocker, tmp_path)
    controller._latest[0].containers[0].git_status = None
    question = mocker.patch(
        "jailbee.qtui.app.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    )

    controller.on_action("destroy", "p-foo")

    assert "unknown" in question.call_args.args[2].lower()


def test_destroy_unknown_note_is_the_same_sentence_the_cli_prints(mocker, tmp_path):
    """One container must not be described two different ways depending on
    which front-end asked; both render `unknown_status_warning`."""
    from jailbee.destroy_guard import unknown_status_warning

    controller = _controller_with_group(mocker, tmp_path)
    controller._latest[0].containers[0].git_status = None
    question = mocker.patch(
        "jailbee.qtui.app.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    )

    controller.on_action("destroy", "p-foo")

    assert unknown_status_warning(["p-foo"]) in question.call_args.args[2]


def test_destroy_mount_mode_gets_no_unknown_note(mocker, tmp_path):
    """A mount container's working tree is the host's and survives the
    destroy, so "may discard uncommitted work" would be provably false."""
    controller = _controller_with_group(mocker, tmp_path)
    ci = controller._latest[0].containers[0]
    ci.git_status = None
    ci.mode = "mount"
    question = mocker.patch(
        "jailbee.qtui.app.QMessageBox.question",
        return_value=QMessageBox.StandardButton.No,
    )

    controller.on_action("destroy", "p-foo")

    assert "unknown" not in question.call_args.args[2].lower()


def test_non_destroy_verbs_do_not_assess(mocker, tmp_path):
    controller = _controller_with_group(mocker, tmp_path)
    assess = mocker.patch("jailbee.destroy_guard.assess")
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_action("start", "p-foo")

    assess.assert_not_called()


def test_run_restores_layout_and_ignores_a_persisted_cadence(mocker):
    """The cadence is the state service's now; a value an older jailbee
    persisted stays in the row but reaches nothing."""
    mocker.patch("jailbee.qtui.app.QApplication")
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[Path("/x")])
    mock_window_cls = mocker.patch("jailbee.qtui.app.MainWindow")
    _mock_state_client(mocker)
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState

    mocker.patch("jailbee.qtui.app.seed_view_state", return_value=ViewState())
    mocker.patch(
        "jailbee.db.gui_state.load_gui_state",
        return_value=GuiState(layout="table", refresh_interval=7.0, refresh_paused=False),
    )
    mocker.patch("jailbee.db.gui_state.save_gui_state")

    qapp.run(None)

    _args, kwargs = mock_window_cls.call_args
    assert kwargs["layout"] == "table"
    assert not {"interval", "paused", "git_enabled"} & kwargs.keys()


def _new_container_groups():
    return [RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [])]


def test_on_new_container_warns_when_the_prefix_is_unknown(mocker):
    warn = mocker.patch.object(QMessageBox, "warning")
    # Patching subprocess.Popen also disables subprocess.run (run is built on
    # top of Popen), so no test in this group may perform other subprocess
    # work while this patch is active — see the repo history for a prior
    # incident where this silently broke an unrelated real subprocess.run
    # call in the same test.
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups(_new_container_groups())

    controller.on_new_container("")

    warn.assert_called_once()
    popen.assert_not_called()


def test_on_new_container_warns_for_an_orphan_group(mocker):
    """No config path, nothing to create against — same rule as the TUI.

    The message must actually name the orphan repo, not the generic "no repo
    selected" wording — a right-click on the orphan's own header already
    named a real prefix, so telling the user nothing was selected would be
    false (the bug FIX 2 in the review closed).
    """
    warn = mocker.patch.object(QMessageBox, "warning")
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups([RepoGroup("orphan", None, None, [])])

    controller.on_new_container("orphan")

    warn.assert_called_once()
    message = warn.call_args.args[2]
    assert "orphan" in message
    popen.assert_not_called()


def test_on_new_container_launches_in_a_terminal(mocker):
    """`jailbee new` asks its own questions, so it needs a real TTY — a
    detached Popen would hit the escalation prompt with no stdin."""
    from jailbee.qtui.prompts import NewContainerAnswers

    mocker.patch("jailbee.qtui.app.new_container_base_default", return_value="main")
    mocker.patch("jailbee.qtui.app.host_branches", return_value=("main",))
    dialog = mocker.Mock()
    dialog.exec.return_value = QDialog.DialogCode.Accepted
    dialog.answers.return_value = NewContainerAnswers(branch="feat-x", base="main")
    dialog_cls = mocker.patch("jailbee.qtui.app.NewContainerDialog", return_value=dialog)
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=mocker.sentinel.term)
    resolve = mocker.patch(
        "jailbee.qtui.app.resolve_launch", return_value=["xterm", "-e", "jailbee", "new"]
    )
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    client = mocker.Mock()
    controller = qapp.AppController(mocker.Mock(), client)
    controller.on_groups(_new_container_groups())

    controller.on_new_container("p")

    assert dialog_cls.call_args.kwargs["branches"] == ("main",)
    action = resolve.call_args.args[0]
    assert action.launch == "terminal"
    assert action.argv == [
        "jailbee",
        "new",
        "--config",
        "/repo/.jailbee/config.yaml",
        "--background",
        "--",
        "feat-x",
        "main",
    ]
    assert action.cwd == Path("/repo")
    popen.assert_called_once()
    assert popen.call_args.kwargs["cwd"] == Path("/repo")
    client.refresh.assert_called_once()


def test_on_new_container_omits_config_and_runs_a_scratch_repo_in_its_root(mocker):
    """`jailbee new` in a repo with no config file is addressed by its cwd:
    there is no path to pass, and the terminal wrapper must inherit the root."""
    from jailbee.qtui.prompts import NewContainerAnswers

    mocker.patch("jailbee.qtui.app.new_container_base_default", return_value="main")
    mocker.patch("jailbee.qtui.app.host_branches", return_value=("main",))
    dialog = mocker.Mock()
    dialog.exec.return_value = QDialog.DialogCode.Accepted
    dialog.answers.return_value = NewContainerAnswers(branch="feat-x", base="main")
    mocker.patch("jailbee.qtui.app.NewContainerDialog", return_value=dialog)
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=mocker.sentinel.term)
    resolve = mocker.patch(
        "jailbee.qtui.app.resolve_launch", return_value=["xterm", "-e", "jailbee", "new"]
    )
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups([RepoGroup("s", "/scratch", None, [])])

    controller.on_new_container("s")

    action = resolve.call_args.args[0]
    assert action.argv == ["jailbee", "new", "--background", "--", "feat-x", "main"]
    assert action.cwd == Path("/scratch")
    assert popen.call_args.kwargs["cwd"] == Path("/scratch")


def test_on_new_container_does_nothing_when_the_dialog_is_cancelled(mocker):
    mocker.patch("jailbee.qtui.app.new_container_base_default", return_value="main")
    mocker.patch("jailbee.qtui.app.host_branches", return_value=("main",))
    dialog = mocker.Mock()
    dialog.exec.return_value = QDialog.DialogCode.Rejected
    mocker.patch("jailbee.qtui.app.NewContainerDialog", return_value=dialog)
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups(_new_container_groups())

    controller.on_new_container("p")

    popen.assert_not_called()


def test_on_new_container_reports_a_missing_terminal(mocker):
    from jailbee.qtui.actions import TerminalNotFoundError
    from jailbee.qtui.prompts import NewContainerAnswers

    mocker.patch("jailbee.qtui.app.new_container_base_default", return_value="main")
    mocker.patch("jailbee.qtui.app.host_branches", return_value=("main",))
    dialog = mocker.Mock()
    dialog.exec.return_value = QDialog.DialogCode.Accepted
    dialog.answers.return_value = NewContainerAnswers(branch="feat-x", base="main")
    mocker.patch("jailbee.qtui.app.NewContainerDialog", return_value=dialog)
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=None)
    mocker.patch(
        "jailbee.qtui.app.resolve_launch", side_effect=TerminalNotFoundError("no terminal")
    )
    warn = mocker.patch.object(QMessageBox, "warning")
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups(_new_container_groups())

    controller.on_new_container("p")

    warn.assert_called_once()
    popen.assert_not_called()


def test_on_new_pr_container_launches_in_a_terminal_for_selected_repo(mocker):
    from PySide6.QtWidgets import QInputDialog

    prompt = mocker.patch.object(QInputDialog, "getInt", return_value=(123, True))
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=mocker.sentinel.term)
    resolve = mocker.patch(
        "jailbee.qtui.app.resolve_launch", return_value=["xterm", "-e", "jailbee", "new"]
    )
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups(_new_container_groups())

    controller.on_new_pr_container("p")

    assert prompt.call_args.kwargs["minValue"] == 1
    action = resolve.call_args.args[0]
    assert action.argv == [
        "jailbee",
        "new",
        "--config",
        "/repo/.jailbee/config.yaml",
        "--background",
        "--pr",
        "123",
    ]
    assert action.launch == "terminal"
    assert action.cwd == Path("/repo")
    assert popen.call_args.kwargs["cwd"] == Path("/repo")


def test_on_new_pr_container_cancel_does_not_launch(mocker):
    from PySide6.QtWidgets import QInputDialog

    mocker.patch.object(QInputDialog, "getInt", return_value=(1, False))
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups(_new_container_groups())

    controller.on_new_pr_container("p")

    popen.assert_not_called()


def test_on_new_pr_container_rejects_orphan_before_prompt(mocker):
    from PySide6.QtWidgets import QInputDialog

    prompt = mocker.patch.object(QInputDialog, "getInt")
    warn = mocker.patch.object(QMessageBox, "warning")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups([RepoGroup("orphan", None, None, [])])

    controller.on_new_pr_container("orphan")

    prompt.assert_not_called()
    assert "orphan" in warn.call_args.args[2]


def test_on_config_edit_launches_the_tui_in_a_terminal(mocker, tmp_path):
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    mocker.patch("jailbee.qtui.app.resolve_launch", side_effect=lambda action, _t: action.argv)
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups(
        [
            RepoGroup(
                prefix="demo",
                repo_root=str(tmp_path),
                config_path=tmp_path / ".jailbee" / "config.yaml",
                containers=[],
            )
        ]
    )

    controller.on_config_edit("demo", False)

    argv = popen.call_args.args[0]
    assert argv[:3] == ["jailbee", "config", "edit"]
    assert "--global" not in argv

    controller.on_config_edit("demo", True)
    assert "--global" in popen.call_args.args[0]


def test_on_config_edit_refuses_the_repo_layer_of_a_synthesized_config(mocker, tmp_path):
    """A repo with no config file of its own is refused for the repo layer and
    allowed for the global one — so the Qt call site has to pass `global_layer`
    through to the shared note, not just consult it.
    """
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    mocker.patch("jailbee.qtui.app.resolve_launch", side_effect=lambda action, _t: action.argv)
    warning = mocker.patch("jailbee.qtui.app.QMessageBox.warning")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups(
        [RepoGroup(prefix="demo", repo_root=str(tmp_path), config_path=None, containers=[])]
    )

    controller.on_config_edit("demo", False)

    popen.assert_not_called()
    warning.assert_called_once()

    controller.on_config_edit("demo", True)
    assert "--global" in popen.call_args.args[0]


def test_on_config_edit_refuses_an_orphan_group(mocker):
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    warning = mocker.patch("jailbee.qtui.app.QMessageBox.warning")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock())
    controller.on_groups(
        [RepoGroup(prefix="orphan", repo_root=None, config_path=None, containers=[])]
    )

    controller.on_config_edit("orphan", False)

    popen.assert_not_called()
    warning.assert_called_once()


def test_run_wires_new_container_signal(mocker):
    """A signal nobody connected is a dead menu item."""
    window = mocker.Mock()
    controller = mocker.Mock()
    qapp._wire(window, mocker.Mock(), controller)
    window.newContainerRequested.connect.assert_called_once_with(controller.on_new_container)


def test_run_wires_config_edit_signal(mocker):
    """Mirrors `test_run_wires_new_container_signal` for the new Config menu."""
    window = mocker.Mock()
    controller = mocker.Mock()
    qapp._wire(window, mocker.Mock(), controller)
    window.configEditRequested.connect.assert_called_once_with(controller.on_config_edit)


@pytest.mark.parametrize("operation", ["load", "delete"])
def test_run_persistence_error_still_joins_outbox_and_stops_refresh(
    qtbot, mocker, make_cfg, tmp_path, operation
):
    from threading import Event, Thread

    from jailbee.dashboard.model import RepoTarget
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState
    from jailbee.outbox_io import JournalStore
    from jailbee.qtui import outbox
    from tests.outbox_support import IDENTITY
    from tests.test_qtui_outbox import select, views

    cfg = make_cfg(tmp_path)
    view = views(tmp_path)
    entered, release, completed = Event(), Event(), Event()
    controllers, requests, notifications = [], [], []
    original_controller = qapp.AppController
    mocker.patch.object(qapp, "QApplication")
    fake_app = qapp.QApplication.instance.return_value
    mocker.patch.object(qapp, "collect_repo_roots", return_value=[tmp_path])
    window = MainWindow()
    qtbot.addWidget(window)
    mocker.patch.object(qapp, "MainWindow", return_value=window)
    mocker.patch.object(window, "show")  # Never open a user-display window.
    client = _mock_state_client(mocker, first=_snapshot([]))
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    mocker.patch.object(qapp, "seed_view_state", return_value=ViewState())
    mocker.patch.object(qapp, "dashboard_config_migration_notice", return_value=None)
    mocker.patch("jailbee.db.gui_state.load_gui_state", return_value=GuiState())
    failure = OSError("disk full")
    mocker.patch("jailbee.db.gui_state.save_gui_state", side_effect=failure)
    mocker.patch.object(outbox.config_api, "load_repo_config", return_value=cfg)
    mocker.patch.object(outbox.commands, "resolve_target", return_value=(cfg, IDENTITY.full_name))
    mocker.patch.object(outbox, "Incus", return_value=mocker.Mock())
    mocker.patch.object(outbox, "JournalStore", return_value=JournalStore(tmp_path / "journals"))
    read = mocker.patch.object(outbox.service, "load_container", return_value=view)
    mutation = mocker.patch.object(outbox.service, "execute_delete", return_value=())
    mocker.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes)

    def controller_factory(*args, **kwargs):
        controller = original_controller(*args, **kwargs)
        controllers.append(controller)
        return controller

    mocker.patch.object(qapp, "AppController", side_effect=controller_factory)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        completed.set()
        return view if operation == "load" else ()

    def event_loop():
        controller = controllers[0]
        controller._open_outbox(RepoTarget(cfg.repo_root, None), IDENTITY.full_name)
        dialog = next(iter(controller._outboxes.values()))
        qtbot.waitUntil(lambda: not dialog.busy)
        dialog.changed.connect(lambda: notifications.append(QThread.currentThread()))
        if operation == "load":
            read.side_effect = blocked
            dialog.refresh()
        else:
            mutation.side_effect = blocked
            select(dialog, action=0, comment=0)
            dialog.delete_selected()
        qtbot.waitUntil(entered.is_set)
        requests.append(dialog._request)
        return 0

    fake_app.exec.side_effect = event_loop
    releaser = Thread(target=lambda: (entered.wait(3) and release.wait(0.1)) or release.set())
    releaser.start()
    try:
        with pytest.raises(OSError) as caught:
            qapp.run(None)
        assert caught.value is failure
        assert completed.is_set()
        assert not requests[0].isRunning()
        assert not controllers[0]._outboxes
        assert len(notifications) == (1 if operation == "delete" else 0)
        assert all(thread is QThread.currentThread() for thread in notifications)
        client.close.assert_called_once()
    finally:
        release.set()
        releaser.join(3)
        controllers[0]._finish_outboxes()


def test_run_fills_the_window_before_showing_it(mocker):
    """The first snapshot is waited for and shown before `show()`, so the
    window is never seen blank."""
    window = mocker.patch("jailbee.qtui.app.MainWindow").return_value
    client = _patch_run(mocker)

    qapp.run(None)

    client.start.assert_called_once()
    client.wait_first_snapshot.assert_called_once_with(qapp.STARTUP_TIMEOUT_SECONDS)
    names = [c[0] for c in window.method_calls]
    assert names.index("set_groups") < names.index("show")


def test_run_still_shows_the_window_when_the_service_is_unavailable(mocker):
    """Unlike the TUI, the GUI has nowhere to print — a window that never
    appears is a worse report of an unreachable state service than one
    carrying the error in its status bar. The client keeps reconnecting."""
    window = mocker.patch("jailbee.qtui.app.MainWindow").return_value
    _patch_run(mocker, unavailable="state service did not start")

    qapp.run(None)

    window.show.assert_called_once()
    window.set_refresh_failed.assert_called_once()
    assert "state service did not start" in window.set_refresh_failed.call_args.args[0]
    names = [c[0] for c in window.method_calls]
    assert names.index("set_refresh_failed") < names.index("show")


def test_run_closes_the_client_on_exit(mocker):
    mocker.patch("jailbee.qtui.app.MainWindow")
    client = _patch_run(mocker)

    qapp.run(None)

    client.close.assert_called_once()


def test_run_closes_the_client_even_when_persisting_fails(mocker):
    mocker.patch("jailbee.qtui.app.MainWindow")
    client = _patch_run(mocker)
    mocker.patch("jailbee.db.gui_state.save_gui_state", side_effect=OSError("disk full"))

    with pytest.raises(OSError):
        qapp.run(None)

    client.close.assert_called_once()


def test_sort_persistence_survives_write_failure(mocker):
    mocker.patch("jailbee.db.view_prefs.save_view_state", side_effect=OSError("disk full"))
    window = mocker.Mock()
    window.enabled_columns.return_value = ("name", "state")
    window.collapsed_repos.return_value = set()
    window.hidden_repos.return_value = set()
    controller = qapp.AppController(window, mocker.Mock(), engine=mocker.sentinel.engine)

    controller.on_sort_changed()

    window.set_status.assert_called_once()
    assert "disk full" in window.set_status.call_args.args[0]


def _bulk_controller(mocker, tmp_path, **kw):
    from jailbee.lifecycle import ContainerInfo

    controller = _controller_with_group(mocker, tmp_path, **kw)
    controller._latest[0].containers.append(
        ContainerInfo(name="p-bar", state="Running", network="strict", ip=None, memory_limit=None)
    )
    return controller


def test_bulk_stop_launches_one_detached_child_per_container(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_bulk_action("stop", ["p-foo", "p-bar"])

    assert [c.args[0][:3] for c in popen.call_args_list] == [
        ["jailbee", "stop", "p-foo"],
        ["jailbee", "stop", "p-bar"],
    ]


def test_bulk_destroy_asks_once_and_forces(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    mocker.patch("jailbee.dashboard.bulk.destroy_risk_lines", return_value=("⚠ p-foo: dirty",))
    question = mocker.patch(
        "jailbee.qtui.app.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes
    )
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_bulk_action("destroy", ["p-foo", "p-bar"])

    question.assert_called_once()
    assert "⚠ p-foo: dirty" in question.call_args.args[2]
    assert all("--force" in c.args[0] for c in popen.call_args_list)
    assert len(popen.call_args_list) == 2


def test_bulk_destroy_declined_launches_nothing(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    mocker.patch("jailbee.dashboard.bulk.destroy_risk_lines", return_value=())
    mocker.patch(
        "jailbee.qtui.app.QMessageBox.question", return_value=QMessageBox.StandardButton.No
    )
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_bulk_action("destroy", ["p-foo", "p-bar"])

    popen.assert_not_called()


def test_bulk_loose_asks_the_ttl_once(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    ask = mocker.patch("jailbee.qtui.app.QInputDialog.getItem", return_value=("2h", True))
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_bulk_action("net loose", ["p-foo", "p-bar"])

    ask.assert_called_once()
    assert [c.args[0][-2:] for c in popen.call_args_list] == [["--for", "2h"], ["--for", "2h"]]


def test_bulk_push_opens_one_output_window_for_the_repo(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    mocker.patch("jailbee.qtui.app.push_questions", return_value=(False, False))
    output = mocker.patch.object(controller, "_open_output")

    controller.on_bulk_action("git push", ["p-foo", "p-bar"])

    output.assert_called_once()
    assert output.call_args.args[0][:5] == ["jailbee", "git", "push", "p-foo", "p-bar"]


def test_bulk_pull_confirms_once_and_runs_once(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    question = mocker.patch(
        "jailbee.qtui.app.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes
    )
    output = mocker.patch.object(controller, "_open_output")

    controller.on_bulk_action("git pull", ["p-foo", "p-bar"])

    question.assert_called_once()
    assert output.call_args.args[0][:5] == ["jailbee", "git", "pull", "p-foo", "p-bar"]


def test_bulk_merge_opens_one_terminal(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    spawn = mocker.patch.object(controller, "_spawn", return_value=True)

    controller.on_bulk_action("merge", ["p-foo", "p-bar"])

    action = spawn.call_args.args[0]
    assert action.argv[:4] == ["jailbee", "merge", "p-foo", "p-bar"]
    assert "--into" not in action.argv
    assert action.launch == "terminal"


def _second_repo(controller, tmp_path):
    from jailbee.dashboard.model import RepoGroup
    from jailbee.lifecycle import ContainerInfo

    root = tmp_path / "other"
    cfg = root / ".jailbee" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("container_prefix: q\n")
    controller._latest.append(
        RepoGroup(
            prefix="q",
            repo_root=str(root),
            config_path=cfg,
            containers=[
                ContainerInfo(
                    name="q-baz", state="Running", network="strict", ip=None, memory_limit=None
                )
            ],
            loose_ttl_default="5m",
            push_action_default="ask",
            push_source_default="base",
        )
    )
    return cfg


def test_bulk_nothing_eligible_informs_and_launches_nothing(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    controller._latest[0].containers[0].state = "Stopped"
    controller._latest[0].containers[1].state = "Stopped"
    info = mocker.patch("jailbee.qtui.app.QMessageBox.information")
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_bulk_action("stop", ["p-foo", "p-bar"])

    info.assert_called_once()
    assert "nothing to do" in info.call_args.args[2]
    popen.assert_not_called()


def test_bulk_partial_skip_runs_eligible_and_reports_skipped(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    controller._latest[0].containers[1].state = "Stopped"
    warn = mocker.patch("jailbee.qtui.app.QMessageBox.warning")
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_bulk_action("stop", ["p-foo", "p-bar"])

    assert [c.args[0][:3] for c in popen.call_args_list] == [["jailbee", "stop", "p-foo"]]
    warn.assert_called_once()
    assert "p-bar" in warn.call_args.args[2]


def test_bulk_parallel_across_repos_uses_each_repos_config(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    cfg2 = _second_repo(controller, tmp_path)
    mocker.patch("jailbee.qtui.app.QMessageBox.warning")
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")

    controller.on_bulk_action("stop", ["p-foo", "q-baz"])

    argvs = {c.args[0][2]: c.args[0] for c in popen.call_args_list}
    assert argvs["p-foo"][argvs["p-foo"].index("--config") + 1] == str(
        tmp_path / ".jailbee" / "config.yaml"
    )
    assert argvs["q-baz"][argvs["q-baz"].index("--config") + 1] == str(cfg2)


def test_bulk_partial_launch_failure_one_summary_and_refresh(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    _second_repo(controller, tmp_path)
    warn = mocker.patch("jailbee.qtui.app.QMessageBox.warning")
    popen = mocker.patch(
        "jailbee.qtui.app.subprocess.Popen", side_effect=[None, OSError("boom"), None]
    )
    refresh = mocker.patch.object(controller, "_request_refresh")

    controller.on_bulk_action("stop", ["p-foo", "p-bar", "q-baz"])

    assert popen.call_count == 3
    warn.assert_called_once()
    assert "p-bar: boom" in warn.call_args.args[2]
    assert "p-foo" not in warn.call_args.args[2]
    refresh.assert_called_once()


def test_bulk_all_launches_failing_does_not_refresh(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    warn = mocker.patch("jailbee.qtui.app.QMessageBox.warning")
    mocker.patch("jailbee.qtui.app.subprocess.Popen", side_effect=OSError("boom"))
    refresh = mocker.patch.object(controller, "_request_refresh")

    controller.on_bulk_action("stop", ["p-foo", "p-bar"])

    warn.assert_called_once()
    refresh.assert_not_called()


def test_bulk_push_cancelled_dialog_runs_nothing(mocker, tmp_path):
    controller = _bulk_controller(mocker, tmp_path)
    mocker.patch.object(controller, "_collect_answers", return_value=None)
    output = mocker.patch.object(controller, "_open_output")
    spawn = mocker.patch.object(controller, "_spawn")

    controller.on_bulk_action("git push", ["p-foo", "p-bar"])

    output.assert_not_called()
    spawn.assert_not_called()
