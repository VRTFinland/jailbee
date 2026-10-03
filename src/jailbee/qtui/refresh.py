"""Bridge from the shared state service to the Qt dashboard.

`StateClient` calls `StateBridge.publish` from its reader thread after every
snapshot or status change; the bridge re-emits it as a signal. The bridge
lives on the GUI thread, so Qt delivers those cross-thread emissions to the
controller as queued calls — the same contract `RefreshWorker` had, without
a gather loop of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal

if TYPE_CHECKING:
    from jailbee.state_service.client import StateClient


class StateBridge(QObject):
    snapshotReady = Signal(object)  # noqa: N815 - Qt signal naming convention (camelCase); payload: Snapshot
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._client: StateClient | None = None

    def attach(self, client: StateClient) -> None:
        """Set once, before the client's first callback can matter."""
        self._client = client

    def publish(self) -> None:
        """Emit the client's current state. Safe from any thread."""
        client = self._client
        if client is None:
            return
        status = client.status()
        if status is not None:
            self.failed.emit(status)
            return
        snapshot = client.latest()
        if snapshot is not None:
            self.snapshotReady.emit(snapshot)
