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


def _labels(**values):
    incus = MagicMock()
    incus.config_get.side_effect = lambda name, key: values.get(key.removeprefix("user.jailbee."))
    return incus


def test_prepare_fork_returns_source_state(cfg, wired):
    incus = _labels(base_branch="main")
    fs = forking.prepare_fork(cfg, incus, "src")
    assert fs == forking.ForkSource("myrepo-src", "feat/a", "abc123", "main", untrusted=False)
    incus.config_get.assert_any_call("myrepo-src", "user.jailbee.base_branch")


def test_prepare_fork_of_a_pr_container_is_untrusted(cfg, wired):
    # Its commits may be a PR author's: forking must not launder them into a
    # container whose repo autostart runs unasked.
    incus = _labels(base_branch="main", pr="42")
    assert forking.prepare_fork(cfg, incus, "src").untrusted is True
    incus.config_get.assert_any_call("myrepo-src", "user.jailbee.pr")


def test_prepare_fork_without_a_pr_label_is_trusted(cfg, wired):
    assert forking.prepare_fork(cfg, _labels(pr=""), "src").untrusted is False


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
    incus = MagicMock()
    forking.prepare_fork(cfg, incus, "src")
    assert dirty.call_args.kwargs["uid"] == cfg.container_user.uid
    assert dirty.call_args.args == (incus, "myrepo-src", "/home/dev/myrepo")


def test_prepare_fork_fetches_by_short_name(cfg, wired):
    _, fetch = wired
    incus = MagicMock()
    forking.prepare_fork(cfg, incus, "src")
    fetch.assert_called_once_with(cfg, incus, "src")


def test_prepare_fork_unknown_source_is_a_fork_error(cfg, wired, mocker):
    mocker.patch(
        "jailbee.sync.assert_container_publishable",
        side_effect=ValueError("No container named 'nosuch'"),
    )
    with pytest.raises(forking.ForkError, match="nosuch"):
        forking.prepare_fork(cfg, MagicMock(), "nosuch")


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


def _git(repo, *args):
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def real_repo(cfg):
    repo = cfg.repo_root
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "c")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_pin_fork_commit_writes_the_forks_own_ref(cfg, real_repo):
    # The source's `refs/jailbee/<src>/<branch>` dies with the source (or is
    # force-moved by its next fetch); the fork's `--shared` clone must not.
    repo, sha = real_repo
    ref = forking.pin_fork_commit(cfg, "myrepo-b", sha)
    assert ref == "refs/jailbee/b/HEAD"
    assert _git(repo, "rev-parse", "refs/jailbee/b/HEAD") == sha


def test_pin_fork_commit_is_under_the_prefix_destroy_cleans(cfg, real_repo):
    from jailbee import git as git_helpers

    repo, sha = real_repo
    ref = forking.pin_fork_commit(cfg, "myrepo-b", sha)
    # Exactly the prefix `destroy_container` lists and deletes.
    assert git_helpers.list_refs(repo, "refs/jailbee/b/") == [ref]


def test_pin_fork_commit_failure_is_a_git_error(cfg, mocker):
    from jailbee.git import GitError

    mocker.patch("jailbee.git.update_ref", return_value=False)
    with pytest.raises(GitError, match="refs/jailbee/b/HEAD"):
        forking.pin_fork_commit(cfg, "myrepo-b", "abc123")
