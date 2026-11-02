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


def test_request_unreadable_config_keeps_own_branch_available(mocker, tmp_path):
    from jailbee.qtui.pr_sources import _Request
    from jailbee.config import ConfigError

    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigError("unavailable"))
    request = _Request(RepoTarget(tmp_path, None), "short")
    request.run()
    assert request.pending == []
