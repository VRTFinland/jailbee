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
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    return dirty, fetch


def _labels(**values):
    incus = MagicMock()
    incus.config_get.side_effect = lambda name, key: values.get(key.removeprefix("user.jailbee."))
    return incus


def test_inherited_authored_labels_become_adopted():
    labels = forking.inherited_pr_labels(_labels(pr="4", pr_branch="x", pr_author="1"), "src")
    assert labels == {
        "user.jailbee.pr": "4",
        "user.jailbee.pr_branch": "x",
        "user.jailbee.pr_adopted": "1",
    }


def test_inherited_review_and_stacked_labels():
    labels = forking.inherited_pr_labels(
        _labels(pr="4", stacked_pr="5", stacked_pr_author="1", stacked_pr_base="main"), "src"
    )
    assert labels == {
        "user.jailbee.pr": "4",
        "user.jailbee.stacked_pr": "5",
        "user.jailbee.stacked_pr_adopted": "1",
        "user.jailbee.stacked_pr_base": "main",
    }


def test_inherited_submodule_labels():
    import json

    labels = forking.inherited_pr_labels(
        _labels(sub_pr='{"lib": {"pr": 4, "branch": "topic", "author": true}}'), "src"
    )
    assert json.loads(labels["user.jailbee.sub_pr"]) == {
        "lib": {"pr": 4, "branch": "topic", "author": False, "adopted": True}
    }
    from jailbee.pr_flow import PrRecord
    from jailbee.submodule_pr import SubmodulePrState

    incus = MagicMock()
    incus.config_get.side_effect = lambda full, key: labels.get(key)
    assert SubmodulePrState(incus, "fork", "lib").read() == PrRecord(4, "topic", False, True)


def test_invalid_primary_does_not_block_stacked(capsys):
    labels = forking.inherited_pr_labels(_labels(pr="bad", pr_branch="x", stacked_pr="5"), "src")
    assert labels == {"user.jailbee.stacked_pr": "5"}
    assert "bad" in capsys.readouterr().out


def test_inherited_unbound_and_malformed_submodule():
    assert forking.inherited_pr_labels(_labels(sub_pr="not json"), "src") == {}


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


def test_prepare_fork_brings_the_source_submodule_commits_to_the_host(cfg, wired, mocker):
    # The fork's clone inits its submodules from the host sub-repos; a gitlink
    # the source committed exists only in the source until it is transported.
    _, fetch = wired
    order = MagicMock()
    order.attach_mock(fetch, "fetch")
    transport = mocker.patch("jailbee.submodules.transport_submodules_to_host")
    order.attach_mock(transport, "transport")
    incus = MagicMock()
    forking.prepare_fork(cfg, incus, "src")
    transport.assert_called_once_with(cfg, incus, "myrepo-src", "src", repo_dir="/home/dev/myrepo")
    assert [c[0] for c in order.mock_calls] == ["fetch", "transport"]


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
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "c",
    )
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


def test_fork_does_not_inherit_branch_pr_bindings():
    labels = forking.inherited_pr_labels(_labels(branch_prs='{"a": {"pr": 42}}'), "src")
    assert "user.jailbee.branch_prs" not in labels


@pytest.mark.parametrize("ref", ["../x", "HEAD~1", "abc123"])
def test_explicit_ref_rejects_invalid_input_before_fetch(cfg, wired, mocker, ref):
    fetch = mocker.patch("jailbee.sync.fetch_ref_from_container")
    with pytest.raises(forking.ForkError, match="Invalid"):
        forking.prepare_fork(cfg, _labels(), "src", ref=ref)
    fetch.assert_not_called()


def test_explicit_ref_resolves_source_commit_without_dirty_check(cfg, wired, mocker):
    sha = "a" * 40
    dirty, fetch = wired
    dirty.return_value = True
    incus = _labels(pr="42", base_branch="main")
    incus.exec.return_value = sha + "\n"
    pinned = mocker.patch(
        "jailbee.sync.fetch_ref_from_container", return_value="refs/jailbee/src/source/heads/topic"
    )
    transport = mocker.patch(
        "jailbee.submodules.transport_submodules_to_host_at_commit", create=True
    )
    result = forking.prepare_fork(cfg, incus, "src", ref="refs/heads/topic")
    assert result.commit == sha
    assert result.untrusted and result.pr_labels == {}
    dirty.assert_not_called()
    fetch.assert_not_called()
    pinned.assert_called_once_with(cfg, incus, "src", "refs/heads/topic", sha)
    transport.assert_called_once_with(
        cfg, incus, "myrepo-src", "src", repo_dir="/home/dev/myrepo", commit=sha
    )


def test_explicit_full_ref_requires_commit_in_source(cfg, wired, mocker):
    from jailbee.incus import IncusError

    incus = _labels()
    incus.exec.side_effect = IncusError("missing")
    fetch = mocker.patch("jailbee.sync.fetch_ref_from_container")
    with pytest.raises(forking.ForkError, match="refs/heads/missing"):
        forking.prepare_fork(cfg, incus, "src", ref="refs/heads/missing")
    fetch.assert_not_called()


def test_explicit_sha_requires_commit_in_source(cfg, wired, mocker):
    from jailbee.incus import IncusError

    incus = _labels()
    incus.exec.side_effect = IncusError("missing")
    fetch = mocker.patch("jailbee.sync.fetch_sha_from_container")
    sha = "a" * 40
    with pytest.raises(forking.ForkError, match="resolve"):
        forking.prepare_fork(cfg, incus, "src", ref=sha)
    fetch.assert_not_called()


def test_explicit_sha_fetches_verified_commit(cfg, wired, mocker):
    sha = "b" * 40
    incus = _labels(branch="main")
    incus.exec.return_value = sha
    fetch = mocker.patch("jailbee.sync.fetch_sha_from_container")
    mocker.patch("jailbee.submodules.transport_submodules_to_host_at_commit")
    result = forking.prepare_fork(cfg, incus, "src", ref=sha)
    assert result.commit == sha
    assert any("^{commit}" in arg for call in incus.exec.call_args_list for arg in call.args[1])
    fetch.assert_called_once_with(cfg, incus, "src", sha)


def test_historical_submodule_gitlink_is_fetched_not_current_head(cfg, mocker):
    from jailbee import submodules

    sha = "a" * 40
    child_sha = "b" * 40
    mocker.patch("jailbee.submodules._gitmodules_paths_at", side_effect=[[("lib", "lib")], []])
    mocker.patch("jailbee.submodules._gitlink_at", return_value=child_sha)
    sub = submodules.HostSubRepo("lib", cfg.repo_root / "lib.git", None)
    mocker.patch("jailbee.submodules.locate_host_subrepo", return_value=sub)
    mocker.patch("jailbee.git.check_ref_format", return_value=True)
    fetch = mocker.patch("jailbee.git.fetch_url")
    mocker.patch("jailbee.git.rev_parse", return_value=child_sha)
    submodules.transport_submodules_to_host_at_commit(
        cfg, MagicMock(), "myrepo-src", "src", repo_dir="/repo", commit=sha
    )
    assert fetch.call_args.args[2] == f"+{child_sha}:refs/jailbee-sub/src/lib/commits/{child_sha}"
