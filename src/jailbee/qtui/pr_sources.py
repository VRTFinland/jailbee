"""Load PR proposals off the GUI thread before collecting publication options."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Qt
from PySide6.QtWidgets import QProgressDialog

from jailbee.branch_pr import PendingSource

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from jailbee.dashboard.model import RepoTarget

log = logging.getLogger(__name__)


class _Request(QThread):
    def __init__(self, target: RepoTarget, name: str) -> None:
        super().__init__()
        self.target = target
        self.name = name
        self.pending: list[PendingSource] = []

    def run(self) -> None:
        from jailbee import branch_pr, config, lifecycle
        from jailbee.incus import Incus

        try:
            cfg = (
                config.load_config(self.target.config_path)
                if self.target.config_path is not None
                else config.load_repo_config(self.target.repo_root)
            )
            incus = Incus()
            full = lifecycle.resolve_container_name(cfg, incus, self.name)
            self.pending = branch_pr.pending_source_manifests(cfg, incus, full)
        except Exception:
            # The normal CLI remains responsible for reporting target failures.
            log.debug("Cannot read PR source proposals", exc_info=True)


def pending_pr_sources(target: RepoTarget, name: str, parent: QWidget | None) -> list[PendingSource]:
    request = _Request(target, name)
    progress = QProgressDialog("Reading PR source proposals…", "", 0, 0, parent)
    progress.setCancelButton(None)
    progress.setWindowModality(Qt.WindowModality.ApplicationModal)
    request.finished.connect(progress.accept)
    request.start()
    progress.exec()
    # Closing the progress window must not destroy a running QThread.
    request.wait()
    return request.pending
