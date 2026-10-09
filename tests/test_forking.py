from unittest.mock import MagicMock

import pytest

from jailbee import forking
from jailbee.sync import FetchResult, SyncError


@pytest.fixture
def cfg(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    return make_cfg(repo)


@pytest.fixture
def wired(mocker):
    mocker.patch("jailbee.sync.assert_container_publishable", return_value="myrepo-src")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/myrepo")
    dirty = mocker.patch("jailbee.sync._container_status_dirty", return_value=False)
    fetch = mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=FetchResult(
            branch="feat/a", old_oid=None, new_oid="abc123", base_oid=None, commits_added=0
        ),
    )
    return dirty, fetch


def test_prepare_fork_returns_source_state(cfg, wired):
    incus = MagicMock()
    incus.config_get.return_value = "main"
    fs = forking.prepare_fork(cfg, incus, "src")
    assert fs == forking.ForkSource("myrepo-src", "feat/a", "abc123", "main")
    incus.config_get.assert_called_with("myrepo-src", "user.jailbee.base_branch")


def test_prepare_fork_unset_base_branch_is_none(cfg, wired):
    incus = MagicMock()
    incus.config_get.return_value = None
    assert forking.prepare_fork(cfg, incus, "src").base_branch is None


def test_prepare_fork_refuses_dirty_source_before_fetching(cfg, wired):
    dirty, fetch = wired
    dirty.return_value = True
    with pytest.raises(forking.ForkError, match="uncommitted changes in 'src'"):
        forking.prepare_fork(cfg, MagicMock(), "src")
    fetch.assert_not_called()


def test_prepare_fork_dirty_check_uses_container_uid(cfg, wired):
    dirty, _ = wired
    forking.prepare_fork(cfg, MagicMock(), "src")
    assert dirty.call_args.kwargs["uid"] == cfg.container_user.uid


def test_prepare_fork_turns_sync_errors_into_fork_errors(cfg, wired, mocker):
    mocker.patch(
        "jailbee.sync.assert_container_publishable",
        side_effect=SyncError("Container 'src' is not running. Start it with: jailbee start src"),
    )
    with pytest.raises(forking.ForkError, match="not running"):
        forking.prepare_fork(cfg, MagicMock(), "src")


def test_prepare_fork_detached_head_is_a_fork_error(cfg, wired):
    _, fetch = wired
    fetch.side_effect = SyncError("Cannot determine branch for container 'src'.")
    with pytest.raises(forking.ForkError, match="Cannot determine branch"):
        forking.prepare_fork(cfg, MagicMock(), "src")
