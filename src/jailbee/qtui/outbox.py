"""Native, read-only proposal text with shared revision-checked deletion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from jailbee import config as config_api
from jailbee.incus import Incus
from jailbee.issue_manifest import foreign_slug
from jailbee.outbox import commands, service
from jailbee.outbox.delete import DeletePlan, DeleteSelection, plan_delete
from jailbee.outbox.inspect import safe_text
from jailbee.outbox.models import ContainerView, OutboxChanged, OutboxError, ProposalView
from jailbee.outbox_io import JournalStore
from jailbee.remote_ssh.session import is_ssh_session

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoTarget
    from jailbee.dashboard.outbox import SentItem

# Reads the Sent log on demand: ``whole_repo`` False is the container's current
# incarnation, True the repo timeline (every container, destroyed ones marked).
SentSource = Callable[[bool], "tuple[SentItem, ...]"]


@dataclass(frozen=True)
class _Selection:
    proposal: ProposalView
    action: int | None = None
    comment: int | None = None


class _Request(QThread):
    """One controlled request; no widgets or retained Config cross this boundary."""

    def __init__(
        self, target: RepoTarget, container: str, generation: int, plan: DeletePlan | None
    ) -> None:
        super().__init__()
        self.target = target
        self.container = container
        self.generation = generation
        self.plan = plan
        self.view: ContainerView | None = None
        self.error: str | None = None
        self.mutation_attempted = False

    def run(self) -> None:
        try:
            cfg = (
                config_api.load_config(self.target.config_path)
                if self.target.config_path is not None
                else config_api.load_repo_config(self.target.repo_root)
            )
            incus, journals = Incus(), JournalStore()
            own, full = commands.resolve_target(cfg, incus, self.container)
            if self.plan is not None:
                fresh, resolved = commands.resolve_target(cfg, incus, full)
                if fresh != own or resolved != full:
                    raise OutboxChanged("target config changed; refresh required")
                self.mutation_attempted = True
                service.execute_delete(
                    own, incus, full, self.plan, journal_store=journals, lock_timeout=2.0
                )
            self.view = service.load_container(own, incus, full, journal_store=journals)
        except Exception as exc:
            # Always deliver completion, including ambiguous mutation/reload failures.
            self.error = str(exc)


class OutboxDialog(QDialog):
    """Retain in the controller until retired, including after a busy close.

    Controlled subprocesses and lock waits have finite deadlines. Filesystem
    and scheduler stalls are not preemptible; this class never kills a worker.
    """

    changed = Signal()
    publishRequested = Signal(str, str)  # noqa: N815 - Qt signal convention
    retired = Signal()

    def __init__(
        self,
        target: RepoTarget,
        container: str,
        *,
        parent: QWidget | None = None,
        sent_source: SentSource | None = None,
    ) -> None:
        if is_ssh_session():
            raise OutboxError("Use the read-only terminal outbox manager over SSH")
        super().__init__(parent)
        self._target, self._container = target, container
        self._sent_source = sent_source
        self._sent_items: tuple[SentItem, ...] = ()
        self._first_render = True
        self._request: _Request | None = None
        self._generation = 0
        self._pending_load = False
        self._confirming = False
        self._view: ContainerView | None = None
        self._return_refresh = False
        self.closing = False
        self._retired = False
        self.setWindowTitle(f"Outbox - {container}")
        self.resize(1000, 600)
        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Proposal / action / comment"])
        self.tree.currentItemChanged.connect(self._selection_changed)
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.details.setFont(font)
        self.sent_tree = QTreeWidget(self)
        self.sent_tree.setRootIsDecorated(False)
        self.sent_tree.currentItemChanged.connect(self._sent_selection_changed)
        self.sent_tree.hide()
        splitter = QSplitter(self)
        splitter.addWidget(self.tree)
        splitter.addWidget(self.sent_tree)
        splitter.addWidget(self.details)
        self.status = QLabel("Loading", self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.refresh_button = QPushButton("Refresh", self)
        self.delete_button = QPushButton("Delete selected", self)
        self.publish_button = QPushButton("Publish all pending in manifest", self)
        self.sent_button = QPushButton("Sent", self)
        self.sent_button.setCheckable(True)
        self.sent_button.setVisible(sent_source is not None)
        self.repo_check = QCheckBox("Whole repo", self)
        self.repo_check.setVisible(False)
        self.open_button = QPushButton("Open in browser", self)
        self.open_button.setVisible(False)
        self.open_button.setEnabled(False)
        self.sent_button.toggled.connect(self._toggle_sent)
        self.repo_check.toggled.connect(self._reload_sent)
        self.open_button.clicked.connect(self.open_sent_url)
        close = QPushButton("Close", self)
        self.refresh_button.clicked.connect(self.refresh)
        self.delete_button.clicked.connect(self.delete_selected)
        self.publish_button.clicked.connect(self.publish_selected)
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        for button in (self.refresh_button, self.delete_button, self.publish_button):
            buttons.addWidget(button)
        for extra in (self.sent_button, self.repo_check, self.open_button, close):
            buttons.addWidget(extra)
        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self.refresh()

    @property
    def busy(self) -> bool:
        return self._request is not None or self._confirming

    @Slot()
    def refresh(self) -> None:
        if self.closing or self._confirming:
            return
        if self.sent_button.isChecked():
            self._load_sent()
        if self._request is not None and self._request.plan is not None:
            return
        self._generation += 1
        if self._request is not None:
            self._pending_load = True
            return
        self._start(None)

    def _start(self, plan: DeletePlan | None) -> None:
        request = _Request(self._target, self._container, self._generation, plan)
        self._request = request
        request.finished.connect(self._completed)
        self.status.setText("Deleting" if plan else "Loading")
        self._buttons()
        request.start()

    @Slot()
    def _completed(self) -> None:
        request = self._request
        if request is None or request.isRunning():
            return
        request.wait()  # finished can precede thread-local destructor cleanup.
        self._request = None
        if request.mutation_attempted:
            self.changed.emit()  # Never discard mutation completion on close/generation.
        if not self.closing and (
            request.plan is not None or request.generation == self._generation
        ):
            if request.view is not None:
                self._render(request.view)
            if request.error:
                self._render(ContainerView(None, self._container, False, request.error, (), ()))
        request.deleteLater()
        if self.closing:
            self._retire()
        elif self._pending_load:
            self._pending_load = False
            self._start(None)
        else:
            self._buttons()

    def _selected(self) -> _Selection | None:
        item = self.tree.currentItem()
        value = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        return value if isinstance(value, _Selection) else None

    def _render(self, view: ContainerView) -> None:
        previous = self._selected()
        self._view = view
        self.tree.clear()
        if not self.sent_mode:
            self.details.clear()
        status = view.error or (
            "No proposals (empty)" if not view.proposals else "Select a proposal"
        )
        warnings = [text for store in view.stores for text in (*store.warnings, *store.rejected)]
        if not self.sent_mode:
            self.status.setText("\n".join([status, *warnings]))
        restored: QTreeWidgetItem | None = None
        for proposal in view.proposals:
            root = QTreeWidgetItem(self.tree, [f"{proposal.id} [{proposal.state}]"])
            root.setData(0, Qt.ItemDataRole.UserRole, _Selection(proposal))
            if previous and previous.proposal.id == proposal.id:
                restored = root
            for action in proposal.actions:
                child = QTreeWidgetItem(
                    root, [f"Action {action.index}: {action.kind} [{action.state}]"]
                )
                child.setData(0, Qt.ItemDataRole.UserRole, _Selection(proposal, action.index))
                same = (
                    previous
                    and previous.proposal.id == proposal.id
                    and previous.proposal.revision == proposal.revision
                )
                if same and previous and previous.action == action.index:
                    restored = child
                for comment in action.comments:
                    leaf = QTreeWidgetItem(child, [f"Comment {comment.index}: {comment.label}"])
                    leaf.setData(
                        0,
                        Qt.ItemDataRole.UserRole,
                        _Selection(proposal, action.index, comment.index),
                    )
                    if (
                        same
                        and previous
                        and previous.action == action.index
                        and previous.comment == comment.index
                    ):
                        restored = leaf
        self.tree.expandAll()
        if restored is not None:
            self.tree.setCurrentItem(restored)
        self._buttons()
        first, self._first_render = self._first_render, False
        if first and not view.proposals and not self.sent_mode and self._has_sent():
            self.sent_button.setChecked(True)  # only Sent records exist: no dead-end first view

    @Slot()
    def _selection_changed(self) -> None:
        selected = self._selected()
        if self.sent_mode:
            self._buttons()
            return
        if selected is None:
            self._buttons()
            return
        proposal = selected.proposal
        action = next((a for a in proposal.actions if a.index == selected.action), None)
        comment = (
            next((c for c in action.comments if c.index == selected.comment), None)
            if action
            else None
        )
        if comment:
            text = comment.text
        elif action:
            text = action.text + (f"\nReceipt: {action.receipt}" if action.receipt else "")
        else:
            texts = [
                f"{proposal.id} [{proposal.state}]\nRevision: {proposal.revision}",
                proposal.error or "",
                proposal.edit_block or "",
                "Raw manifest:\n" + proposal.raw_text,
            ]
            for entry in proposal.actions:
                mark = " ⚠ foreign repository" if foreign_slug(entry.repo) else ""
                texts.append(
                    f"Action {entry.index}: {entry.kind} {entry.repo}{mark} "
                    f"{entry.target} [{entry.state}]\n{entry.text}"
                )
                if entry.receipt:
                    texts.append("Receipt: " + entry.receipt)
                texts.extend(f"Comment {c.index}: {c.label}\n{c.text}" for c in entry.comments)
            text = "\n\n".join(texts)
        self.details.setPlainText(text)
        if proposal.edit_block:
            self.status.setText(proposal.edit_block)
        self._buttons()

    def _buttons(self) -> None:
        selected = self._selected()
        enabled = not self.busy and not self.closing
        sent = self._selected_sent()
        self.open_button.setEnabled(bool(sent and sent.row.url))
        self.sent_button.setEnabled(enabled)
        self.repo_check.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self.delete_button.setEnabled(
            bool(enabled and not self.sent_mode and selected and not selected.proposal.edit_block)
        )
        self.publish_button.setEnabled(
            bool(
                enabled
                and not self.sent_mode
                and selected
                and not selected.proposal.error
                and selected.proposal.state in ("pending", "partial", "awaiting-pr")
            )
        )
        self.tree.setEnabled(enabled)
        self.sent_tree.setEnabled(not self.closing)

    @property
    def sent_mode(self) -> bool:
        return self.sent_button.isChecked()

    def _has_sent(self) -> bool:
        if self._sent_source is None:
            return False
        try:
            return bool(self._sent_source(False))
        except Exception:
            return False

    @Slot(bool)
    def _toggle_sent(self, on: bool) -> None:
        self.tree.setVisible(not on)
        self.sent_tree.setVisible(on)
        self.repo_check.setVisible(on)
        self.open_button.setVisible(on)
        self.details.clear()
        if on:
            self._load_sent()
        else:
            self._selection_changed()
        self._buttons()

    @Slot()
    def _reload_sent(self) -> None:
        if self.sent_mode:
            self._load_sent()

    def _load_sent(self) -> None:
        """Read the Sent log (on opening and refreshing only, like the terminal box)."""
        if self._sent_source is None:
            return
        previous = self._selected_sent()
        whole = self.repo_check.isChecked()
        try:
            items = self._sent_source(whole)
        except Exception as exc:  # a state-DB failure must not break the dialog
            self.status.setText(f"could not read the Sent log: {exc}")
            items = ()
        self._sent_items = items
        self.sent_tree.clear()
        self.details.clear()
        headers = ["Sent (UTC)", "By", "Action", "Target", "Summary"]
        if whole:
            headers.insert(2, "Container")
        self.sent_tree.setHeaderLabels(headers)
        restored: QTreeWidgetItem | None = None
        for item in items:
            node = QTreeWidgetItem(self.sent_tree, self._sent_columns(item, whole))
            node.setData(0, Qt.ItemDataRole.UserRole, item)
            if item.row.state == "uncertain":
                node.setToolTip(0, "uncertain: GitHub may not have accepted this action")
            if item.gone:
                node.setToolTip(0, item.gone)
            if previous is not None and previous.row.id == item.row.id:
                restored = node
        self.status.setText(f"{len(items)} sent" if items else "Nothing sent yet")
        if restored is not None:
            self.sent_tree.setCurrentItem(restored)
        self._buttons()

    @staticmethod
    def _sent_columns(item: SentItem, whole: bool) -> list[str]:
        from jailbee.outbox import sent_view

        row = item.row
        mark = ("† " if item.gone else "") + ("? " if row.state == "uncertain" else "")
        target = f"{row.repo}#{row.target}" if row.target is not None else row.repo
        cols = [
            mark + row.sent_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M"),
            "orch" if row.publisher == "orchestrator" else "user",
            f"{row.outbox}/{row.kind}",
            safe_text(target),
            sent_view.summary(row) + ("  [uncertain]" if row.state == "uncertain" else ""),
        ]
        if whole:
            cols.insert(2, safe_text(row.container or row.instance))
        return cols

    def _selected_sent(self) -> SentItem | None:
        node = self.sent_tree.currentItem()
        value = node.data(0, Qt.ItemDataRole.UserRole) if node is not None else None
        return value if value is not None and hasattr(value, "row") else None

    @Slot()
    def _sent_selection_changed(self) -> None:
        from jailbee.outbox import sent_view

        item = self._selected_sent()
        if item is None:
            self.details.clear()
        else:
            lines = sent_view.show_lines(item.row)
            if item.gone:
                lines.insert(1, f"Note:       {item.gone}")
            self.details.setPlainText("\n".join(lines))
        self._buttons()

    @Slot()
    def open_sent_url(self) -> None:
        item = self._selected_sent()
        if item is None or not item.row.url:
            return
        QDesktopServices.openUrl(QUrl(item.row.url))

    @Slot()
    def delete_selected(self) -> None:
        selected = self._selected()
        if (
            self.busy
            or self.closing
            or selected is None
            or self._view is None
            or selected.proposal.edit_block
        ):
            return
        self._confirming = True
        self._buttons()
        try:
            selection = DeleteSelection(selected.action, selected.comment)
            try:
                plan = plan_delete(self._view, selected.proposal.id, selection)
            except OutboxError:
                plan = plan_delete(
                    self._view,
                    selected.proposal.id,
                    DeleteSelection(selected.action, selected.comment, with_dependents=True),
                )
            # QMessageBox runs a nested event loop; keep this immutable plan frozen.
            reply = QMessageBox.question(
                self,
                "Delete exact scope",
                "\n".join(plan.summary),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        except OutboxError as exc:
            self.status.setText(str(exc))
            reply = QMessageBox.StandardButton.No
        finally:
            self._confirming = False
        if not self.closing and reply == QMessageBox.StandardButton.Yes:
            self._generation += 1
            self._start(plan)
        elif self.closing:
            self._retire()
        else:
            self._buttons()

    @Slot()
    def publish_selected(self) -> None:
        selected = self._selected()
        if self.busy or self.closing or selected is None or not self.publish_button.isEnabled():
            return
        self.publishRequested.emit(str(selected.proposal.id), selected.proposal.revision)

    def publication_started(self) -> None:
        self._return_refresh = True
        self.status.setText(
            "Terminal owns confirmation for all pending actions in this manifest. "
            "Refresh after return; launch is not publication."
        )

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowActivate and getattr(self, "_return_refresh", False):
            self._return_refresh = False
            self.refresh()
        return super().event(event)

    def reject(self) -> None:
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self.closing = True
        self._pending_load = False
        self.hide()
        event.accept()
        if not self.busy:
            self._retire()

    def _retire(self) -> None:
        if not self._retired:
            self._retired = True
            self.retired.emit()

    def finish_on_shutdown(self) -> None:
        """App exit keeps ownership until controlled I/O completes, never terminate."""
        self.close()
        if self._request is not None:
            self._request.wait()
            self._completed()
