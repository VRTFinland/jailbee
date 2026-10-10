"""QApplication bootstrap and wiring for the Qt dashboard.

Container state comes from the shared state service, like the TUI's: the
GUI opens no Incus paths of its own — it renders the service's snapshots
through the dashboard data layer and executes actions as ``jailbee``
subprocesses.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Slot
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox

from jailbee.dashboard.columns import seed_view_state
from jailbee.dashboard.menus import (
    config_edit_reject_note_for_prefix,
    host_branches,
    new_container_argv,
    new_container_base_default,
    new_container_reject_note_for_prefix,
    new_pr_container_argv,
)
from jailbee.dashboard.model import (
    NOTHING_TO_SHOW,
    STARTUP_TIMEOUT_SECONDS,
    RepoTarget,
    collect_repo_roots,
    dashboard_config_migration_notice,
    dashboard_group_notices,
    present,
)
from jailbee.dashboard.sorting import SortSpec
from jailbee.db.view_prefs import FRONTEND_QT
from jailbee.lifecycle import tracking_notices
from jailbee.qtui.actions import (
    ActionCommand,
    TerminalNotFoundError,
    build_action,
    build_bulk_action,
    build_outbox_publish,
    resolve_launch,
)
from jailbee.qtui.outbox import OutboxDialog
from jailbee.qtui.output import CommandOutputDialog
from jailbee.qtui.prompts import (
    NewContainerDialog,
    PrOptionsDialog,
    PushOptionsDialog,
    RetargetDialog,
    confirm_text,
    pr_flags,
    pr_refresh_title,
    push_flags,
    push_questions,
)
from jailbee.qtui.refresh import StateBridge
from jailbee.qtui.terminal import detect_terminal
from jailbee.qtui.window import MainWindow
from jailbee.remote_ssh.session import is_ssh_session
from jailbee.state_service import StateServiceUnavailable
from jailbee.state_service.client import StateClient
from jailbee.tui import error

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoGroup
    from jailbee.lifecycle import ContainerInfo
    from jailbee.state_service.protocol import Snapshot

log = logging.getLogger(__name__)


def preflight(cwd_root: Path | None) -> list[Path] | None:
    """Resolve the repo roots the dashboard would show, or None if there are none.

    A launch-time guard only ("nothing to show, don't open a window") — the
    returned list is deliberately not handed on: the state service re-resolves
    it per gather, so a repo registered while the window is open stops
    rendering as a menu-less orphan.
    """
    repo_roots = collect_repo_roots(cwd_root)
    return repo_roots or None


def _group_for(groups: list[RepoGroup], container_name: str) -> RepoGroup | None:
    for g in groups:
        for c in g.containers:
            if c.name == container_name:
                return g
    return None


def _env() -> dict[str, str]:
    return dict(os.environ)


class AppController(QObject):
    """Routes bridge/window signals to GUI-thread slots.

    Constructed with default (main-thread) affinity and never moved to
    another thread. The ``StateBridge`` emits from the ``StateClient``'s
    reader thread; because these handlers are ``@Slot``-decorated bound
    methods of a ``QObject`` living on the GUI thread, Qt delivers those
    emissions as *queued* calls — so the handlers always run on the GUI
    thread, never on the reader thread.

    ``bridge_client`` is the state service connection: ``None`` only in
    tests that exercise the controller alone.
    """

    def __init__(
        self,
        window: MainWindow,
        bridge_client: StateClient | None = None,
        *,
        cwd_root: Path | None = None,
        engine: object | None = None,
    ) -> None:
        super().__init__()
        self._window = window
        self._client = bridge_client
        self._cwd_root = cwd_root
        self._engine = engine
        # The latest snapshot's setting, shown as `(no-git)` in the status bar.
        self._git_enabled = True
        # What the client was last told; a new window starts active.
        self._active = True
        # Latest snapshot, kept for resolving a clicked action's config path.
        self._latest: list[RepoGroup] = []
        self._outboxes: dict[tuple[RepoTarget, str], OutboxDialog] = {}

    @Slot(object)
    def on_snapshot(self, snapshot: Snapshot) -> None:
        """A snapshot from the state service, shown as this window's own view."""
        self._git_enabled = snapshot.git_enabled
        self.on_groups(present(snapshot.groups, self._cwd_root))

    @Slot(bool)
    def on_window_active(self, active: bool) -> None:
        """The window was minimised (inactive) or restored (active).

        A restored window also asks for a refresh: what it shows was not
        kept current while nobody could see it. Repeats are dropped — the
        window reports every state change (maximise, fullscreen) as active.
        """
        if self._client is None or active == self._active:
            return
        self._active = active
        self._client.set_active(active)
        if active:
            self._client.refresh()

    @Slot(object)
    def on_groups(self, groups: list[RepoGroup]) -> None:
        self._latest = groups
        now = datetime.now().astimezone()
        self._window.set_groups(groups, now=now)
        self._window.set_refresh_ok(at=now, git_enabled=self._git_enabled)
        notices = tracking_notices([c for group in groups for c in group.containers])
        notices.extend(dashboard_group_notices(groups))
        if notices:
            self._window.set_status("; ".join(notices))

    @Slot(str)
    def on_failed(self, msg: str) -> None:
        # Non-modal: the state service keeps gathering and the client keeps
        # reconnecting, so a QMessageBox here would pop up once per failure
        # and spam the user.
        self._window.set_refresh_failed(msg)

    @Slot()
    def on_refresh_requested(self) -> None:
        """The "Refresh now" menu action was triggered."""
        self._request_refresh()

    def _request_refresh(self) -> None:
        """Ask the state service for a gather now (a no-op without a client)."""
        if self._client is not None:
            self._client.refresh()

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
                card_style=self._window.current_card_style(),
            ),
        )

    def _persist_view_state(self) -> None:
        """Write the Qt dashboard's own view state — columns, folds and visibility.

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
                show_empty_repos=self._show_empty_repos(),
                hidden_repos=self._hidden_repos(),
                sort_field=self._window.sort_spec().field,
                sort_desc=self._window.sort_spec().desc,
            ),
        )

    @Slot()
    def on_sort_changed(self) -> None:
        """A header click changed the row sort — the window re-sorted itself; persist it."""
        try:
            self._persist_view_state()
        except Exception as exc:
            log.warning("could not save Qt row sort preferences: %s", exc)
            self._window.set_status(f"Could not save row sort: {exc}")

    @Slot()
    def on_repo_visibility_changed(self) -> None:
        """Persist menu visibility choices without disrupting the live view."""
        try:
            self._persist_view_state()
        except Exception as exc:
            log.warning("could not save Qt repository visibility preferences: %s", exc)
            self._window.set_status(f"Could not save repository visibility: {exc}")

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
        """The Columns menu toggled a column — repaint immediately, then persist.

        Without the repaint, the change only reaches the table on whatever
        the *next* snapshot the state service happens to push — seconds away
        at best, making the menu look inert. `set_groups(None)`'s "columns"
        default already reads the window's own live `enabled_columns()`, so
        re-pushing the latest snapshot here picks up the toggle without
        re-gathering anything.
        """
        if self._latest:
            self._window.set_groups(self._latest, now=datetime.now().astimezone())
        self._persist_view_state()

    def persist_on_close(self) -> None:
        """Save the full GUI-state snapshot (incl. table column widths/order)
        when the window is closing."""
        self._persist()

    def _ask_ttl(self, title: str, question: str, default_after: str) -> str | None:
        """Ask for a TTL. None means cancelled.

        Mirrors the CLI's questionary prompt: the configured default is
        pre-selected (and inserted into the list when it is not one of the
        presets), and a typed value is checked with the same parser the CLI
        uses — the action is launched as a detached ``Popen`` with no terminal,
        so an unparseable duration would be invisible.
        """
        from jailbee.config import TTL_PRESETS, parse_ttl

        items = list(TTL_PRESETS)
        if default_after not in items:
            items.insert(0, default_after)
        items.append("never")
        current = items.index(default_after)

        while True:
            choice, ok = QInputDialog.getItem(
                self._window,
                title,
                question,
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
                parse_ttl(value)
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
        from jailbee.config import load_repo_config
        from jailbee.destroy_guard import (
            assess,
            status_is_unknown,
            unknown_status_warning,
        )

        target = RepoTarget.of(group)
        if container is None or target is None:
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
                    # From the repo root, not a config path: a repo with no
                    # config file still has a config — the synthesized one the
                    # dashboard is already displaying — and the guard has to
                    # see the same one.
                    summary = assess(load_repo_config(target.repo_root), container)
                except Exception:
                    # An unreadable repo config must not block the GUI — the
                    # guard degrades to "no risk shown", same as an unprobed
                    # container — but the failure should still be discoverable
                    # rather than vanishing silently.
                    log.debug("destroy guard: could not assess %s", target.repo_root, exc_info=True)
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
        if verb == "git retarget":
            current = container.base_branch if container else None
            retarget_dlg = RetargetDialog(
                name,
                current_base=current,
                branches=host_branches(group.repo_root, exclude=current),
                parent=self._window,
            )
            if retarget_dlg.exec() != QDialog.DialogCode.Accepted:
                return None
            return ["--", retarget_dlg.answer()]
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
            duration = self._ask_ttl(
                "Loose network", f"Keep {name} in loose for how long?", group.loose_ttl_default
            )
            if duration is None:
                return None
            return ["--for", duration]
        return []

    @Slot(str, str)
    def on_action(self, verb: str, name: str) -> None:
        group = _group_for(self._latest, name)
        if group is None:
            return
        if not self._is_group_visible(group):
            return
        target = RepoTarget.of(group)
        if target is None:
            return  # an orphan group: no repo root to address a child at
        if verb == "outbox browse" and not is_ssh_session():
            self._open_outbox(target, name)
            return
        container = next((c for c in group.containers if c.name == name), None)
        extra = self._collect_answers(verb, name, group, container)
        if extra is None:
            return  # a dialog was cancelled
        action = build_action(verb, name, target, extra_flags=extra)
        if action.confirm and not self._confirm(verb, name, group, container):
            return
        if action.launch == "output":
            self._open_output(action.argv, f"jailbee {verb} {name}", action.cwd)
            return
        if self._spawn(action):
            self._request_refresh()  # an action likely changed state — refresh ASAP

    def _spawn(self, action: ActionCommand) -> bool:
        """Start a "terminal" or "detached" action; False after explaining a failure."""
        failure = self._launch(action)
        if failure is not None:
            QMessageBox.warning(self._window, *failure)
            return False
        return True

    def _launch(self, action: ActionCommand) -> tuple[str, str] | None:
        """Start ``action``; ``(dialog title, cause)`` on failure, None on success."""
        terminal = detect_terminal(env=_env(), which=shutil.which)
        try:
            argv = resolve_launch(action, terminal)
        except TerminalNotFoundError as exc:
            return "No terminal", str(exc)
        try:
            subprocess.Popen(argv, start_new_session=True, cwd=action.cwd)
        except OSError as exc:
            return "Launch failed", str(exc)
        return None

    @Slot(str, list)
    def on_bulk_action(self, verb: str, names: list[str]) -> None:
        """Run ``verb`` over the selected rows (see `jailbee.dashboard.bulk`)."""
        from jailbee.dashboard.bulk import (
            bulk_loose_default,
            foreground_runs,
            nothing_to_do,
            plan_bulk,
        )

        groups = [g for g in self._latest if self._is_group_visible(g)]
        action = plan_bulk(groups, names, verb)
        if not action.eligible:
            QMessageBox.information(self._window, "Nothing to do", nothing_to_do(action))
            return
        skipped = "\n".join(f"{name}: {reason}" for name, reason in action.skipped)
        if action.mode == "parallel":
            extra: list[str] = []
            if verb == "net loose":
                default = bulk_loose_default(groups, action.eligible)
                if default is not None:
                    duration = self._ask_ttl(
                        "Loose network",
                        f"Keep {len(action.eligible)} containers in loose for how long?",
                        default,
                    )
                    if duration is None:
                        return
                    extra = ["--for", duration]
            if verb == "destroy" and not self._confirm_bulk_destroy(groups, action.eligible):
                return
            launched = 0
            problems: list[str] = []
            for name in action.eligible:
                group = _group_for(groups, name)
                target = RepoTarget.of(group) if group is not None else None
                if target is None:
                    problems.append(f"{name}: no repo to address")
                    continue
                failure = self._launch(build_action(verb, name, target, extra_flags=extra))
                if failure is None:
                    launched += 1
                else:
                    problems.append(f"{name}: {failure[1]}")
            if launched:
                self._request_refresh()
            if problems or skipped:
                lines = [f"Could not start: {len(problems)} of {len(action.eligible)}"] * bool(
                    problems
                )
                lines += problems
                if skipped:
                    lines += ["Skipped:", skipped]
                QMessageBox.warning(self._window, "Bulk action", "\n".join(lines))
            return
        if verb == "git pull" and not self._confirm_bulk_pull(action.eligible):
            return
        for run in foreground_runs(groups, action):
            flags: list[str] = []
            if verb == "git push":
                group = next(g for g in groups if g.prefix == run.prefix)
                answers = self._collect_answers(verb, ", ".join(run.names), group, None)
                if answers is None:
                    return  # a dialog was cancelled: nothing after it runs either
                flags = answers
            command = build_bulk_action(verb, run.names, run.target, extra_flags=flags)
            if command.launch == "output":
                self._open_output(
                    command.argv, f"jailbee {verb} {' '.join(run.names)}", command.cwd
                )
            else:
                self._spawn(command)
        if skipped:
            QMessageBox.information(self._window, "Skipped", skipped)

    def _confirm_bulk_destroy(self, groups: list[RepoGroup], names: tuple[str, ...]) -> bool:
        """One question for every container; the risk summary is the only guard (``--force``)."""
        from jailbee.dashboard import bulk

        lines = bulk.destroy_risk_lines(groups, names)
        detail = ("\n\n" + "\n".join(lines) + "\nDestroying loses this.") if lines else ""
        reply = QMessageBox.question(
            self._window,
            "Confirm",
            f"Destroy {len(names)} containers: {', '.join(names)}?{detail}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _confirm_bulk_pull(self, names: tuple[str, ...]) -> bool:
        reply = QMessageBox.question(
            self._window,
            "Confirm",
            confirm_text("git pull", ", ".join(names), None),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    @Slot(str)
    def on_new_container(self, prefix: str) -> None:
        """Collect a branch and a base, then launch `jailbee new` in a terminal.

        A terminal, not a detached `Popen`: `jailbee new` asks about reusing an
        existing branch and about the branch-autostart escalation, and it asks
        in the foreground parent even under `--background`
        (`lifecycle._autostart_approved`). The alternative is `--yes`, which
        accepts a network-widening branch config unseen — so this is the one
        place the GUI deliberately spends a terminal window on a non-attach
        verb.
        """
        note = new_container_reject_note_for_prefix(self._latest, prefix)
        group = next((g for g in self._latest if g.prefix == prefix), None)
        if group is not None and not self._is_group_visible(group):
            return
        if note is not None:
            # Same wording the TUI uses for the same state
            # (`jailbee.dashboard.menus.new_container_reject_note`) — an orphan group's real prefix
            # must not be reported as "no repo selected", which used to be
            # this dialog's one hardcoded message regardless of cause.
            QMessageBox.warning(self._window, "No repo selected", note)
            return
        # `note` being None guarantees a matching group with a repo root;
        # mypy --strict doesn't narrow that through the helper call, so this
        # spells it out again where the type checker can see it.
        assert group is not None
        target = RepoTarget.of(group)
        assert target is not None  # ditto — the note rejects a rootless group
        dialog = NewContainerDialog(
            group.prefix,
            base_default=new_container_base_default(group.repo_root),
            branches=host_branches(group.repo_root),
            parent=self._window,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        answers = dialog.answers()
        action = ActionCommand(
            argv=new_container_argv(target, answers.branch, answers.base),
            launch="terminal",
            confirm=False,
            cwd=target.cwd(),
        )
        try:
            argv = resolve_launch(action, detect_terminal(env=_env(), which=shutil.which))
        except TerminalNotFoundError as exc:
            QMessageBox.warning(self._window, "No terminal", str(exc))
            return
        try:
            subprocess.Popen(argv, start_new_session=True, cwd=action.cwd)
        except OSError as exc:
            QMessageBox.warning(self._window, "Launch failed", str(exc))
            return
        self._request_refresh()

    @Slot(str)
    def on_new_pr_container(self, prefix: str) -> None:
        """Ask for a PR number and open the CLI's review-container flow."""
        note = new_container_reject_note_for_prefix(self._latest, prefix)
        group = next((g for g in self._latest if g.prefix == prefix), None)
        if group is not None and not self._is_group_visible(group):
            return
        if note is not None:
            QMessageBox.warning(self._window, "No repo selected", note)
            return
        assert group is not None
        target = RepoTarget.of(group)
        assert target is not None
        number, accepted = QInputDialog.getInt(
            self._window, f"New review container in '{prefix}'", "PR number", minValue=1
        )
        if not accepted:
            return
        action = ActionCommand(
            argv=new_pr_container_argv(target, number),
            launch="terminal",
            confirm=False,
            cwd=target.cwd(),
        )
        try:
            argv = resolve_launch(action, detect_terminal(env=_env(), which=shutil.which))
        except TerminalNotFoundError as exc:
            QMessageBox.warning(self._window, "No terminal", str(exc))
            return
        try:
            subprocess.Popen(argv, start_new_session=True, cwd=action.cwd)
        except OSError as exc:
            QMessageBox.warning(self._window, "Launch failed", str(exc))
            return
        self._request_refresh()

    def _is_group_visible(self, group: RepoGroup) -> bool:
        """Gate stale UI actions against the window's current filtered view."""
        return group.prefix not in self._hidden_repos() and (
            bool(group.containers) or self._show_empty_repos()
        )

    def _show_empty_repos(self) -> bool:
        value = self._window.show_empty_repos()
        return value if isinstance(value, bool) else True

    def _hidden_repos(self) -> frozenset[str]:
        value = self._window.hidden_repos()
        return frozenset(value) if isinstance(value, (set, frozenset)) else frozenset()

    @Slot(str, bool)
    def on_config_edit(self, prefix: str, global_layer: bool) -> None:
        """Open `jailbee config edit` for `prefix` in a real terminal window.

        A terminal, not a detached `Popen` or an output window: the editor is a
        full-screen TUI and needs a TTY, exactly like `shell` and `tmux`. The
        GUI grows no form of its own (spec 5.2/9) — the machinery to hand a
        terminal to a jailbee command already exists, so the whole Qt entry
        point is this method.
        """
        note = config_edit_reject_note_for_prefix(self._latest, prefix, global_layer=global_layer)
        if note is not None:
            QMessageBox.warning(self._window, "No repo selected", note)
            return
        group = next((g for g in self._latest if g.prefix == prefix), None)
        # `note` being None guarantees a matching group with a repo root; mypy
        # --strict does not narrow that through the helper call.
        assert group is not None
        target = RepoTarget.of(group)
        assert target is not None
        argv = ["jailbee", "config", "edit", *target.flags()]
        if global_layer:
            argv.append("--global")
        action = ActionCommand(argv=argv, launch="terminal", confirm=False, cwd=target.cwd())
        try:
            launch_argv = resolve_launch(action, detect_terminal(env=_env(), which=shutil.which))
        except TerminalNotFoundError as exc:
            QMessageBox.warning(self._window, "No terminal", str(exc))
            return
        try:
            subprocess.Popen(launch_argv, start_new_session=True, cwd=action.cwd)
        except OSError as exc:
            QMessageBox.warning(self._window, "Launch failed", str(exc))
            return
        self._request_refresh()  # the config may have changed under every card

    def _open_outbox(self, target: RepoTarget, container: str) -> None:
        if is_ssh_session():
            self.on_action("outbox browse", container)
            return
        key = (target, container)
        dialog = self._outboxes.get(key)
        if dialog is not None:
            if not dialog.closing:
                dialog.show()
                dialog.raise_()
                dialog.activateWindow()
            return  # A closing worker still owns this slot until completion.
        dialog = OutboxDialog(target, container, parent=self._window)
        self._outboxes[key] = dialog
        dialog.changed.connect(self.on_refresh_requested)
        dialog.publishRequested.connect(self._on_outbox_publish)
        dialog.retired.connect(self._on_outbox_retired)
        dialog.show()

    @Slot(str, str)
    def _on_outbox_publish(self, proposal: str, revision: str) -> None:
        dialog = self.sender()
        for (target, container), candidate in self._outboxes.items():
            if candidate is dialog and not candidate.closing:
                if self._publish_outbox(target, container, proposal, revision):
                    candidate.publication_started()
                return

    def _publish_outbox(
        self, target: RepoTarget, container: str, proposal: str, revision: str
    ) -> bool:
        if is_ssh_session():
            QMessageBox.warning(
                self._window, "Read-only browser", "Use explicit outbox apply over SSH."
            )
            return False
        try:
            action = build_outbox_publish(container, proposal, revision, target)
            argv = resolve_launch(action, detect_terminal(env=_env(), which=shutil.which))
        except (TerminalNotFoundError, ValueError) as exc:
            QMessageBox.warning(self._window, "No terminal or invalid proposal", str(exc))
            return False
        try:
            subprocess.Popen(argv, start_new_session=True, cwd=action.cwd)
        except OSError as exc:
            QMessageBox.warning(self._window, "Launch failed", str(exc))
            return False
        return True  # No success receipt: refresh on reactivation or explicitly.

    @Slot()
    def _on_outbox_retired(self) -> None:
        dialog = self.sender()
        for key, candidate in list(self._outboxes.items()):
            if candidate is dialog:
                del self._outboxes[key]
                candidate.deleteLater()
                return

    def _finish_outboxes(self) -> None:
        for dialog in list(self._outboxes.values()):
            dialog.finish_on_shutdown()

    def _open_output(self, argv: list[str], title: str, cwd: Path) -> None:
        """Show a command's output in its own window.

        Non-modal and parented to the main window: the dashboard keeps
        refreshing behind it, and several commands can be watched at once.

        ``cwd`` reaches the dialog's ``QProcess``, not just the detached
        launches: a repo with no config file is addressed by it alone.
        """
        dialog = CommandOutputDialog(argv, title, cwd, parent=self._window)
        # Routed through a controller slot, the same way on_action asks for
        # a refresh, rather than wiring the client into a dialog signal.
        dialog.view.finished.connect(self._on_output_finished)
        dialog.show()

    @Slot(int)
    def _on_output_finished(self, _code: int) -> None:
        self._request_refresh()  # the command likely changed state


def _wire(window: MainWindow, bridge: StateBridge, controller: AppController) -> None:
    """Connect window/bridge signals to the controller.

    The bridge is a ``QObject`` living on the GUI thread, but it emits from
    the ``StateClient``'s reader thread; Qt resolves those cross-thread
    emissions to *queued* connections, so ``on_snapshot``/``on_failed`` run
    on the GUI thread — nothing in the reader thread touches a widget.
    Requests the other way (refresh, active) are plain method calls on the
    client from controller slots: the client's own lock makes them safe.
    """
    bridge.snapshotReady.connect(controller.on_snapshot)
    bridge.failed.connect(controller.on_failed)
    window.actionRequested.connect(controller.on_action)
    window.bulkActionRequested.connect(controller.on_bulk_action)
    window.newContainerRequested.connect(controller.on_new_container)
    window.newPrContainerRequested.connect(controller.on_new_pr_container)
    window.configEditRequested.connect(controller.on_config_edit)
    window.refreshRequested.connect(controller.on_refresh_requested)
    window.activeChanged.connect(controller.on_window_active)
    window.layoutChanged.connect(controller.on_layout_changed)
    window.cardStyleChanged.connect(controller.on_card_style_changed)
    window.card_view.collapsedChanged.connect(controller.on_collapsed_changed)
    window.columnsChanged.connect(controller.on_columns_changed)
    window.sortChanged.connect(controller.on_sort_changed)
    window.repoVisibilityChanged.connect(controller.on_repo_visibility_changed)


def run(cwd_root: Path | None) -> int:
    """Launch the Qt dashboard. Returns the process exit code."""
    roots = preflight(cwd_root)
    if roots is None:
        error(NOTHING_TO_SHOW)
        return 1

    from jailbee.db import get_engine
    from jailbee.db.gui_state import load_gui_state

    engine = get_engine()
    column_notices: list[str] = []
    view_state = seed_view_state(engine, FRONTEND_QT, on_migration=column_notices.append)
    column_notice = "; ".join(column_notices) if column_notices else None
    config_notice = dashboard_config_migration_notice()
    if config_notice:
        column_notice = "; ".join(filter(None, (column_notice, config_notice)))

    state = load_gui_state(engine)

    app = QApplication.instance() or QApplication([])
    window = MainWindow(
        layout=state.layout,
        header_state=state.table_header_state,
        card_style=state.card_style,
        enabled_columns=view_state.columns,
        show_empty_repos=view_state.show_empty_repos,
        hidden_repos=view_state.hidden_repos,
        sort=SortSpec(view_state.sort_field, view_state.sort_desc),
    )
    window.card_view.set_collapsed(set(view_state.folded))

    # The client needs `bridge.publish` as its `on_update` at construction:
    # bridge first, then the client, then attach. The controller is kept on
    # the GUI thread (never moveToThread'd) so the bridge's cross-thread
    # emissions resolve to queued connections.
    bridge = StateBridge()
    client = StateClient(cwd_root, on_update=bridge.publish)
    bridge.attach(client)
    controller = AppController(window, client, cwd_root=cwd_root, engine=engine)
    _wire(window, bridge, controller)
    client.start()

    # Waited for before the window is shown, so it never appears blank. A
    # failure still shows the window, unlike the TUI: a GUI launched detached
    # has nowhere to print, and the client keeps reconnecting behind it.
    try:
        controller.on_snapshot(client.wait_first_snapshot(STARTUP_TIMEOUT_SECONDS))
    except StateServiceUnavailable as exc:
        controller.on_failed(str(exc))

    window.show()
    if column_notice:
        QMessageBox.warning(window, "Dashboard column migration", column_notice)
    try:
        return int(app.exec())
    finally:
        try:
            controller.persist_on_close()
        finally:
            try:
                controller._finish_outboxes()
            finally:
                client.close()
