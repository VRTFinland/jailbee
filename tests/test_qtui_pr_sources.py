from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from jailbee.dashboard.model import RepoTarget


@pytest.mark.parametrize("configured", [True, False])
def test_request_loads_target_config_and_resolves_full_name(mocker, tmp_path, configured):
    from jailbee.qtui.pr_sources import _Request

    path = tmp_path / "config.yaml" if configured else None
    target = RepoTarget(tmp_path, path)
    cfg = MagicMock()
    load = mocker.patch("jailbee.config.load_config", return_value=cfg)
    repo_load = mocker.patch("jailbee.config.load_repo_config", return_value=cfg)
    incus = mocker.patch("jailbee.incus.Incus").return_value
    resolve = mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="full")
    pending = mocker.patch("jailbee.branch_pr.pending_source_manifests", return_value=[])
    request = _Request(target, "short")
    request.run()
    if configured:
        load.assert_called_once_with(path)
        repo_load.assert_not_called()
    else:
        repo_load.assert_called_once_with(tmp_path)
        load.assert_not_called()
    resolve.assert_called_once_with(cfg, incus, "short")
    pending.assert_called_once_with(cfg, incus, "full")
    assert request.pending == []


@pytest.mark.parametrize("dismiss", ["escape", "close"])
def test_source_loading_keeps_processing_events_when_dismissed(mocker, tmp_path, qtbot, dismiss):
    import threading

    from PySide6.QtCore import Qt, QThread, QTimer
    from PySide6.QtWidgets import QApplication

    from jailbee.qtui import pr_sources

    release = threading.Event()

    class SlowRequest(QThread):
        def __init__(self):
            super().__init__()
            self.pending = []

        def run(self):
            release.wait(timeout=1)

    worker = SlowRequest()
    mocker.patch.object(pr_sources, "_Request", return_value=worker)
    ticks = []
    timer = QTimer()

    def tick():
        ticks.append(1)
        if len(ticks) >= 8:
            release.set()

    def dismiss_window():
        window = QApplication.activeModalWidget()
        assert window is not None
        if dismiss == "escape":
            qtbot.keyClick(window, Qt.Key.Key_Escape)
        else:
            window.close()

    timer.timeout.connect(tick)
    timer.start(10)
    QTimer.singleShot(20, dismiss_window)
    try:
        assert pr_sources.pending_pr_sources(RepoTarget(tmp_path, None), "short", None) == []
    finally:
        release.set()
        worker.wait()
        timer.stop()
    assert len(ticks) >= 8


def test_request_unreadable_config_keeps_own_branch_available(mocker, tmp_path):
    from jailbee.config import ConfigError
    from jailbee.qtui.pr_sources import _Request

    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigError("unavailable"))
    request = _Request(RepoTarget(tmp_path, None), "short")
    request.run()
    assert request.pending == []
