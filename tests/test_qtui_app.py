import logging

import pytest

pytest.importorskip("PySide6")

from pathlib import Path

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QDialog, QMessageBox

from jailbee.dashboard import RepoGroup
from jailbee.git_status import GitStatus
from jailbee.qtui import app as qapp
from jailbee.qtui.refresh import RefreshWorker
from jailbee.qtui.window import MainWindow


def test_preflight_returns_none_when_no_configs(mocker):
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[])
    assert qapp.preflight(None) is None


def test_preflight_returns_paths_when_present(mocker):
    paths = [Path("/repo/.gie/config.yaml")]
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=paths)
    assert qapp.preflight(Path("/repo/.gie/config.yaml")) == paths


def test_run_returns_1_when_no_configs(mocker):
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[])
    rc = qapp.run(mocker.Mock(), None, interval=3.0, git_interval=10.0, no_git=False)
    assert rc == 1


def test_the_launch_guard_message_is_the_tuis_own(mocker):
    """The TUI, the Qt window and `cli`'s pre-detach check all print the same
    sentence — one constant rather than three copies that drift apart."""
    import jailbee.dashboard as dash

    assert qapp.NOTHING_TO_SHOW is dash.NOTHING_TO_SHOW


def test_on_groups_updates_tree_and_status_bar(mocker):
    groups = [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [])]
    window = mocker.Mock()
    controller = qapp.AppController(window, mocker.Mock(), interval=3.0)

    controller.on_groups(groups)

    window.set_groups.assert_called_once()
    window.set_refresh_ok.assert_called_once()
    assert window.set_refresh_ok.call_args.kwargs["interval"] == 3.0


def test_on_failed_updates_status_bar_not_a_modal(mocker):
    """A failed gather must not pop a QMessageBox — FIX 1 makes the worker
    keep retrying, so a modal per failure would spam the user."""
    window = mocker.Mock()
    controller = qapp.AppController(window, mocker.Mock(), interval=3.0)
    critical = mocker.patch("jailbee.qtui.app.QMessageBox.critical")

    controller.on_failed("boom")

    window.set_refresh_failed.assert_called_once_with("boom")
    critical.assert_not_called()


def test_on_interval_changed_updates_stored_interval_and_status(mocker):
    window = mocker.Mock()
    controller = qapp.AppController(window, mocker.Mock(), interval=3.0)
    now = mocker.patch("jailbee.qtui.app.datetime")
    now.now.return_value.astimezone.return_value = "the-time"

    controller.on_groups([])  # establishes a last-refresh timestamp
    window.reset_mock()

    controller.on_interval_changed(10.0)

    assert controller._interval == 10.0
    assert controller._paused is False
    window.set_refresh_ok.assert_called_once_with(at="the-time", interval=10.0, paused=False)


def test_on_auto_refresh_disabled_marks_paused_and_updates_status(mocker):
    window = mocker.Mock()
    controller = qapp.AppController(window, mocker.Mock(), interval=3.0)
    now = mocker.patch("jailbee.qtui.app.datetime")
    now.now.return_value.astimezone.return_value = "the-time"

    controller.on_groups([])
    window.reset_mock()

    controller.on_auto_refresh_disabled()

    assert controller._paused is True
    window.set_refresh_ok.assert_called_once_with(at="the-time", interval=3.0, paused=True)


def test_run_wires_window_signals_to_controller_not_worker(mocker):
    """Assert the three MainWindow signals connect ONLY to the controller,
    never directly to a worker bound method.

    ``RefreshWorker.run_loop`` is a blocking loop, not a Qt event loop, so a
    signal connected directly from the GUI thread to a worker method would
    resolve to a queued connection the worker thread never processes (it
    silently never fires). All worker control must be routed through an
    ``AppController`` slot that calls the worker method directly instead.
    """
    mocker.patch("jailbee.qtui.app.QApplication")
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[Path("/x")])
    mock_window_cls = mocker.patch("jailbee.qtui.app.MainWindow")
    window = mock_window_cls.return_value
    mocker.patch("jailbee.qtui.app.QThread")
    mock_worker_cls = mocker.patch("jailbee.qtui.app.RefreshWorker")
    worker = mock_worker_cls.return_value
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState

    mocker.patch("jailbee.qtui.app.seed_view_state", return_value=ViewState())
    mocker.patch("jailbee.db.gui_state.load_gui_state", return_value=GuiState())
    # persist_on_close() (in run()'s `finally`) also calls save_gui_state —
    # patch it too so it doesn't try to open a real session on the sentinel.
    mocker.patch("jailbee.db.gui_state.save_gui_state")

    qapp.run(mocker.Mock(), None, interval=3.0, git_interval=10.0, no_git=False)

    refresh_targets = [c.args[0] for c in window.refreshRequested.connect.call_args_list]
    assert worker.force not in refresh_targets
    assert len(refresh_targets) == 1

    interval_targets = [c.args[0] for c in window.intervalChanged.connect.call_args_list]
    assert worker.set_interval not in interval_targets
    assert len(interval_targets) == 1

    auto_disabled_targets = [c.args[0] for c in window.autoRefreshDisabled.connect.call_args_list]
    assert worker.set_paused not in auto_disabled_targets
    assert not any(getattr(t, "__name__", "") == "<lambda>" for t in auto_disabled_targets)
    assert len(auto_disabled_targets) == 1

    layout_targets = [c.args[0] for c in window.layoutChanged.connect.call_args_list]
    assert len(layout_targets) == 1

    card_style_targets = [c.args[0] for c in window.cardStyleChanged.connect.call_args_list]
    assert len(card_style_targets) == 1

    collapsed_targets = [
        c.args[0] for c in window.card_view.collapsedChanged.connect.call_args_list
    ]
    assert len(collapsed_targets) == 1

    columns_targets = [c.args[0] for c in window.columnsChanged.connect.call_args_list]
    assert len(columns_targets) == 1


def test_groups_ready_from_worker_thread_handled_on_main_thread(qtbot, mocker):
    """Regression test for the threading bug: worker signals emitted from a
    background QThread must be handled on the GUI (main) thread, not the
    worker thread. Before the ``AppController`` fix, connecting a plain
    closure to ``worker.groupsReady`` resolved to a Direct (same-thread)
    connection, so the handler ran on the worker thread and mutated widgets
    off the GUI thread.
    """
    groups = [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [])]
    mocker.patch("jailbee.qtui.refresh.gather_live", return_value=groups)

    main_thread = QThread.currentThread()
    handled_on: list[QThread] = []

    window = mocker.Mock()

    def _set_groups(_groups: object, *, now: object) -> None:
        handled_on.append(QThread.currentThread())

    window.set_groups.side_effect = _set_groups

    worker = RefreshWorker(
        incus=mocker.Mock(),
        cwd_root=Path("/repo"),
        interval=0.5,
        git_interval=10.0,
        git_enabled=True,
    )
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run_loop)

    # Constructed here (the test's/main thread) and never moved off it —
    # mirrors how `run()` wires the controller.
    controller = qapp.AppController(window, worker, interval=0.5)
    worker.groupsReady.connect(controller.on_groups)

    with qtbot.waitSignal(worker.groupsReady, timeout=3000):
        thread.start()

    qtbot.waitUntil(lambda: len(handled_on) == 1, timeout=3000)

    worker.request_stop()
    thread.quit()
    assert thread.wait(3000)

    # Sanity check the test is non-vacuous: the worker really did run on a
    # different thread than the one handling the signal.
    assert thread is not main_thread
    assert handled_on[0] is main_thread
    window.set_groups.assert_called_once()


def test_wire_delivers_interval_and_force_to_a_real_worker_thread(qtbot, mocker):
    """Regression test for the queued-connection bug: ``RefreshWorker.run_loop``
    is a blocking ``while`` loop, not a Qt event loop, so a signal connected
    directly from the (main-thread) window to a worker bound method resolves
    to a *queued* connection the worker thread never processes — it's
    silently dropped forever.

    Uses a REAL, started ``QThread`` (not a mock) so this fails against the
    pre-fix wiring (``window.intervalChanged.connect(worker.set_interval)``
    and ``window.refreshRequested.connect(worker.force)``): under that
    wiring neither ``qtbot.waitUntil`` below would ever observe the change,
    and the test would time out. It exercises ``app._wire`` — the same
    helper ``run()`` uses — so production and test wiring are identical.
    """
    groups = [RepoGroup("p", "/repo", Path("/repo/.gie/config.yaml"), [])]
    mocker.patch("jailbee.qtui.refresh.gather_live", return_value=groups)

    window = MainWindow(git_enabled=True, interval=0.5)
    qtbot.addWidget(window)

    worker = RefreshWorker(
        incus=mocker.Mock(),
        cwd_root=Path("/repo"),
        interval=0.5,
        git_interval=10.0,
        git_enabled=True,
    )
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run_loop)

    # Constructed here (the main thread) and never moved off it — mirrors
    # how `run()` wires the controller.
    controller = qapp.AppController(window, worker, interval=0.5)
    qapp._wire(window, worker, controller)

    try:
        with qtbot.waitSignal(worker.groupsReady, timeout=3000):
            thread.start()

        window.intervalChanged.emit(2.5)
        qtbot.waitUntil(lambda: worker._interval == 2.5, timeout=3000)

        # Pause the worker so that NO periodic gather can fire during the
        # refresh-force test. Only the manual force() call can produce a
        # groupsReady signal, making this assertion depend only on force()
        # wiring, not on periodic tick luck.
        worker.set_paused(True)

        with qtbot.waitSignal(worker.groupsReady, timeout=3000):
            window.refreshRequested.emit()
    finally:
        worker.request_stop()
        worker.force()
        thread.quit()
        assert thread.wait(3000)


def test_controller_persists_on_layout_change(mocker):
    save = mocker.patch("jailbee.db.gui_state.save_gui_state")
    window = mocker.Mock()
    window.current_layout.return_value = "table"
    window.table_header_state.return_value = "Zm9v"
    window.current_card_style.return_value = "compact"
    controller = qapp.AppController(
        window, mocker.Mock(), interval=3.0, engine=mocker.sentinel.engine
    )
    controller.on_layout_changed("table")
    save.assert_called_once()
    engine_arg, state_arg = save.call_args.args
    assert engine_arg is mocker.sentinel.engine
    assert state_arg.layout == "table"
    assert state_arg.table_header_state == "Zm9v"
    assert state_arg.refresh_interval == 3.0


def test_controller_persist_is_noop_without_engine(mocker):
    save = mocker.patch("jailbee.db.gui_state.save_gui_state")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock(), interval=3.0)
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
    controller = qapp.AppController(
        window, mocker.Mock(), interval=3.0, engine=mocker.sentinel.engine
    )

    controller.on_collapsed_changed()

    save_view.assert_called_once()
    engine_arg, frontend_arg, state_arg = save_view.call_args.args
    assert engine_arg is mocker.sentinel.engine
    assert frontend_arg == "qt"
    assert state_arg.columns == ("name", "state")
    assert state_arg.folded == frozenset({"p", "q"})
    # The two writers must never clobber each other's row.
    save_gui.assert_not_called()


def test_on_columns_changed_persists_view_state(mocker):
    """The Columns menu's toggle must also land in `view_prefs`, carrying
    whatever the window's own folded set currently is."""
    save_view = mocker.patch("jailbee.db.view_prefs.save_view_state")
    window = mocker.Mock()
    window.enabled_columns.return_value = ("name", "ip")
    window.collapsed_repos.return_value = set()
    controller = qapp.AppController(
        window, mocker.Mock(), interval=3.0, engine=mocker.sentinel.engine
    )

    controller.on_columns_changed()

    save_view.assert_called_once()
    _engine_arg, frontend_arg, state_arg = save_view.call_args.args
    assert frontend_arg == "qt"
    assert state_arg.columns == ("name", "ip")
    assert state_arg.folded == frozenset()


def test_on_columns_changed_repaints_immediately(mocker):
    """A column toggle must reach the table right away, not on whatever the
    next refresh tick happens to push — with "Off (manual)" refresh, that
    tick may never come, and the Columns menu would look completely inert.
    This fails if on_columns_changed goes back to only persisting."""
    mocker.patch("jailbee.db.view_prefs.save_view_state")
    groups = [RepoGroup("p", "/repo", Path("/repo/.jailbee/config.yaml"), [])]
    window = mocker.Mock()
    window.enabled_columns.return_value = ("name", "ip")
    window.collapsed_repos.return_value = set()
    controller = qapp.AppController(
        window, mocker.Mock(), interval=3.0, engine=mocker.sentinel.engine
    )
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
    controller = qapp.AppController(
        window, mocker.Mock(), interval=3.0, engine=mocker.sentinel.engine
    )

    controller.on_columns_changed()

    window.set_groups.assert_not_called()


def test_persist_view_state_is_noop_without_engine(mocker):
    save_view = mocker.patch("jailbee.db.view_prefs.save_view_state")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock(), interval=3.0)

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
    controller = qapp.AppController(
        window, mocker.Mock(), interval=3.0, engine=mocker.sentinel.engine
    )

    controller.on_layout_changed("table")

    save_view.assert_not_called()


def test_persist_on_close_writes_snapshot(mocker):
    save = mocker.patch("jailbee.db.gui_state.save_gui_state")
    window = mocker.Mock()
    window.current_layout.return_value = "cards"
    window.table_header_state.return_value = "AAAA"
    window.current_card_style.return_value = "grid"
    controller = qapp.AppController(
        window, mocker.Mock(), interval=5.0, engine=mocker.sentinel.engine, paused=True
    )
    controller.persist_on_close()
    _engine, state = save.call_args.args
    assert state.layout == "cards"
    assert state.table_header_state == "AAAA"
    assert state.refresh_interval == 5.0
    assert state.refresh_paused is True
    assert state.card_style == "grid"


def test_persist_writes_card_style(qtbot, mocker):
    """Round-trips through a real in-memory engine (not a mocked
    save_gui_state) to exercise the actual GuiState(...) construction in
    _persist, mirroring test_db_gui_state.py's _engine() helper."""
    from sqlmodel import SQLModel, create_engine

    from jailbee.db.gui_state import load_gui_state

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)

    window = MainWindow(git_enabled=True, interval=0.5)
    qtbot.addWidget(window)
    controller = qapp.AppController(window, mocker.Mock(), interval=3.0, engine=engine)

    window._switch_card_style("grid")
    controller.on_card_style_changed("grid")

    saved = load_gui_state(engine)
    assert saved.card_style == "grid"


def test_on_card_style_changed_persists(mocker):
    save = mocker.patch("jailbee.db.gui_state.save_gui_state")
    window = mocker.Mock()
    window.current_card_style.return_value = "grid"
    controller = qapp.AppController(
        window, mocker.Mock(), interval=3.0, engine=mocker.sentinel.engine
    )

    controller.on_card_style_changed("grid")

    save.assert_called_once()
    _engine_arg, state_arg = save.call_args.args
    assert state_arg.card_style == "grid"


def test_run_restores_card_style(mocker):
    mocker.patch("jailbee.qtui.app.QApplication")
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[Path("/x")])
    mock_window_cls = mocker.patch("jailbee.qtui.app.MainWindow")
    mocker.patch("jailbee.qtui.app.QThread")
    mocker.patch("jailbee.qtui.app.RefreshWorker")
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState

    mocker.patch("jailbee.qtui.app.seed_view_state", return_value=ViewState())
    mocker.patch(
        "jailbee.db.gui_state.load_gui_state",
        return_value=GuiState(
            layout="cards",
            refresh_interval=7.0,
            refresh_paused=False,
            card_style="grid",
        ),
    )
    mocker.patch("jailbee.db.gui_state.save_gui_state")

    qapp.run(mocker.Mock(), None, interval=None, git_interval=10.0, no_git=False)

    _args, kwargs = mock_window_cls.call_args
    assert kwargs["card_style"] == "grid"


def test_run_restores_enabled_columns_and_folded_repos(mocker):
    """`run()` must seed the window's Columns menu and the card view's fold
    state from the Qt front-end's own `view_prefs` row — not from the TUI's."""
    mocker.patch("jailbee.qtui.app.QApplication")
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[Path("/x")])
    mock_window_cls = mocker.patch("jailbee.qtui.app.MainWindow")
    window = mock_window_cls.return_value
    mocker.patch("jailbee.qtui.app.QThread")
    mocker.patch("jailbee.qtui.app.RefreshWorker")
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState

    mocker.patch(
        "jailbee.qtui.app.seed_view_state",
        return_value=ViewState(columns=("name", "ip"), folded=frozenset({"repo-a"})),
    )
    mocker.patch("jailbee.db.gui_state.load_gui_state", return_value=GuiState())
    mocker.patch("jailbee.db.gui_state.save_gui_state")

    qapp.run(mocker.Mock(), None, interval=3.0, git_interval=10.0, no_git=False)

    _args, kwargs = mock_window_cls.call_args
    assert kwargs["enabled_columns"] == ("name", "ip")
    window.card_view.set_collapsed.assert_called_once_with({"repo-a"})


def _controller_with_group(
    mocker,
    tmp_path,
    *,
    loose_ttl_default="5m",
    push_action_default="ask",
    push_source_default="base",
    base_branch=None,
    pr_number=None,
):
    """An AppController holding one snapshot row, so on_action can resolve a config."""
    from jailbee.dashboard import RepoGroup
    from jailbee.lifecycle import ContainerInfo

    window = mocker.Mock()
    worker = mocker.Mock()
    controller = qapp.AppController(window, worker, interval=3.0)
    ci = ContainerInfo(
        name="p-foo",
        state="Running",
        network="strict",
        ip=None,
        memory_limit=None,
        base_branch=base_branch,
        pr_number=pr_number,
    )
    config_path = tmp_path / ".gie" / "config.yaml"
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
            config_path=config_path,
            containers=[ci],
            loose_ttl_default=loose_ttl_default,
            push_action_default=push_action_default,
            push_source_default=push_source_default,
        )
    ]
    return controller


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
    """A malformed `.gie/config.yaml` must not block the destroy guard — the
    outcome stays "no risk shown", same as before — but the failure must be
    discoverable rather than vanishing into a bare `except Exception: pass`."""
    controller = _controller_with_group(mocker, tmp_path)
    controller._latest[0].containers[0].git_status = GitStatus(
        wt="+12 -3", ahead_diff="clean", ahead_count="0", conflict="ok"
    )
    mocker.patch("jailbee.config.load_config", side_effect=ValueError("boom"))
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


def test_run_uses_persisted_interval_when_cli_none(mocker):
    mocker.patch("jailbee.qtui.app.QApplication")
    mocker.patch("jailbee.qtui.app.collect_repo_roots", return_value=[Path("/x")])
    mock_window_cls = mocker.patch("jailbee.qtui.app.MainWindow")
    mocker.patch("jailbee.qtui.app.QThread")
    mocker.patch("jailbee.qtui.app.RefreshWorker")
    mocker.patch("jailbee.db.get_engine", return_value=mocker.sentinel.engine)
    from jailbee.db.models import GuiState
    from jailbee.db.view_prefs import ViewState

    mocker.patch("jailbee.qtui.app.seed_view_state", return_value=ViewState())
    mocker.patch(
        "jailbee.db.gui_state.load_gui_state",
        return_value=GuiState(layout="table", refresh_interval=7.0, refresh_paused=False),
    )
    mocker.patch("jailbee.db.gui_state.save_gui_state")

    qapp.run(mocker.Mock(), None, interval=None, git_interval=10.0, no_git=False)

    _args, kwargs = mock_window_cls.call_args
    assert kwargs["interval"] == 7.0
    assert kwargs["layout"] == "table"


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
    controller = qapp.AppController(mocker.Mock(), mocker.Mock(), interval=3.0)
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
    controller = qapp.AppController(mocker.Mock(), mocker.Mock(), interval=3.0)
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
    dialog = mocker.Mock()
    dialog.exec.return_value = QDialog.DialogCode.Accepted
    dialog.answers.return_value = NewContainerAnswers(branch="feat-x", base="main")
    mocker.patch("jailbee.qtui.app.NewContainerDialog", return_value=dialog)
    mocker.patch("jailbee.qtui.app.detect_terminal", return_value=mocker.sentinel.term)
    resolve = mocker.patch(
        "jailbee.qtui.app.resolve_launch", return_value=["xterm", "-e", "jailbee", "new"]
    )
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    worker = mocker.Mock()
    controller = qapp.AppController(mocker.Mock(), worker, interval=3.0)
    controller.on_groups(_new_container_groups())

    controller.on_new_container("p")

    action = resolve.call_args.args[0]
    assert action.launch == "terminal"
    assert action.argv == [
        "jailbee",
        "new",
        "feat-x",
        "main",
        "--config",
        "/repo/.jailbee/config.yaml",
    ]
    popen.assert_called_once()
    worker.force.assert_called_once()


def test_on_new_container_does_nothing_when_the_dialog_is_cancelled(mocker):
    mocker.patch("jailbee.qtui.app.new_container_base_default", return_value="main")
    dialog = mocker.Mock()
    dialog.exec.return_value = QDialog.DialogCode.Rejected
    mocker.patch("jailbee.qtui.app.NewContainerDialog", return_value=dialog)
    popen = mocker.patch("jailbee.qtui.app.subprocess.Popen")
    controller = qapp.AppController(mocker.Mock(), mocker.Mock(), interval=3.0)
    controller.on_groups(_new_container_groups())

    controller.on_new_container("p")

    popen.assert_not_called()


def test_on_new_container_reports_a_missing_terminal(mocker):
    from jailbee.qtui.actions import TerminalNotFoundError
    from jailbee.qtui.prompts import NewContainerAnswers

    mocker.patch("jailbee.qtui.app.new_container_base_default", return_value="main")
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
    controller = qapp.AppController(mocker.Mock(), mocker.Mock(), interval=3.0)
    controller.on_groups(_new_container_groups())

    controller.on_new_container("p")

    warn.assert_called_once()
    popen.assert_not_called()


def test_run_wires_new_container_signal(mocker):
    """A signal nobody connected is a dead menu item."""
    window = mocker.Mock()
    controller = mocker.Mock()
    qapp._wire(window, mocker.Mock(), controller)
    window.newContainerRequested.connect.assert_called_once_with(controller.on_new_container)
