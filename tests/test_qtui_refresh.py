"""StateBridge turns StateClient callbacks (any thread) into Qt signals."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from jailbee.dashboard import RepoGroup
from jailbee.qtui.refresh import StateBridge
from jailbee.state_service.protocol import Snapshot

SNAP = Snapshot(1, datetime(2026, 10, 4, tzinfo=UTC), True, [RepoGroup("p", "/r", None, [])])


class Client:
    def __init__(self, latest=SNAP, status=None):
        self._latest, self._status = latest, status

    def latest(self):
        return self._latest

    def status(self):
        return self._status


def test_a_snapshot_published_from_another_thread_arrives_as_a_signal(qtbot):
    bridge = StateBridge()
    bridge.attach(Client())
    with qtbot.waitSignal(bridge.snapshotReady, timeout=2000) as blocker:
        threading.Thread(target=bridge.publish).start()
    assert blocker.args == [SNAP]


def test_a_status_is_published_as_failed(qtbot):
    bridge = StateBridge()
    bridge.attach(Client(status="state service disconnected — reconnecting"))
    with qtbot.waitSignal(bridge.failed, timeout=2000) as blocker:
        bridge.publish()
    assert blocker.args == ["state service disconnected — reconnecting"]


def test_nothing_is_published_before_a_client_is_attached(qtbot):
    bridge = StateBridge()
    with qtbot.assertNotEmitted(bridge.snapshotReady, wait=200):
        bridge.publish()


def test_a_status_withholds_the_stale_snapshot(qtbot):
    """While the service is unhealthy the last snapshot is old news: only
    the failure is published, so the window does not stamp it as fresh."""
    bridge = StateBridge()
    bridge.attach(Client(status="state service disconnected — reconnecting"))
    with qtbot.assertNotEmitted(bridge.snapshotReady, wait=200):
        bridge.publish()
