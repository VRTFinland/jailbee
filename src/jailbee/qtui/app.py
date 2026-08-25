"""QApplication bootstrap and wiring for the Qt dashboard.

``run`` has the same signature as ``dashboard.run`` so ``cli.py`` can dispatch
to either. The GUI opens no new Incus paths — it reuses the dashboard data
layer and executes actions as ``jailbee`` subprocesses.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Slot
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox

from jailbee.dashboard import collect_config_paths, seed_view_state
from jailbee.db.view_prefs import FRONTEND_QT
from jailbee.qtui.actions import (
    TerminalNotFoundError,
    build_action,
    resolve_launch,
)
from jailbee.qtui.output import CommandOutputDialog
from jailbee.qtui.prompts import (
    PrOptionsDialog,
    PushOptionsDialog,
    confirm_text,
    pr_flags,
    pr_refresh_title,
    push_flags,
    push_questions,
)
from jailbee.qtui.refresh import RefreshWorker
from jailbee.qtui.terminal import detect_terminal
from jailbee.qtui.window import MainWindow
from jailbee.tui import error

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jailbee.dashboard import RepoGroup
    from jailbee.incus import Incus
    from jailbee.lifecycle import ContainerInfo

log = logging.getLogger(__name__)


def preflight(cwd_config: Path | None) -> list[Path] | None:
    """Resolve the config paths the dashboard would show, or None if there are none.

    A launch-time guard only ("nothing to show, don't open a window") — the
    returned list is deliberately not handed to the worker, which re-resolves
    it per gather via ``dashboard.gather_live`` so a repo registered while the
    window is open stops rendering as a menu-less orphan.
    """
    config_paths = collect_config_paths(cwd_config)
    return config_paths or None


def _group_for(groups: list[RepoGroup], container_name: str) -> RepoGroup | None:
    for g in groups:
        for c in g.containers:
            if c.name == container_name:
                return g
    return None


def _env() -> dict[str, str]:
    return dict(os.environ)


class AppController(QObject):
    """Routes worker/window signals to GUI-thread slots.

    Constructed with default (main-thread) affinity and never moved to
    another thread. Because its handlers are ``@Slot``-decorated bound
    methods of a ``QObject`` living on the GUI thread, Qt resolves
    cross-thread signal connections (e.g. from the background
    ``RefreshWorker``) as *queued* rather than direct — so the handlers
    always run on the GUI thread, never on the worker thread.
    """

    def __init__(
        self,
        window: MainWindow,
        worker: RefreshWorker,
        *,
        interval: float,
        engine: object | None = None,
        paused: bool = False,
        columns: Sequence[str] | None = None,
    ) -> None:
        super().__init__()
        self._window = window
        self._worker = worker
        self._interval = interval
        self._engine = engine
        self._paused = paused
        # An explicit override for `on_groups` to force onto `window.set_groups`;
        # `run()` no longer sets this — the window owns the live, menu-driven
        # enabled-column set (`self._window.enabled_columns()`), and passing
        # `None` here each tick lets `set_groups` fall back to that instead of
        # re-forcing a startup snapshot that would otherwise mask every toggle
        # in the Columns menu.
        self._columns = columns
        # Timestamp of the last successful refresh, so a cadence change (a
        # menu action, not a refresh) can still update the status line
        # immediately instead of waiting for the next gather.
        self._last_refresh_at: datetime | None = None
        # Latest snapshot, kept for resolving a clicked action's config path.
        self._latest: list[RepoGroup] = []

    @Slot(object)
    def on_groups(self, groups: list[RepoGroup]) -> None:
        self._latest = groups
        now = datetime.now().astimezone()
        self._last_refresh_at = now
        self._window.set_groups(groups, now=now, columns=self._columns)
        self._window.set_refresh_ok(at=now, interval=self._interval, paused=self._paused)

    @Slot(str)
    def on_failed(self, msg: str) -> None:
        # Non-modal: FIX 1 makes the worker keep retrying on failure, so a
        # QMessageBox here would pop up once per interval and spam the user.
        self._window.set_refresh_failed(msg)

    @Slot(float)
    def on_interval_changed(self, value: float) -> None:
        """A numeric cadence preset was picked in the Refresh menu."""
        self._interval = value
        self._paused = False
        self._worker.set_interval(value)
        self._update_cadence_status()
        self._persist()

    @Slot()
    def on_auto_refresh_disabled(self) -> None:
        """The "Off (manual)" option was picked in the Refresh menu."""
        self._paused = True
        self._worker.set_paused(True)
        self._update_cadence_status()
        self._persist()

    @Slot()
    def on_refresh_requested(self) -> None:
        """The "Refresh now" menu action was triggered."""
        self._worker.force()

    def _update_cadence_status(self) -> None:
        """Reflect the current cadence/pause state in the status bar right
        away, rather than waiting for the next scheduled refresh."""
        if self._last_refresh_at is not None:
            self._window.set_refresh_ok(
                at=self._last_refresh_at, interval=self._interval, paused=self._paused
            )

    def _persist(self) -> None:
        if self._engine is None:
            return
        from jailbee.db.gui_state import save_gui_state
        from jailbee.db.models import GuiState

        save_gui_state(
            self._engine,  # type: ignore[arg-type]  # Engine at runtime; typed as object to keep app.py PySide-only imports
            GuiState(
                id=1,
                layout=self._window.current_layout(),
                table_header_state=self._window.table_header_state(),
                refresh_interval=self._interval,
                refresh_paused=self._paused,
                card_style=self._window.current_card_style(),
            ),
        )

    def _persist_view_state(self) -> None:
        """Write the Qt dashboard's own view state — columns and folded repos.

        Separate from `_persist`, which owns the Qt widget state in
        `gui_state`. Two writers, two rows: nothing here can clobber the
        TUI's row, and nothing here belongs in a table about window layout.
        """
        if self._engine is None:
            return
        from jailbee.db.view_prefs import FRONTEND_QT, ViewState, save_view_state

        save_view_state(
            self._engine,  # type: ignore[arg-type]  # Engine at runtime; typed as object to keep app.py PySide-only imports
            FRONTEND_QT,
            ViewState(
                columns=self._window.enabled_columns(),
                folded=frozenset(self._window.collapsed_repos()),
            ),
        )

    @Slot(str)
    def on_layout_changed(self, name: str) -> None:
        """The View menu switched layout — persist the new choice."""
        self._persist()

    @Slot(str)
    def on_card_style_changed(self, name: str) -> None:
        """The View menu switched card style — persist the new choice."""
        self._persist()

    @Slot()
    def on_collapsed_changed(self) -> None:
        """A card group was expanded/collapsed — persist the folded set.

        Routed to ``_persist_view_state``, not ``_persist``: the folded set
        lives in ``view_prefs``, not ``gui_state``, so this must never touch
        the widget-layout row.
        """
        self._persist_view_state()

    @Slot()
    def on_columns_changed(self) -> None:
        """The Columns menu toggled a column — persist the enabled set."""
        self._persist_view_state()

    def persist_on_close(self) -> None:
        """Save the full GUI-state snapshot (incl. table column widths/order)
        when the window is closing."""
        self._persist()

    def _ask_loose_ttl(self, name: str, default_after: str) -> str | None:
        """Ask how long ``name`` stays in loose. None means cancelled.

        Mirrors the CLI's questionary prompt: the repo's configured
        ``loose_auto_revert.after`` is pre-selected (and inserted into the
        list when it is not one of the presets), and a typed value is checked
        with the same parser the CLI uses — the action is launched as a
        detached ``Popen`` with no terminal, so an unparseable duration would
        make `jailbee net loose` exit 2 where nobody can see it.
        """
        from jailbee.config import LOOSE_TTL_PRESETS, parse_loose_ttl

        items = list(LOOSE_TTL_PRESETS)
        if default_after not in items:
            items.insert(0, default_after)
        items.append("never")
        current = items.index(default_after)

        while True:
            choice, ok = QInputDialog.getItem(
                self._window,
                "Loose network",
                f"Keep {name} in loose for how long?",
                items,
                current,
                True,  # editable — the user can type e.g. `90m`
            )
            if not ok:
                return None
            value = choice.strip()
            if not value:
                return None
            try:
                parse_loose_ttl(value)
            except ValueError as exc:
                QMessageBox.warning(self._window, "Invalid duration", str(exc))
                continue
            return value

    def _confirm(
        self, verb: str, name: str, group: RepoGroup, container: ContainerInfo | None
    ) -> bool:
        """Confirm a destructive verb before dispatching. True to proceed.

        Every verb in ``_CONFIRM_VERBS`` lands here, so the question text comes
        from :func:`confirm_text` — `git pull` writes to the *host* repo and
        needs to say so. Only `destroy` also gets the destroy guard's detail:
        it is the one verb whose "⚠ … Destroying loses this" is true, and it
        launches with ``--force`` (a detached Popen cannot answer the CLI's own
        prompt), so this dialog is the *only* guard in the GUI — the CLI-side
        one from `_warn_before_destroy` is bypassed here by construction.
        "No" (decline) is the default button.
        """
        from jailbee.config import load_config
        from jailbee.destroy_guard import (
            assess,
            status_is_unknown,
            unknown_status_warning,
        )

        if container is None or group.config_path is None:
            return False

        detail = ""
        if verb == "destroy":
            if status_is_unknown(container):
                # Same sentence the CLI prints (`tui.confirm_destroy_risk`): one
                # container must not be described two ways depending on which
                # front-end asked. A mount-mode container is deliberately not
                # "unknown" — see `status_is_unknown`.
                detail = f"\n\n{unknown_status_warning([container.display_name])}."
            elif container.git_status is not None:
                try:
                    summary = assess(load_config(group.config_path), container)
                except Exception:
                    # An unreadable repo config must not block the GUI — the
                    # guard degrades to "no risk shown", same as an unprobed
                    # container — but the failure should still be discoverable
                    # rather than vanishing silently.
                    log.debug(
                        "destroy guard: could not assess %s", group.config_path, exc_info=True
                    )
                    summary = None
                if summary is not None:
                    detail = f"\n\n⚠  {summary.line}\nDestroying loses this."

        question = confirm_text(verb, name, container.base_branch)
        reply = QMessageBox.question(
            self._window,
            "Confirm",
            f"{question}{detail}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _collect_answers(
        self, verb: str, name: str, group: RepoGroup, container: ContainerInfo | None
    ) -> list[str] | None:
        """The flags answering what the CLI would have prompted for.

        Returns the (possibly empty) flag list, or None when the user cancelled
        a dialog — the caller must then dispatch nothing. Asking happens *only*
        where the CLI would ask: a repo that pinned its `push:` defaults has
        already answered, and a flag on top of that would override its policy.
        """
        if verb in ("git push", "git push --pr"):
            pr_refresh = verb == "git push --pr"
            ask_action, ask_source = push_questions(
                group.push_action_default, group.push_source_default
            )
            if pr_refresh:
                # `--pr` *is* the source: the CLI pushes `refs/jailbee/pr/<N>/head`
                # and rejects --from/--current alongside it, so the answer this
                # dialog could give would be a usage error, not a choice.
                ask_source = False
            if not (ask_action or ask_source):
                return []
            push_dlg = PushOptionsDialog(
                name,
                ask_action=ask_action,
                ask_source=ask_source,
                base_branch=container.base_branch if container else None,
                title=(
                    pr_refresh_title(name, container.pr_number if container else None)
                    if pr_refresh
                    else None
                ),
                parent=self._window,
            )
            if push_dlg.exec() != QDialog.DialogCode.Accepted:
                return None
            return push_flags(push_dlg.answers())
        if verb == "pr":
            pr_dlg = PrOptionsDialog(name, parent=self._window)
            if pr_dlg.exec() != QDialog.DialogCode.Accepted:
                return None
            return pr_flags(pr_dlg.answers())
        if verb == "net loose":
            # A None `loose_ttl_default` means the repo's auto-revert policy is
            # disabled: there is no TTL to schedule, so skip the dialog and let
            # `jailbee net loose` run flagless — the same choice the CLI prompt
            # makes.
            if group.loose_ttl_default is None:
                return []
            duration = self._ask_loose_ttl(name, group.loose_ttl_default)
            if duration is None:
                return None
            return ["--for", duration]
        return []

    @Slot(str, str)
    def on_action(self, verb: str, name: str) -> None:
        group = _group_for(self._latest, name)
        if group is None or group.config_path is None:
            return
        container = next((c for c in group.containers if c.name == name), None)
        extra = self._collect_answers(verb, name, group, container)
        if extra is None:
            return  # a dialog was cancelled
        action = build_action(verb, name, group.config_path, extra_flags=extra)
        if action.confirm and not self._confirm(verb, name, group, container):
            return
        if action.launch == "output":
            self._open_output(action.argv, f"jailbee {verb} {name}")
            return
        terminal = detect_terminal(env=_env(), which=shutil.which)
        try:
            argv = resolve_launch(action, terminal)
        except TerminalNotFoundError as exc:
            QMessageBox.warning(self._window, "No terminal", str(exc))
            return
        try:
            subprocess.Popen(argv, start_new_session=True)
        except OSError as exc:
            QMessageBox.warning(self._window, "Launch failed", str(exc))
            return
        self._worker.force()  # an action likely changed state — refresh ASAP

    def _open_output(self, argv: list[str], title: str) -> None:
        """Show a command's output in its own window.

        Non-modal and parented to the main window: the dashboard keeps
        refreshing behind it, and several commands can be watched at once.
        """
        dialog = CommandOutputDialog(argv, title, parent=self._window)
        # Never connect a worker method straight to a signal — the refresh
        # worker has no Qt event loop of its own. Route through this slot, the
        # same way on_action does.
        dialog.view.finished.connect(self._on_output_finished)
        dialog.show()

    @Slot(int)
    def _on_output_finished(self, _code: int) -> None:
        self._worker.force()  # the command likely changed state


def _wire(window: MainWindow, worker: RefreshWorker, controller: AppController) -> None:
    """Connect window/worker signals to the controller.

    All worker *control* (force/set_interval/set_paused) is routed through
    an ``AppController`` slot that calls the worker method directly, the
    same pattern as ``on_action``'s ``self._worker.force()`` call — never
    connected straight from a window signal to a worker bound method.
    ``RefreshWorker.run_loop`` is a blocking loop, not a Qt event loop, so a
    signal connected directly to a worker method resolves to a *queued*
    connection the worker thread never processes; it would silently never
    fire. The worker -> controller connections below are the mirror image
    and are correct as direct signal/slot connections: they cross from the
    worker thread to a controller living on the GUI thread, which *does*
    run a real event loop, so Qt correctly delivers them as queued.
    """
    worker.groupsReady.connect(controller.on_groups)
    worker.failed.connect(controller.on_failed)
    window.actionRequested.connect(controller.on_action)
    window.refreshRequested.connect(controller.on_refresh_requested)
    window.intervalChanged.connect(controller.on_interval_changed)
    window.autoRefreshDisabled.connect(controller.on_auto_refresh_disabled)
    window.layoutChanged.connect(controller.on_layout_changed)
    window.cardStyleChanged.connect(controller.on_card_style_changed)
    window.card_view.collapsedChanged.connect(controller.on_collapsed_changed)
    window.columnsChanged.connect(controller.on_columns_changed)


def run(
    incus: Incus,
    cwd_config: Path | None,
    *,
    interval: float | None,
    git_interval: float,
    no_git: bool,
) -> int:
    """Launch the Qt dashboard. Returns the process exit code."""
    if preflight(cwd_config) is None:
        error("No repos registered and no .jailbee/config.yaml in the current directory.")
        return 1

    from jailbee.db import get_engine
    from jailbee.db.gui_state import load_gui_state

    engine = get_engine()

    # Resolved once for the whole run — a live-refreshing dashboard must not
    # re-merge config on every refresh tick.
    view_state = seed_view_state(engine, FRONTEND_QT)

    state = load_gui_state(engine)

    resolved = interval if interval is not None else state.refresh_interval
    resolved = max(0.5, resolved if resolved is not None else 3.0)
    git_interval = max(git_interval, resolved)
    paused = state.refresh_paused
    git_enabled = not no_git

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        git_enabled=git_enabled,
        interval=resolved,
        paused=paused,
        layout=state.layout,
        header_state=state.table_header_state,
        card_style=state.card_style,
        enabled_columns=view_state.columns,
    )
    window.card_view.set_collapsed(set(view_state.folded))

    thread = QThread()
    worker = RefreshWorker(
        incus,
        cwd_config,
        interval=resolved,
        git_interval=git_interval,
        git_enabled=git_enabled,
    )
    worker.moveToThread(thread)
    thread.started.connect(worker.run_loop)

    # Kept on the GUI thread (never moveToThread'd) so cross-thread worker
    # signals resolve to queued connections; held in a local so it isn't
    # garbage-collected while `app.exec()` runs.
    controller = AppController(window, worker, interval=resolved, engine=engine, paused=paused)
    _wire(window, worker, controller)
    if paused:
        worker.set_paused(True)

    thread.start()
    window.show()
    try:
        return int(app.exec())
    finally:
        controller.persist_on_close()
        worker.request_stop()
        worker.force()
        thread.quit()
        thread.wait(2000)
