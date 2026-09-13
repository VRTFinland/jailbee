"""Tests for the sync module (gie git fetch / checkout / merge)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jailbee import sync


def test_build_ext_url_format(mocker, make_cfg, tmp_path):
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.sync import _build_ext_url

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    # Pre-feature container: no repo_dir label → fall back to repo_root.name.
    incus.config_get.return_value = None
    url = _build_ext_url(cfg, incus, "sampleapp-feat-foo")
    expected_repo = f"/home/{CONTAINER_USERNAME}/{tmp_path.name}"
    assert url == (
        f"ext::incus exec --user {cfg.container_user.uid} "
        f"sampleapp-feat-foo -- git upload-pack {expected_repo}"
    )


def test_build_ext_url_uses_persisted_repo_dir_label(mocker, make_cfg, tmp_path):
    """When user.jailbee.repo_dir is set, _build_ext_url uses the persisted path."""
    from jailbee.sync import _build_ext_url

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.return_value = "/home/dev/gisgro"
    url = _build_ext_url(cfg, incus, "sampleapp-feat-foo")
    assert url.endswith("-- git upload-pack /home/dev/gisgro")


def test_fetch_result_dataclass_shape():
    from jailbee.sync import FetchResult

    r = FetchResult(
        branch="feat/foo", old_oid=None, new_oid="abc", base_oid="base", commits_added=2
    )
    assert r.branch == "feat/foo"
    assert r.old_oid is None
    assert r.new_oid == "abc"
    assert r.base_oid == "base"
    assert r.commits_added == 2


def _mock_container_running(incus_mock, name: str):
    """Make incus.list_containers return one running container with that name."""
    incus_mock.list_containers.return_value = [{"name": name, "status": "Running", "profiles": []}]


def _mock_container_stopped(incus_mock, name: str):
    incus_mock.list_containers.return_value = [{"name": name, "status": "Stopped", "profiles": []}]


def test_fetch_happy_path_with_user_gie_branch(mocker, make_cfg, tmp_path):
    from jailbee.sync import fetch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = "feat/foo"
    incus.exec.return_value = ""

    mocker.patch("jailbee.sync.git.rev_parse", side_effect=["abc1234", "def5678"])
    mock_fetch = mocker.patch("jailbee.sync.git.fetch_url")
    mocker.patch(
        "jailbee.sync.git.log_oneline",
        return_value=["def5678 fix", "9abcdef tests"],
    )
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    result = fetch_from_container(cfg, incus, "feat-foo")

    assert result.branch == "feat/foo"
    assert result.old_oid == "abc1234"
    assert result.new_oid == "def5678"
    assert result.base_oid == "abc1234"
    assert result.commits_added == 2
    mock_fetch.assert_called_once()
    args = mock_fetch.call_args.args
    assert args[0] == cfg.repo_root
    assert args[1].startswith("ext::incus exec --user ")
    assert args[2] == "+refs/heads/feat/foo:refs/jailbee/feat-foo/feat/foo"


def test_fetch_falls_back_to_git_head_when_meta_missing(mocker, make_cfg, tmp_path):
    from jailbee.sync import fetch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    def exec_side_effect(name, cmd, **kwargs):
        if "test" in cmd:
            return ""
        if "symbolic-ref" in cmd:
            return "feat/foo\n"
        return ""

    incus.exec.side_effect = exec_side_effect

    # rev_parse calls: (1) old ref, (2) new ref after fetch, (3) HEAD as base.
    mocker.patch("jailbee.sync.git.rev_parse", side_effect=[None, "def5678", "headoid1"])
    mocker.patch("jailbee.sync.git.fetch_url")
    mock_log = mocker.patch("jailbee.sync.git.log_oneline", return_value=["def5678 first"])
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    result = fetch_from_container(cfg, incus, "feat-foo")

    assert result.branch == "feat/foo"
    assert result.old_oid is None
    assert result.base_oid == "headoid1"
    assert result.commits_added == 1
    # Count must come from HEAD..new_oid, not the full history of new_oid.
    mock_log.assert_called_once_with(cfg.repo_root, "headoid1..def5678")


def test_fetch_prefers_container_head_over_user_gie_branch_label(mocker, make_cfg, tmp_path):
    """If the user checked out a different branch inside the container after
    `gie new`, fetch should follow the container's actual HEAD, not the
    stale `user.jailbee.branch` label."""
    from jailbee.sync import fetch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)

    def exec_side_effect(name, cmd, **kwargs):
        if "test" in cmd:
            return ""
        if "symbolic-ref" in cmd:
            return "user/midnight\n"
        return ""

    incus.exec.side_effect = exec_side_effect
    # Label says "build-scripts" — stale; should not be used.
    incus.config_get.return_value = "build-scripts"

    mocker.patch("jailbee.sync.git.rev_parse", side_effect=["abc", "def", "head"])
    mocker.patch("jailbee.sync.git.fetch_url")
    mocker.patch("jailbee.sync.git.log_oneline", return_value=[])
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    result = fetch_from_container(cfg, incus, "feat-foo")
    assert result.branch == "user/midnight"


def test_fetch_falls_back_to_label_on_detached_head(mocker, make_cfg, tmp_path):
    """If the container's HEAD is detached (no symbolic-ref), use the label."""
    from jailbee.incus import IncusError
    from jailbee.sync import fetch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)

    def exec_side_effect(name, cmd, **kwargs):
        if "symbolic-ref" in cmd:
            raise IncusError("detached")
        return ""

    incus.exec.side_effect = exec_side_effect
    incus.config_get.return_value = "feat/foo"

    mocker.patch("jailbee.sync.git.rev_parse", side_effect=["abc", "def", "head"])
    mocker.patch("jailbee.sync.git.fetch_url")
    mocker.patch("jailbee.sync.git.log_oneline", return_value=[])
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    result = fetch_from_container(cfg, incus, "feat-foo")
    assert result.branch == "feat/foo"


def test_fetch_explicit_branch_overrides_lookup(mocker, make_cfg, tmp_path):
    from jailbee.sync import fetch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = "feat/foo"
    incus.exec.return_value = ""

    mocker.patch("jailbee.sync.git.rev_parse", side_effect=[None, "xxx", "headoid2"])
    mock_fetch = mocker.patch("jailbee.sync.git.fetch_url")
    mocker.patch("jailbee.sync.git.log_oneline", return_value=["xxx"])
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    fetch_from_container(cfg, incus, "feat-foo", branch="other/branch")

    # Explicit branch override means user.jailbee.branch must not be read.
    # (user.jailbee.mode is read by the mount-mode guard — that's fine.)
    branch_lookups = [
        c for c in incus.config_get.call_args_list if c.args[1] == "user.jailbee.branch"
    ]
    assert branch_lookups == []
    assert (
        mock_fetch.call_args.args[2]
        == "+refs/heads/other/branch:refs/jailbee/feat-foo/other/branch"
    )


def test_fetch_stopped_container_raises(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, fetch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_stopped(incus, full)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    with pytest.raises(SyncError) as exc:
        fetch_from_container(cfg, incus, "feat-foo")
    assert "not running" in str(exc.value).lower()
    assert "jailbee start" in str(exc.value)


def test_fetch_no_clone_raises(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, fetch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = "feat/foo"
    incus.exec.side_effect = IncusError("exec failed")
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    with pytest.raises(SyncError) as exc:
        fetch_from_container(cfg, incus, "feat-foo")
    assert "no clone" in str(exc.value).lower()


def test_fetch_branch_unresolvable_raises(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, fetch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    def exec_side_effect(name, cmd, **kwargs):
        if "test" in cmd:
            return ""
        if "symbolic-ref" in cmd:
            return ""
        return ""

    incus.exec.side_effect = exec_side_effect
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    with pytest.raises(SyncError) as exc:
        fetch_from_container(cfg, incus, "feat-foo")
    assert "--branch" in str(exc.value)


def test_fetch_no_op_when_no_new_commits(mocker, make_cfg, tmp_path):
    from jailbee.sync import fetch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = "feat/foo"
    incus.exec.return_value = ""

    same_oid = "abc1234"
    mocker.patch("jailbee.sync.git.rev_parse", side_effect=[same_oid, same_oid])
    mocker.patch("jailbee.sync.git.fetch_url")
    mocker.patch("jailbee.sync.git.log_oneline", return_value=[])
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    result = fetch_from_container(cfg, incus, "feat-foo")
    assert result.commits_added == 0
    assert result.old_oid == same_oid
    assert result.new_oid == same_oid


def test_fetch_rejects_a_branch_the_container_does_not_have(mocker, make_cfg, tmp_path):
    """An explicit `-b` naming a branch that isn't in the container must fail
    with a SyncError naming the available branches — not with a raw GitError
    from `git fetch` ("couldn't find remote ref") several frames down.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-compose-4"
    _mock_container_running(incus, full)
    incus.config_get.return_value = "compose-4"
    incus.exec.side_effect = lambda _n, cmd, **_kw: (
        "compose-4\nmain\n" if "for-each-ref" in cmd else ""
    )
    mock_fetch = mocker.patch("jailbee.sync.git.fetch_url")
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    with pytest.raises(sync.SyncError) as exc:
        sync.fetch_from_container(cfg, incus, "compose-4", branch="compose-4-1")

    msg = str(exc.value)
    assert "compose-4-1" in msg
    assert "compose-4, main" in msg
    assert "--branch" in msg  # explains what -b actually selects
    mock_fetch.assert_not_called()


def test_fetch_rejects_a_stale_branch_label_without_blaming_the_flag(mocker, make_cfg, tmp_path):
    """Same guard on the auto-detected path (stale `user.jailbee.branch` label):
    the hint tells the user to pick a branch, not what `-b` means.
    """
    from jailbee.incus import IncusError

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = "gone"

    def _exec(_n, cmd, **_kw):
        if "for-each-ref" in cmd:
            return "main\n"
        if "symbolic-ref" in cmd:
            raise IncusError("detached")
        return ""

    incus.exec.side_effect = _exec
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    with pytest.raises(sync.SyncError) as exc:
        sync.fetch_from_container(cfg, incus, "feat-foo")

    msg = str(exc.value)
    assert "'gone'" in msg
    assert "--branch <name>" in msg


def test_fetch_still_runs_when_the_branch_list_is_unavailable(mocker, make_cfg, tmp_path):
    """The guard must not turn an unreadable branch list into a false
    "no such branch" — an exec failure means unknown, so the fetch proceeds.
    """
    from jailbee.incus import IncusError

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = "feat/foo"

    def _exec(_n, cmd, **_kw):
        if "for-each-ref" in cmd:
            raise IncusError("exec failed")
        return ""

    incus.exec.side_effect = _exec
    mocker.patch("jailbee.sync.git.rev_parse", side_effect=["abc1234", "def5678"])
    mock_fetch = mocker.patch("jailbee.sync.git.fetch_url")
    mocker.patch("jailbee.sync.git.log_oneline", return_value=[])
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    result = sync.fetch_from_container(cfg, incus, "feat-foo")

    assert result.branch == "feat/foo"
    mock_fetch.assert_called_once()


def _stub_fetch(
    mocker, branch="feat/foo", short="feat-foo", new_oid="def5678", head_oid="def5678def"
):
    """Stub fetch_from_container to skip the incus path entirely.

    Also stubs `git.rev_parse` so `checkout_from_container` /
    `merge_from_container` can look up the post-op HEAD oid without
    running a real subprocess.
    """
    from jailbee.sync import FetchResult

    mocker.patch("jailbee.sync.git.rev_parse", return_value=head_oid)
    return mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=FetchResult(
            branch=branch,
            old_oid="abc1234",
            new_oid=new_oid,
            base_oid="abc1234",
            commits_added=2,
        ),
    )


def _sync_refs_setup(mocker, cfg, short="feat-foo", branch="feat/foo"):
    """Common wiring for sync_refs_from_container tests."""
    from jailbee.sync import FetchResult

    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-{short}"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None  # no pr_branch label
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=FetchResult(
            branch=branch, old_oid=None, new_oid="newsha", base_oid=None, commits_added=2
        ),
    )
    return incus, full


def test_sync_refs_creates_the_host_branch_without_checking_it_out(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)  # branch absent
    update = mocker.patch("jailbee.sync.git.update_ref", return_value=True)
    checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo")

    assert result.target == "feat/foo"
    assert result.superproject.status == "created"
    update.assert_called_once_with(cfg.repo_root, "refs/heads/feat/foo", "newsha", old_oid=None)
    checkout.assert_not_called()


def test_sync_refs_leaves_a_diverged_branch_alone(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="oldsha")
    mocker.patch("jailbee.sync.git.run_capture", return_value=(False, ""))  # not an ancestor
    update = mocker.patch("jailbee.sync.git.update_ref")
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo")

    assert result.superproject.status == "diverged"
    update.assert_not_called()


def test_sync_refs_forces_a_diverged_branch_that_is_not_checked_out(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="oldsha")
    mocker.patch("jailbee.sync.git.run_capture", return_value=(False, ""))
    update = mocker.patch("jailbee.sync.git.update_ref", return_value=True)
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo", force=True)

    assert result.superproject.status == "forced"
    assert result.superproject.old_oid == "oldsha"
    update.assert_called_once_with(cfg.repo_root, "refs/heads/feat/foo", "newsha", old_oid=None)


def test_sync_refs_refuses_to_force_the_checked_out_branch(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    # The host is sitting ON feat/foo, and the container has diverged from it.
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="oldsha")
    # Clean tree: the merge's own GitError is what forces the classification
    # below to fall through to the `force`-was-asked-for branch, not the
    # dirty-tree one.
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=False)
    mocker.patch("jailbee.sync.git.merge_ref", side_effect=sync.git.GitError("not a ff"))
    # Mocked although the code path must not reach it: an implementation that
    # fell through to the ref ladder would otherwise shell out to real git.
    mocker.patch("jailbee.sync.git.run_capture", return_value=(False, ""))
    update = mocker.patch("jailbee.sync.git.update_ref")
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo", force=True)

    # Forcing a ref out from under a live index and working tree is the one
    # destructive case this command will not perform.
    assert result.superproject.status == "refused"
    update.assert_not_called()


def test_sync_refs_refuses_the_checked_out_branch_when_the_tree_is_dirty(
    mocker, make_cfg, tmp_path
):
    """When git's own `--ff-only` merge declines because local modifications
    are in the way, that is `"refused"`, not `"diverged"` — the branches may
    well be fast-forwardable; it's the working tree blocking it (Important
    2). The merge is always attempted now — the old pre-emptive
    `host_tree_dirty` short-circuit is gone — so this test's job is only the
    post-failure classification, not whether the merge runs.
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="oldsha")
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=True)
    merge = mocker.patch(
        "jailbee.sync.git.merge_ref",
        side_effect=sync.git.GitError("Your local changes would be overwritten by merge"),
    )
    mocker.patch("jailbee.sync.git.run_capture", return_value=(False, ""))
    update = mocker.patch("jailbee.sync.git.update_ref")
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo")

    assert result.superproject.status == "refused"
    merge.assert_called_once()
    update.assert_not_called()


def test_sync_refs_fast_forwards_the_checked_out_branch_even_with_a_dirty_tree(
    mocker, make_cfg, tmp_path
):
    """The case Important 2 restored: a dirty tree must not pre-empt a
    fast-forward that git's own `--ff-only` merge would have allowed
    anyway (e.g. an untracked file in an unrelated directory). `place_branch`
    is not consulted here — `host_tree_dirty` is only ever read *after* the
    merge fails, never before attempting it.
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="oldsha")
    dirty = mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=True)
    merge = mocker.patch("jailbee.sync.git.merge_ref")
    update = mocker.patch("jailbee.sync.git.update_ref")
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo")

    assert result.superproject.status == "checked-out-ff"
    assert merge.call_args.kwargs["ff_only"] is True
    dirty.assert_not_called()
    update.assert_not_called()


def test_sync_refs_fast_forwards_the_checked_out_branch_in_place(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="oldsha")
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=False)
    merge = mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.git.run_capture", return_value=(False, ""))
    update = mocker.patch("jailbee.sync.git.update_ref")
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo")

    assert result.superproject.status == "checked-out-ff"
    assert merge.call_args.kwargs["ff_only"] is True
    assert merge.call_args.args[1] == "refs/jailbee/feat-foo/feat/foo"
    # A ref write here would desync HEAD's index and working tree.
    update.assert_not_called()


def test_sync_refs_places_a_branch_head_already_points_at_unborn(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    # HEAD is on an unborn `feat/foo`: creating the ref is correct, and the
    # checked-out-branch special case must not swallow it.
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=True)
    merge = mocker.patch("jailbee.sync.git.merge_ref")
    update = mocker.patch("jailbee.sync.git.update_ref", return_value=True)
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo")

    assert result.superproject.status == "created"
    merge.assert_not_called()
    update.assert_called_once_with(cfg.repo_root, "refs/heads/feat/foo", "newsha", old_oid=None)


def test_sync_refs_reports_up_to_date_on_the_checked_out_branch(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")  # already there
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=True)
    merge = mocker.patch("jailbee.sync.git.merge_ref")
    update = mocker.patch("jailbee.sync.git.update_ref")
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo")

    # A no-op must stay a no-op even on HEAD's own branch with a dirty tree.
    assert result.superproject.status == "up-to-date"
    merge.assert_not_called()
    update.assert_not_called()


def test_sync_refs_uses_the_pr_branch_label_as_the_target(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, full = _sync_refs_setup(mocker, cfg)
    incus.config_get.return_value = "author/pr-head"
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.update_ref", return_value=True)
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo")

    # Same target rule as checkout_from_container, so fetch-then-switch lands
    # exactly where a checkout would have.
    assert result.target == "author/pr-head"
    # Pin the key: a MagicMock answers every key with the same value, so
    # without this the test would pass on a label read from the wrong one.
    incus.config_get.assert_called_with(full, "user.jailbee.pr_branch")


def test_sync_refs_as_name_wins_over_the_pr_branch_label(mocker, make_cfg, tmp_path):
    """`--as` renames the HOST branch but must never change which ref gets
    fetched (Minor 3). Driven through the checked-out-ff path, where
    `_place_host_branch` calls `git.merge_ref` with `fetched_ref` by name —
    the one place a `target`-vs-container-`branch` mix-up in that ref string
    would actually be observable; every "created" test only ever passes
    `new_oid` to `place_branch`, never `fetched_ref` itself.
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    incus.config_get.return_value = "author/pr-head"
    # HEAD is already on the renamed target, one commit behind.
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="mine")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="oldsha")
    merge = mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo", as_name="mine")

    assert result.target == "mine"
    assert result.superproject.name == "refs/heads/mine"
    assert result.superproject.status == "checked-out-ff"
    # The container branch fetched is "feat/foo" (the _sync_refs_setup
    # default) — `--as mine` must not change what gets fetched.
    assert merge.call_args.args[1] == "refs/jailbee/feat-foo/feat/foo"


def test_sync_refs_places_submodule_branches_from_the_fetched_commit(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.update_ref", return_value=True)
    place = mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    sync.sync_refs_from_container(cfg, incus, "feat-foo")

    place.assert_called_once_with(cfg.repo_root, "newsha", "feat/foo", force=False)


def test_sync_refs_forwards_force_to_the_submodule_placement(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.update_ref", return_value=True)
    place = mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    sync.sync_refs_from_container(cfg, incus, "feat-foo", force=True)

    place.assert_called_once_with(cfg.repo_root, "newsha", "feat/foo", force=True)


def test_sync_refs_transports_submodule_objects_and_returns_the_whole_picture(
    mocker, make_cfg, tmp_path
):
    from jailbee import submodules as submodules_mod
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, full = _sync_refs_setup(mocker, cfg)
    transport = mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.update_ref", return_value=True)
    placement = submodules_mod.SubBranchPlacement("libs/sub", "created", None, "subsha")
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[placement])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo")

    # Objects have to reach the host too, or the submodule refs the step
    # below writes would point at commits the host does not have.
    transport.assert_called_once_with(cfg, incus, full, "feat-foo", repo_dir="/repo")
    assert result.fetch.commits_added == 2
    assert result.fetch.new_oid == "newsha"
    assert result.submodules == (placement,)


def test_sync_refs_reports_a_failed_ref_write(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.update_ref", return_value=False)
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    result = sync.sync_refs_from_container(cfg, incus, "feat-foo")

    assert result.superproject.status == "failed"


def _synced_result(
    *, target="feat/foo", status="created", old_oid=None, new_oid="newsha", branch="feat/foo"
):
    """A `SyncRefsResult` shaped like `sync_refs_from_container`'s return, for
    `checkout_from_container` tests that mock it out entirely — the ref
    mechanics behind each `status` are `test_sync_refs_*`'s job, not this
    layer's.
    """
    from jailbee import sync

    return sync.SyncRefsResult(
        fetch=sync.FetchResult(
            branch=branch, old_oid=None, new_oid=new_oid, base_oid=None, commits_added=1
        ),
        target=target,
        superproject=sync.BranchPlacement(f"refs/heads/{target}", status, old_oid, new_oid),
        submodules=(),
    )


def test_checkout_delegates_to_sync_refs_and_then_switches(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="created"),
    )
    # Corrections to the brief's scaffolding (see task-7-report.md): neither
    # of these is mocked in the brief, so left alone they shell out to real
    # git in tmp_path. get_current_branch must also differ from the target or
    # `checkout.assert_called_once_with(...)` below never fires.
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    update_subs = mocker.patch("jailbee.submodules.update_submodules_on_host")

    result = sync.checkout_from_container(cfg, incus, "feat-foo")

    assert result.branch == "feat/foo"
    assert result.created_new is True
    assert result.head_oid == "newsha"
    assert result.fetch.commits_added == 1
    checkout.assert_called_once_with(cfg.repo_root, "feat/foo")
    update_subs.assert_called_once_with(cfg.repo_root, branch="feat/foo")


def test_checkout_still_raises_on_divergence(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="diverged", old_oid="oldsha"),
    )

    with pytest.raises(sync.SyncError, match="jailbee git pull feat-foo"):
        sync.checkout_from_container(cfg, incus, "feat-foo")


def test_checkout_still_raises_when_refused(mocker, make_cfg, tmp_path):
    """`"refused"` is `_place_host_branch`'s guard for HEAD's own branch:
    either the ff-only merge found uncommitted local changes in the way, or
    (rarer) `force` was requested on a checked-out branch. Neither is a
    divergence — the message must name local changes, not blame the
    container, and must not suggest `--force` or `jailbee git pull` (Important
    2 / Minor 6): a dirty tree makes `pull` refuse for the identical reason.
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="refused", old_oid="oldsha"),
    )

    with pytest.raises(sync.SyncError) as exc:
        sync.checkout_from_container(cfg, incus, "feat-foo")
    msg = str(exc.value)
    assert "uncommitted" in msg.lower()
    assert "jailbee git pull" not in msg
    assert "--force" not in msg
    assert "diverged" not in msg.lower()


def test_checkout_still_raises_when_the_ref_write_failed(mocker, make_cfg, tmp_path):
    """`"failed"` is a ref write git itself refused (e.g. a lost update_ref
    race) — not a divergence, so the message must not claim one or point at
    `jailbee git pull` (Minor 6).
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="failed", old_oid="oldsha"),
    )

    with pytest.raises(sync.SyncError) as exc:
        sync.checkout_from_container(cfg, incus, "feat-foo")
    msg = str(exc.value)
    assert "jailbee git pull" not in msg
    assert "diverged" not in msg.lower()


def test_checkout_still_raises_on_the_checked_out_backstop(mocker, make_cfg, tmp_path):
    """`"checked-out"` is `place_branch`'s refusal for a moving write on
    HEAD's own branch. `_place_host_branch` intercepts that case earlier and
    does the fast-forward merge instead, so this is unreachable outside a
    race — but it is in the type, and a checkout that proceeded on it would
    switch onto a branch that never moved. Not a divergence either (Minor 6).
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="checked-out", old_oid="oldsha"),
    )

    with pytest.raises(sync.SyncError) as exc:
        sync.checkout_from_container(cfg, incus, "feat-foo")
    msg = str(exc.value)
    assert "did not move" in msg.lower()
    assert "jailbee git pull" not in msg
    assert "diverged" not in msg.lower()


def test_checkout_sets_tracking_when_created_and_origin_branch_exists(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="created"),
    )
    remote_exists = mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    set_upstream = mocker.patch("jailbee.sync.git.set_upstream")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    mocker.patch("jailbee.submodules.update_submodules_on_host")

    sync.checkout_from_container(cfg, incus, "feat-foo")

    remote_exists.assert_called_once_with(cfg.repo_root, cfg.upstream_remote, "feat/foo")
    set_upstream.assert_called_once_with(
        cfg.repo_root, "feat/foo", f"{cfg.upstream_remote}/feat/foo"
    )


def test_checkout_skips_tracking_when_origin_branch_missing(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="created"),
    )
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    set_upstream = mocker.patch("jailbee.sync.git.set_upstream")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    mocker.patch("jailbee.submodules.update_submodules_on_host")

    sync.checkout_from_container(cfg, incus, "feat-foo")

    set_upstream.assert_not_called()


def test_checkout_never_probes_tracking_for_an_existing_branch(mocker, make_cfg, tmp_path):
    """Tracking is only ever restored for a branch this call itself created —
    an existing branch's tracking config is left exactly as the user set it.
    A version of this task that gated on the wrong condition (e.g. "target
    isn't the current branch" instead of `status == "created"`) would pass
    every other test here and still silently touch tracking on every
    fast-forward.
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="fast-forwarded", old_oid="oldsha"),
    )
    remote_exists = mocker.patch("jailbee.sync.git.remote_ref_exists")
    set_upstream = mocker.patch("jailbee.sync.git.set_upstream")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    mocker.patch("jailbee.submodules.update_submodules_on_host")

    result = sync.checkout_from_container(cfg, incus, "feat-foo")

    assert result.created_new is False
    remote_exists.assert_not_called()
    set_upstream.assert_not_called()


def test_checkout_skips_the_switch_when_already_on_target(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="checked-out-ff", old_oid="oldsha"),
    )
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    mocker.patch("jailbee.submodules.update_submodules_on_host")

    sync.checkout_from_container(cfg, incus, "feat-foo")

    checkout.assert_not_called()


def test_checkout_switches_when_not_on_target(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="fast-forwarded", old_oid="oldsha"),
    )
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    mocker.patch("jailbee.submodules.update_submodules_on_host")

    sync.checkout_from_container(cfg, incus, "feat-foo")

    checkout.assert_called_once_with(cfg.repo_root, "feat/foo")


def test_checkout_forwards_branch_and_as_name_without_forcing(mocker, make_cfg, tmp_path):
    """`branch` (what to read from the container) and `as_name` (what to
    write on the host) pass straight through; `force` is not part of
    `checkout_from_container`'s signature and must stay at its default — a
    checkout must never overwrite host history.

    This is also the only test where `refs.target` ("mine") differs from
    `refs.fetch.branch` ("feat/foo") — the `--as`/PR-label case the deleted
    `as_name` tests used to cover. Asserting downstream of the delegation
    (not just the forwarded call) is what catches a `target = refs.fetch.branch`
    regression: everything above the delegation would still pass, but HEAD
    would end up on the wrong branch and submodules would be updated for the
    wrong one.
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    sync_refs = mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(target="mine", status="created"),
    )
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    update_subs = mocker.patch("jailbee.submodules.update_submodules_on_host")

    result = sync.checkout_from_container(
        cfg, incus, "feat-foo", branch="other/branch", as_name="mine"
    )

    # tags="reachable" is checkout_from_container's own default, forwarded
    # unconditionally now that it threads the tag policy through.
    sync_refs.assert_called_once_with(
        cfg, incus, "feat-foo", branch="other/branch", as_name="mine", tags="reachable"
    )
    assert result.branch == "mine"
    checkout.assert_called_once_with(cfg.repo_root, "mine")
    update_subs.assert_called_once_with(cfg.repo_root, branch="mine")


def test_checkout_updates_submodules_last(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="fast-forwarded", old_oid="oldsha"),
    )
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")

    order: list[str] = []
    mocker.patch(
        "jailbee.sync.git.checkout_branch", side_effect=lambda *a: order.append("checkout")
    )
    mocker.patch(
        "jailbee.submodules.update_submodules_on_host",
        side_effect=lambda *a, **kw: order.append("update_submodules"),
    )

    sync.checkout_from_container(cfg, incus, "feat-foo")

    assert order == ["checkout", "update_submodules"]


def test_checkout_raises_when_head_does_not_resolve(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="fast-forwarded", old_oid="oldsha"),
    )
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    update_subs = mocker.patch("jailbee.submodules.update_submodules_on_host")

    with pytest.raises(sync.SyncError, match="HEAD did not resolve"):
        sync.checkout_from_container(cfg, incus, "feat-foo")

    update_subs.assert_not_called()


def test_checkout_returns_full_result_for_an_existing_branch(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="fast-forwarded", old_oid="oldsha", new_oid="newsha"),
    )
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    mocker.patch("jailbee.submodules.update_submodules_on_host")

    result = sync.checkout_from_container(cfg, incus, "feat-foo")

    assert isinstance(result, sync.CheckoutResult)
    assert result.branch == "feat/foo"
    assert result.head_oid == "newsha"
    assert result.created_new is False
    assert result.fetch.commits_added == 1


def _merge_result(
    make_cfg,
    tmp_path,
    *,
    branch="feat/foo",
    into_branch="main",
    commits_added=2,
    pre_merge_head="aaaaaaaa",
    head_oid="f00ba12",
):
    """Build a stub MergeResult for cleanup tests.

    Defaults represent a merge that moved HEAD (`pre_merge_head !=
    head_oid`). Pass `pre_merge_head=head_oid` to simulate a no-op.
    """
    from jailbee.sync import FetchResult, MergeResult

    return MergeResult(
        fetch=FetchResult(
            branch=branch,
            old_oid="abc1234",
            new_oid="def5678",
            base_oid="abc1234",
            commits_added=commits_added,
        ),
        branch=branch,
        head_oid=head_oid,
        into_branch=into_branch,
        pre_merge_head=pre_merge_head,
    )


def test_cleanup_destroys_container_with_flag_non_tty(mocker, make_cfg, tmp_path):
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="always",
        branch_policy="always",
    )

    mock_destroy.assert_called_once_with(cfg, incus, f"{cfg.container_prefix}-feat-foo", force=True)
    assert result.destroyed is True
    assert result.cleanup_error is None
    assert result.skipped_reason is None


def test_cleanup_skipped_in_non_tty_without_flag(mocker, make_cfg, tmp_path):
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="prompt",
        branch_policy="prompt",
    )

    mock_destroy.assert_not_called()
    assert result.destroyed is False


def test_cleanup_destroy_prompts_in_tty_yes(mocker, make_cfg, tmp_path):
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="y")

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="prompt",
        branch_policy="prompt",
    )

    mock_destroy.assert_called_once_with(cfg, incus, f"{cfg.container_prefix}-feat-foo", force=True)
    assert result.destroyed is True


def test_cleanup_destroy_prompts_in_tty_no(mocker, make_cfg, tmp_path):
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="")

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="prompt",
        branch_policy="prompt",
    )

    mock_destroy.assert_not_called()
    assert result.destroyed is False


def _guarded_container_info(full_name: str, cfg, *, state: str = "Running"):
    from jailbee.lifecycle import ContainerInfo

    return ContainerInfo(
        name=full_name,
        state=state,
        network=None,
        ip=None,
        memory_limit=None,
        repo=cfg.container_prefix,
        mode="clone",
        repo_dir="/home/dev/repo",
        base_branch="main",
    )


def test_cleanup_always_policy_skips_the_destroy_guard(mocker, make_cfg, tmp_path):
    """destroy_policy='always' (the --cleanup flag) is this call's --force
    equivalent: it must never block, so the guard is not even consulted."""
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    full_name = f"{cfg.container_prefix}-feat-foo"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full_name)
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    list_containers = mocker.patch("jailbee.lifecycle.list_containers")
    probe = mocker.patch("jailbee.git_status.probe_container_git")
    confirm = mocker.patch("jailbee.tui.typer.confirm")

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="always",
        branch_policy="never",
    )

    mock_destroy.assert_called_once_with(cfg, incus, full_name, force=True)
    assert result.destroyed is True
    list_containers.assert_not_called()
    probe.assert_not_called()
    confirm.assert_not_called()


def test_cleanup_destroy_guard_skips_second_prompt_when_clean(mocker, make_cfg, tmp_path):
    """Nothing at risk: the guard prints nothing and asks nothing extra —
    the plain first prompt stays the only one."""
    from jailbee.git_status import GitStatus
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    full_name = f"{cfg.container_prefix}-feat-foo"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full_name)
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="y")
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_guarded_container_info(full_name, cfg)],
    )
    mocker.patch(
        "jailbee.git_status.probe_container_git",
        return_value=GitStatus(wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok"),
    )
    confirm = mocker.patch("jailbee.tui.typer.confirm")

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="prompt",
        branch_policy="never",
    )

    confirm.assert_not_called()
    mock_destroy.assert_called_once_with(cfg, incus, full_name, force=True)
    assert result.destroyed is True


def test_cleanup_destroy_guard_declines_second_prompt_keeps_container(mocker, make_cfg, tmp_path):
    """At risk + second prompt declined (the guard's own default): the
    container survives even though the plain first prompt said yes."""
    from jailbee.git_status import GitStatus
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    full_name = f"{cfg.container_prefix}-feat-foo"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full_name)
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="y")
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_guarded_container_info(full_name, cfg)],
    )
    mocker.patch(
        "jailbee.git_status.probe_container_git",
        return_value=GitStatus(wt="+3 -1", ahead_diff="clean", ahead_count="0", conflict="ok"),
    )
    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)
    mocker.patch("jailbee.tui.typer.confirm", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="prompt",
        branch_policy="never",
    )

    mock_destroy.assert_not_called()
    assert result.destroyed is False


def test_cleanup_destroy_guard_accepts_second_prompt_destroys(mocker, make_cfg, tmp_path):
    """At risk + second prompt accepted: the destroy proceeds."""
    from jailbee.git_status import GitStatus
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    full_name = f"{cfg.container_prefix}-feat-foo"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full_name)
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="y")
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_guarded_container_info(full_name, cfg)],
    )
    mocker.patch(
        "jailbee.git_status.probe_container_git",
        return_value=GitStatus(wt="+3 -1", ahead_diff="clean", ahead_count="0", conflict="ok"),
    )
    mocker.patch("jailbee.destroy_guard.has_commit", return_value=False)
    mocker.patch("jailbee.tui.typer.confirm", return_value=True)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="prompt",
        branch_policy="never",
    )

    mock_destroy.assert_called_once_with(cfg, incus, full_name, force=True)
    assert result.destroyed is True


def test_cleanup_destroy_guard_notes_unknown_for_stopped_container(
    mocker, make_cfg, tmp_path, capsys
):
    """A stopped container is never probed — the guard notes the status is
    unknown rather than reading silence as safety, but still doesn't add a
    second prompt (nothing measurable to weigh)."""
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    full_name = f"{cfg.container_prefix}-feat-foo"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full_name)
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="y")
    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[_guarded_container_info(full_name, cfg, state="Stopped")],
    )
    probe = mocker.patch("jailbee.git_status.probe_container_git")
    confirm = mocker.patch("jailbee.tui.typer.confirm")

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="prompt",
        branch_policy="never",
    )

    probe.assert_not_called()
    confirm.assert_not_called()
    mock_destroy.assert_called_once_with(cfg, incus, full_name, force=True)
    assert result.destroyed is True
    assert "git status unknown" in capsys.readouterr().out.lower()


def test_cleanup_destroy_guard_notes_unknown_when_container_missing_from_listing(
    mocker, make_cfg, tmp_path, capsys
):
    """The container vanished from the listing between resolve and here —
    the same 'silence is never safety' note as the CLI's equivalent gap."""
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    full_name = f"{cfg.container_prefix}-feat-foo"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full_name)
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="y")
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[])
    probe = mocker.patch("jailbee.git_status.probe_container_git")
    confirm = mocker.patch("jailbee.tui.typer.confirm")

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="prompt",
        branch_policy="never",
    )

    probe.assert_not_called()
    confirm.assert_not_called()
    mock_destroy.assert_called_once_with(cfg, incus, full_name, force=True)
    assert result.destroyed is True
    assert "git status unknown" in capsys.readouterr().out.lower()


def test_cleanup_mount_mode_container_is_not_reported_as_unknown(
    mocker, make_cfg, tmp_path, capsys
):
    """`_warn_before_container_destroy` now shares `destroy_guard.
    status_is_unknown` (rather than its own `git_status is None` check), so
    a mount-mode container — whose working tree *is* the host directory and
    survives the destroy — is not flagged unknown just because it was never
    probed. Not reachable via `gie git pull` in production today (mount
    mode is refused earlier in that flow); this just keeps the predicate
    from drifting from the CLI's identical guard."""
    from jailbee.lifecycle import ContainerInfo
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    full_name = f"{cfg.container_prefix}-feat-foo"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full_name)
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="y")
    mount_info = ContainerInfo(
        name=full_name,
        state="Running",
        network=None,
        ip=None,
        memory_limit=None,
        repo=cfg.container_prefix,
        mode="mount",
        repo_dir="/home/dev/repo",
        base_branch="main",
    )
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[mount_info])
    probe = mocker.patch("jailbee.git_status.probe_container_git")
    confirm = mocker.patch("jailbee.tui.typer.confirm")

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="prompt",
        branch_policy="never",
    )

    probe.assert_not_called()  # mode == "mount" skips the probe too
    confirm.assert_not_called()
    mock_destroy.assert_called_once_with(cfg, incus, full_name, force=True)
    assert result.destroyed is True
    assert "git status unknown" not in capsys.readouterr().out.lower()


def test_cleanup_destroy_failure_is_warning_not_fatal(mocker, make_cfg, tmp_path):
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch(
        "jailbee.lifecycle.destroy_container",
        side_effect=RuntimeError("incus exploded"),
    )
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="always",
        branch_policy="always",
    )

    assert result.destroyed is False
    assert result.cleanup_error is not None
    assert "incus exploded" in result.cleanup_error


def test_cleanup_deletes_merged_host_branch(mocker, make_cfg, tmp_path):
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.is_merged_into", return_value=True)
    mock_delete = mocker.patch("jailbee.sync.git.delete_branch")
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="always",
        branch_policy="always",
    )

    mock_delete.assert_called_once_with(cfg.repo_root, "feat/foo")
    assert result.deleted_branch is True


def test_cleanup_skips_branch_delete_when_host_lacks_branch(mocker, make_cfg, tmp_path):
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mock_delete = mocker.patch("jailbee.sync.git.delete_branch")
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="always",
        branch_policy="always",
    )

    mock_delete.assert_not_called()
    assert result.deleted_branch is False


def test_cleanup_skips_branch_delete_when_branch_is_current_head(mocker, make_cfg, tmp_path):
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.is_merged_into", return_value=True)
    mock_delete = mocker.patch("jailbee.sync.git.delete_branch")
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path, into_branch="feat/foo"),
        destroy_policy="always",
        branch_policy="always",
    )

    mock_delete.assert_not_called()
    assert result.deleted_branch is False


def test_cleanup_skips_branch_delete_when_not_merged_into_head(mocker, make_cfg, tmp_path):
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.is_merged_into", return_value=False)
    mock_delete = mocker.patch("jailbee.sync.git.delete_branch")
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="always",
        branch_policy="always",
    )

    mock_delete.assert_not_called()
    assert result.deleted_branch is False


def test_cleanup_skipped_when_head_did_not_move(mocker, make_cfg, tmp_path):
    """HEAD unchanged by merge → don't destroy or delete, even with --cleanup.

    A no-op merge is a sign the user may have forgotten to commit
    inside the container; destroying it then would lose uncommitted work.
    """
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mock_delete = mocker.patch("jailbee.sync.git.delete_branch")
    mock_resolve = mocker.patch("jailbee.lifecycle.resolve_container_name")

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(
            make_cfg,
            tmp_path,
            commits_added=0,
            pre_merge_head="f00ba12",
            head_oid="f00ba12",
        ),
        destroy_policy="always",
        branch_policy="always",
    )

    mock_destroy.assert_not_called()
    mock_delete.assert_not_called()
    mock_resolve.assert_not_called()
    assert result.destroyed is False
    assert result.deleted_branch is False
    assert result.cleanup_error is None
    assert result.skipped_reason is not None
    assert "did not move HEAD" in result.skipped_reason


def test_cleanup_runs_when_fetch_added_no_commits_but_merge_moved_head(mocker, make_cfg, tmp_path):
    """Regression: prior fetch had populated the gie ref already (so
    `fetch.commits_added == 0`), but the current host branch was behind
    that ref and the merge moved HEAD. Cleanup must still run.
    """
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(
            make_cfg,
            tmp_path,
            commits_added=0,
            pre_merge_head="aaaaaaaa",
            head_oid="bbbbbbbb",
        ),
        destroy_policy="always",
        branch_policy="always",
    )

    mock_destroy.assert_called_once_with(cfg, incus, f"{cfg.container_prefix}-feat-foo", force=True)
    assert result.destroyed is True
    assert result.skipped_reason is None


def test_cleanup_branch_delete_failure_is_warning_not_fatal(mocker, make_cfg, tmp_path):
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.is_merged_into", return_value=True)
    mocker.patch(
        "jailbee.sync.git.delete_branch",
        side_effect=RuntimeError("oops"),
    )
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="always",
        branch_policy="always",
    )

    assert result.deleted_branch is False
    assert result.cleanup_error is not None
    assert "oops" in result.cleanup_error


def test_cleanup_branch_delete_checks_merged_into_into_branch_not_head(mocker, make_cfg, tmp_path):
    """Branch-delete guard must call is_merged_into with into_branch, not 'HEAD'.

    Regression: the FF-without-checkout path leaves HEAD off the merge
    target, so HEAD != into_branch.  The guard was using 'HEAD', causing
    false negatives (skipped delete) or false positives (deletes when it
    shouldn't) on those paths.
    """
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mock_is_merged = mocker.patch("jailbee.sync.git.is_merged_into", return_value=True)
    mock_delete = mocker.patch("jailbee.sync.git.delete_branch")
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path, into_branch="dev"),
        destroy_policy="always",
        branch_policy="always",
    )

    # is_merged_into must use the actual merge target ("dev"), not "HEAD"
    mock_is_merged.assert_called_once_with(cfg.repo_root, "feat/foo", "dev")
    mock_delete.assert_called_once_with(cfg.repo_root, "feat/foo")
    assert result.deleted_branch is True


def test_cleanup_branch_delete_skipped_when_into_branch_is_none(mocker, make_cfg, tmp_path):
    """Branch-delete guard must be skipped entirely when into_branch is None.

    A None into_branch means the merge ran on a detached HEAD (legacy
    fallback) — we can't determine the merge target, so the safe choice
    is to leave the branch alone.
    """
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mock_is_merged = mocker.patch("jailbee.sync.git.is_merged_into", return_value=True)
    mock_delete = mocker.patch("jailbee.sync.git.delete_branch")
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path, into_branch=None),
        destroy_policy="always",
        branch_policy="always",
    )

    mock_is_merged.assert_not_called()
    mock_delete.assert_not_called()
    assert result.deleted_branch is False


def _drive_merge_in_place(mocker, tmp_path, make_cfg, *, ff):
    """Drive `merge_from_container` down the in-place path (target == current
    HEAD).

    Mirrors the mock surface of `test_merge_runs_no_ff_with_message` /
    `test_merge_ff_only_passes_through`: no base_branch label, so the target
    falls back to the current branch and the merge runs in place. Callers
    patch `jailbee.git.merge_ref` themselves before calling this.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    return sync.merge_from_container(cfg, incus, "feat-foo", ff=ff)


def _drive_merge_via_checkout(mocker, tmp_path, make_cfg, *, ff):
    """Drive `merge_from_container` down the checkout path: target (the
    container's base branch) differs from current, `fast_forward_branch`
    reports divergence, and `allow_checkout=True`.

    Mirrors the mock surface of `test_merge_checkout_path_merges_and_stays`.
    Callers patch `jailbee.git.merge_ref` themselves before calling this.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="other")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="ccc")
    mocker.patch("jailbee.sync.git.fast_forward_branch", return_value=False)
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=False)
    mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    mocker.patch("jailbee.sync.refresh_container_base")

    return sync.merge_from_container(cfg, incus, "feat-x", ff=ff, allow_checkout=True)


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("never", {"no_ff": True, "ff_only": False}),
        ("auto", {"no_ff": False, "ff_only": False}),
        ("always", {"no_ff": False, "ff_only": True}),
    ],
)
def test_merge_in_place_honours_the_ff_policy(mocker, tmp_path, make_cfg, policy, expected):
    merge_ref = mocker.patch("jailbee.git.merge_ref")
    _drive_merge_in_place(mocker, tmp_path, make_cfg, ff=policy)  # target == current

    assert merge_ref.call_args.kwargs["no_ff"] is expected["no_ff"]
    assert merge_ref.call_args.kwargs["ff_only"] is expected["ff_only"]


def test_auto_keeps_the_provenance_message(mocker, tmp_path, make_cfg):
    """`-m` is ignored by git on a fast-forward, so `auto` still passes it."""
    merge_ref = mocker.patch("jailbee.git.merge_ref")
    _drive_merge_in_place(mocker, tmp_path, make_cfg, ff="auto")

    assert "from container" in (merge_ref.call_args.kwargs["message"] or "")


def test_checkout_path_honours_the_ff_policy(mocker, tmp_path, make_cfg):
    """Pre-existing defect: _merge_via_checkout used to force no_ff regardless."""
    merge_ref = mocker.patch("jailbee.git.merge_ref")
    _drive_merge_via_checkout(mocker, tmp_path, make_cfg, ff="auto")

    assert merge_ref.call_args.kwargs["no_ff"] is False


def test_checkout_path_honours_ff_never(mocker, tmp_path, make_cfg):
    """`pull.ff: never` is the documented way back to the pre-1.4.0 always-merge-
    commit behaviour, and path 3 (`_merge_via_checkout`) is exactly where that
    behaviour was hardcoded before `ff` existed — `test_checkout_path_honours_the_ff_policy`
    above only exercises `auto`, so this is the only test asserting `no_ff=True`
    actually reaches `git.merge_ref` on this path.
    """
    merge_ref = mocker.patch("jailbee.git.merge_ref")
    _drive_merge_via_checkout(mocker, tmp_path, make_cfg, ff="never")

    assert merge_ref.call_args.kwargs["no_ff"] is True


def test_checkout_path_refuses_under_always(mocker, tmp_path, make_cfg):
    """`always` means 'fail on divergence' — reaching the checkout merge is divergence."""
    with pytest.raises(sync.SyncError, match="diverged"):
        _drive_merge_via_checkout(mocker, tmp_path, make_cfg, ff="always")


def test_merge_returns_merge_result(mocker, make_cfg, tmp_path):
    from jailbee.sync import MergeResult, merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker, head_oid="f00ba12f00ba")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    result = merge_from_container(cfg, incus, "feat-foo")

    assert isinstance(result, MergeResult)
    assert result.branch == "feat/foo"
    assert result.head_oid == "f00ba12f00ba"
    assert result.into_branch == "main"


def test_merge_captures_pre_merge_head_distinct_from_post(mocker, make_cfg, tmp_path):
    """`pre_merge_head` must be read *before* `git.merge_ref` is called."""
    from jailbee.sync import FetchResult, merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=FetchResult(
            branch="feat/foo",
            old_oid="abc1234",
            new_oid="def5678",
            base_oid="abc1234",
            commits_added=0,
        ),
    )
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch(
        "jailbee.sync.git.rev_parse",
        side_effect=["pre_oid_aaaa", "post_oid_bbbb"],
    )
    mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    result = merge_from_container(cfg, incus, "feat-foo")

    assert result.pre_merge_head == "pre_oid_aaaa"
    assert result.head_oid == "post_oid_bbbb"


def test_merge_runs_no_ff_with_message(mocker, make_cfg, tmp_path):
    from jailbee.sync import merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mock_merge = mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    # Explicit "never": the new default is "auto", which no longer forces
    # no_ff=True — see test_merge_in_place_honours_the_ff_policy for that.
    merge_from_container(cfg, incus, "feat-foo", ff="never")

    mock_merge.assert_called_once_with(
        cfg.repo_root,
        "refs/jailbee/feat-foo/feat/foo",
        message="Merge branch 'feat/foo' from container feat-foo",
        no_ff=True,
        ff_only=False,
    )


def test_merge_ff_only_passes_through(mocker, make_cfg, tmp_path):
    from jailbee.sync import merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mock_merge = mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    merge_from_container(cfg, incus, "feat-foo", ff="always")

    mock_merge.assert_called_once_with(
        cfg.repo_root,
        "refs/jailbee/feat-foo/feat/foo",
        message=None,
        no_ff=False,
        ff_only=True,
    )


def test_merge_ff_only_propagates_git_error(mocker, make_cfg, tmp_path):
    from jailbee.git import GitError
    from jailbee.sync import merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch(
        "jailbee.sync.git.merge_ref",
        side_effect=GitError("git merge failed (exit 1)"),
    )

    with pytest.raises(GitError):
        merge_from_container(cfg, incus, "feat-foo", ff="always")


def test_merge_into_same_branch_proceeds_without_prompt(mocker, make_cfg, tmp_path):
    """Host and container branches are independent histories even when
    they share a name — merging is allowed without a confirmation prompt.
    """
    from jailbee.sync import merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mock_input = mocker.patch("builtins.input")
    mock_merge = mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    merge_from_container(cfg, incus, "feat-foo")
    mock_merge.assert_called_once()
    mock_input.assert_not_called()


def test_merge_into_same_branch_ff_only_proceeds_without_prompt(mocker, make_cfg, tmp_path):
    from jailbee.sync import merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mock_input = mocker.patch("builtins.input")
    mock_merge = mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    merge_from_container(cfg, incus, "feat-foo", ff="always")
    mock_merge.assert_called_once()
    mock_input.assert_not_called()


def test_merge_conflict_surfaces_sync_error(mocker, make_cfg, tmp_path):
    """A host merge conflict the resolver can't clear surfaces a SyncError summary
    (not a raw GitError) after the gitlink resolver is attempted."""
    from jailbee import sync as sync_mod
    from jailbee.git import GitError
    from jailbee.submodules import GitlinkResolution
    from jailbee.sync import merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch(
        "jailbee.sync.git.merge_ref",
        side_effect=GitError("CONFLICT (content): foo.py"),
    )
    mocker.patch(
        "jailbee.sync.submodules.resolve_gitlink_conflicts",
        return_value=GitlinkResolution(resolved=[], unresolved=[]),
    )
    mocker.patch("jailbee.sync.submodules._has_unmerged", return_value=True)
    mocker.patch(
        "jailbee.sync.submodules._nongitlink_unmerged_paths",
        return_value=["foo.py"],
    )

    with pytest.raises(sync_mod.MergeConflictError) as exc_info:
        merge_from_container(cfg, incus, "feat-foo")
    assert "foo.py" in exc_info.value.report.nongitlink


# ---- base-branch targeting tests ----------------------------------------


def _fake_fetch(branch: str):
    from jailbee.sync import FetchResult

    return FetchResult(branch=branch, old_oid=None, new_oid="bbb", base_oid="aaa", commits_added=1)


def test_merge_targets_base_branch_when_current(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="dev")
    mocker.patch("jailbee.sync.git.rev_parse", side_effect=["aaa", "bbb"])
    merge_ref = mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    mocker.patch("jailbee.sync.refresh_container_base")
    from jailbee import sync

    result = sync.merge_from_container(cfg, incus, "feat-x")
    assert result.into_branch == "dev"
    merge_ref.assert_called_once()  # merged in place


def test_merge_ff_without_checkout_when_base_not_current(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="other")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="bbb")
    ff = mocker.patch("jailbee.sync.git.fast_forward_branch", return_value=True)
    checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.refresh_container_base")
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    from jailbee import sync

    result = sync.merge_from_container(cfg, incus, "feat-x")
    ff.assert_called_once_with(cfg.repo_root, "dev", "refs/jailbee/feat-x/feat/x")
    assert result.into_branch == "dev"
    checkout.assert_not_called()
    upd.assert_not_called()


def test_merge_non_ff_without_checkout_raises(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="other")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="bbb")
    mocker.patch("jailbee.sync.git.fast_forward_branch", return_value=False)
    from jailbee import sync

    with pytest.raises(sync.SyncError, match="diverged"):
        sync.merge_from_container(cfg, incus, "feat-x")


def test_merge_checkout_path_refuses_dirty_tree(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="other")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="bbb")
    mocker.patch("jailbee.sync.git.fast_forward_branch", return_value=False)
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=True)
    from jailbee import sync

    with pytest.raises(sync.SyncError, match="dirty"):
        sync.merge_from_container(cfg, incus, "feat-x", allow_checkout=True)


def test_merge_checkout_path_merges_and_stays(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="other")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="ccc")
    mocker.patch("jailbee.sync.git.fast_forward_branch", return_value=False)
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=False)
    checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    merge_ref = mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    mocker.patch("jailbee.sync.refresh_container_base")
    from jailbee import sync

    result = sync.merge_from_container(cfg, incus, "feat-x", allow_checkout=True)
    # checked out the target exactly once — no restore to 'other'
    checkout.assert_called_once_with(cfg.repo_root, "dev")
    merge_ref.assert_called_once()
    assert result.into_branch == "dev"


def test_merge_ff_with_checkout_checks_out_target(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="other")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="bbb")
    mocker.patch("jailbee.sync.git.fast_forward_branch", return_value=True)
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=False)
    checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    mocker.patch("jailbee.sync.refresh_container_base")
    from jailbee import sync

    result = sync.merge_from_container(cfg, incus, "feat-x", allow_checkout=True)
    checkout.assert_called_once_with(cfg.repo_root, "dev")
    upd.assert_called_once_with(cfg.repo_root, branch="dev")
    assert result.into_branch == "dev"


def test_merge_ff_with_checkout_refuses_dirty_tree(mocker, make_cfg, tmp_path):
    """With --checkout, a dirty tree is refused BEFORE the ref moves."""
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="other")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="bbb")
    ff = mocker.patch("jailbee.sync.git.fast_forward_branch", return_value=True)
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=True)
    from jailbee import sync

    with pytest.raises(sync.SyncError, match="dirty"):
        sync.merge_from_container(cfg, incus, "feat-x", allow_checkout=True)
    ff.assert_not_called()


def test_merge_checkout_from_detached_head_allowed(mocker, make_cfg, tmp_path):
    """Detached HEAD no longer refuses --checkout — there is nothing to restore."""
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value=None)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="ccc")
    mocker.patch("jailbee.sync.git.fast_forward_branch", return_value=False)
    mocker.patch("jailbee.sync.git.host_tree_dirty", return_value=False)
    checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    mocker.patch("jailbee.sync.refresh_container_base")
    from jailbee import sync

    result = sync.merge_from_container(cfg, incus, "feat-x", allow_checkout=True)
    checkout.assert_called_once_with(cfg.repo_root, "dev")
    assert result.into_branch == "dev"


def test_pull_refreshes_base_when_target_is_base(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="dev")
    mocker.patch("jailbee.sync.git.rev_parse", side_effect=["aaa", "bbb"])
    mocker.patch("jailbee.sync.git.merge_ref")
    refresh = mocker.patch("jailbee.sync.refresh_container_base", return_value=True)

    sync.merge_from_container(cfg, incus, "feat-x")

    refresh.assert_called_once_with(cfg, incus, "p-feat-x", base_branch="dev")


def test_pull_skips_refresh_when_into_differs_from_base(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    # current == "staging" == into target; container base is "dev" -> no refresh.
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="staging")
    mocker.patch("jailbee.sync.git.rev_parse", side_effect=["aaa", "bbb"])
    mocker.patch("jailbee.sync.git.merge_ref")
    refresh = mocker.patch("jailbee.sync.refresh_container_base")

    sync.merge_from_container(cfg, incus, "feat-x", into="staging")

    refresh.assert_not_called()


# ---- mount-mode guard ---------------------------------------------------


def _mock_mount_mode(incus, name):
    """Make incus.list_containers and config_get behave as a mount-mode container."""
    incus.list_containers.return_value = [{"name": name, "status": "Running", "profiles": []}]

    def fake_config_get(target, key):
        if key == "user.jailbee.mode":
            return "mount"
        return None

    incus.config_get.side_effect = fake_config_get


def test_fetch_from_container_errors_on_mount_mode(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, fetch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_mount_mode(incus, full)

    with pytest.raises(SyncError, match="mount mode"):
        fetch_from_container(cfg, incus, "feat-foo")


def test_checkout_from_container_errors_on_mount_mode(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_mount_mode(incus, full)

    with pytest.raises(SyncError, match="mount mode"):
        checkout_from_container(cfg, incus, "feat-foo")


def test_merge_from_container_errors_on_mount_mode(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_mount_mode(incus, full)

    with pytest.raises(SyncError, match="mount mode"):
        merge_from_container(cfg, incus, "feat-foo")


# ----------------------------------------------------------------------
# gie git push tests (host -> container)
# ----------------------------------------------------------------------


def test_push_to_container_happy_path_local_branch(mocker, make_cfg, tmp_path):
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None  # not mount mode
    incus.exec.return_value = ""  # no prior refs/jailbee/host/main inside container

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    # No origin ref: the local branch is the only candidate.
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.fetch_remote_ref")
    mocker.patch(
        "jailbee.sync.git.rev_parse",
        side_effect=lambda root, ref: "host-oid" if ref == "refs/heads/main" else None,
    )
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    result = push_to_container(cfg, incus, "feat-foo")

    assert result.source == "main"
    assert result.source_ref == "refs/heads/main"
    assert result.container_ref == "refs/jailbee/host/main"
    assert result.old_oid is None
    assert result.new_oid == "host-oid"

    mock_push.assert_called_once()
    args = mock_push.call_args.args
    assert args[0] == cfg.repo_root
    assert args[1].startswith("ext::incus exec --user ")
    assert "git receive-pack /home/dev/repo" in args[1]
    assert args[2] == "+refs/heads/main:refs/jailbee/host/main"


def test_push_to_container_uses_origin_when_local_absent(mocker, make_cfg, tmp_path):
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    incus.exec.return_value = ""

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.sync.git.fetch_remote_ref")
    mocker.patch("jailbee.sync.git.run_capture", return_value=(True, "0\n"))
    mocker.patch(
        "jailbee.sync.git.rev_parse",
        side_effect=lambda root, ref: "origin-oid" if "origin" in ref else None,
    )
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    result = push_to_container(cfg, incus, "feat-foo")

    assert result.source_ref == "refs/remotes/origin/main"
    assert result.new_oid == "origin-oid"
    assert mock_push.call_args.args[2] == "+refs/remotes/origin/main:refs/jailbee/host/main"


def test_push_to_container_explicit_from_overrides_default(mocker, make_cfg, tmp_path):
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    incus.exec.return_value = ""

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mock_detect = mocker.patch("jailbee.sync.git.detect_default_branch")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.fetch_remote_ref")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="oid")
    mocker.patch("jailbee.sync.git.push_url")

    result = push_to_container(cfg, incus, "feat-foo", source="dev")

    assert result.source == "dev"
    mock_detect.assert_not_called()


def test_push_to_container_pushes_an_explicit_source_ref(mocker, make_cfg, tmp_path):
    """`source_ref` bypasses host branch resolution entirely.

    A PR head lives in `refs/jailbee/pr/<N>/head` (never in a branch — see
    `pr.pr_head_ref`), so neither `refs/heads/<head>` nor
    `refs/remotes/origin/<head>` may be consulted or fetched.
    """
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    incus.exec.return_value = ""

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    local = mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    remote = mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    fetch = mocker.patch("jailbee.sync.git.fetch_remote_ref")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="pr-oid")
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    result = push_to_container(
        cfg,
        incus,
        "feat-foo",
        source="feat/pr-branch",
        source_ref="refs/jailbee/pr/1234/head",
    )

    assert result.source == "feat/pr-branch"
    assert result.source_ref == "refs/jailbee/pr/1234/head"
    assert result.container_ref == "refs/jailbee/host/feat/pr-branch"
    assert result.new_oid == "pr-oid"
    assert result.fetched is False
    assert result.local_only_commits == 0
    assert (
        mock_push.call_args.args[2] == "+refs/jailbee/pr/1234/head:refs/jailbee/host/feat/pr-branch"
    )
    fetch.assert_not_called()
    local.assert_not_called()
    remote.assert_not_called()


def test_push_to_container_explicit_source_ref_missing_raises(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    incus.exec.return_value = ""

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.push_url")

    with pytest.raises(SyncError, match="refs/jailbee/pr/1234/head"):
        push_to_container(
            cfg,
            incus,
            "feat-foo",
            source="feat/pr-branch",
            source_ref="refs/jailbee/pr/1234/head",
        )


def test_push_to_container_stopped_raises(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_stopped(incus, full)
    incus.config_get.return_value = None

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    with pytest.raises(SyncError, match="not running"):
        push_to_container(cfg, incus, "feat-foo")


def test_push_to_container_mount_mode_raises(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    incus.config_get.return_value = "mount"

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    with pytest.raises(SyncError, match="mount mode"):
        push_to_container(cfg, incus, "feat-foo")


def test_push_to_container_missing_source_branch_raises(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.fetch_remote_ref")

    with pytest.raises(SyncError, match="does not exist on host"):
        push_to_container(cfg, incus, "feat-foo")


def test_push_to_container_records_prior_oid(mocker, make_cfg, tmp_path):
    """If container already has refs/jailbee/host/<source>, capture it as old_oid."""
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    incus.exec.return_value = "deadbeef\n"  # prior gie/host/main OID

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.fetch_remote_ref")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newoid")
    mocker.patch("jailbee.sync.git.push_url")

    result = push_to_container(cfg, incus, "feat-foo")
    assert result.old_oid == "deadbeef"
    assert result.new_oid == "newoid"


def test_push_to_container_refreshes_base_when_source_is_base(mocker, make_cfg, tmp_path):
    """Local-ref mode: both refspecs come from refs/heads/<base>.

    The origin-ref counterpart is
    `test_push_base_anchor_uses_origin_ref`.
    """
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    # mode None (not mount) AND base_branch == "main".
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "main"}.get(k)
    incus.exec.return_value = ""

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch(
        "jailbee.sync.git.rev_parse",
        side_effect=lambda root, ref: "host-oid" if ref == "refs/heads/main" else None,
    )
    mock_push_multi = mocker.patch("jailbee.sync.git.push_url_multi")
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    push_to_container(cfg, incus, "feat-foo", prefer_ref="local")

    mock_push.assert_not_called()
    mock_push_multi.assert_called_once()
    refspecs = mock_push_multi.call_args.args[2]
    assert refspecs == [
        "+refs/heads/main:refs/jailbee/host/main",
        "+refs/heads/main:refs/jailbee/base/main",
    ]


def test_push_to_container_no_base_refspec_when_source_not_base(mocker, make_cfg, tmp_path):
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "main"}.get(k)
    incus.exec.return_value = ""

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch(
        "jailbee.sync.git.rev_parse",
        side_effect=lambda root, ref: "host-oid" if ref == "refs/heads/dev" else None,
    )
    mock_push_multi = mocker.patch("jailbee.sync.git.push_url_multi")
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    # source "dev" != base "main" -> single push_url, no base refspec.
    push_to_container(cfg, incus, "feat-foo", source="dev", prefer_ref="local")

    mock_push_multi.assert_not_called()
    mock_push.assert_called_once()
    assert mock_push.call_args.args[2] == "+refs/heads/dev:refs/jailbee/host/dev"


def test_push_to_container_uses_the_from_namespace(mocker, make_cfg, tmp_path):
    """A non-host `namespace` relays a source ref into its own container-side path.

    Per Ruling R19: the container-side ref carries a `from/` prefix
    (`refs/jailbee/from/<short>/<branch>`), not the bare container short name —
    a container literally named "host" or "base" must not collide with the
    `refs/jailbee/host/*` / `refs/jailbee/base/*` namespaces.
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-target"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    mocker.patch("jailbee.sync._container_ref_oid", return_value=None)
    mocker.patch("jailbee.sync.ff_container_branch", return_value=None)
    push = mocker.patch("jailbee.sync.git.push_url")

    result = sync.push_to_container(
        cfg,
        incus,
        "target",
        source="feat/a",
        source_ref="refs/jailbee/c1/feat/a",
        namespace="from/c1",
    )

    assert result.container_ref == "refs/jailbee/from/c1/feat/a"
    assert push.call_args[0][2] == "+refs/jailbee/c1/feat/a:refs/jailbee/from/c1/feat/a"


def test_push_to_container_does_not_advance_base_across_containers(mocker, make_cfg, tmp_path):
    """A relayed branch that happens to share the target's base name must not
    re-anchor `refs/jailbee/base/<base>` — that would silently change what
    `jailbee ls`'s AHEAD column measures against. Discriminates the
    `namespace == "host"` guard: without it, this exact scenario (relayed
    source name == target's base branch) would hit the `push_url_multi` arm,
    since `base_branch is not None and resolved_source == base_branch` is
    True here regardless of namespace.
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-target"
    _mock_container_running(incus, full)
    # The target's base branch has the same NAME as the source's branch.
    incus.config_get.side_effect = lambda name, key: (
        "main" if key == "user.jailbee.base_branch" else None
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    mocker.patch("jailbee.sync._container_ref_oid", return_value=None)
    mocker.patch("jailbee.sync.ff_container_branch", return_value=None)
    push_multi = mocker.patch("jailbee.sync.git.push_url_multi")
    push = mocker.patch("jailbee.sync.git.push_url")

    sync.push_to_container(
        cfg,
        incus,
        "target",
        source="main",
        source_ref="refs/jailbee/c1/main",
        namespace="from/c1",
    )

    push_multi.assert_not_called()
    push.assert_called_once()


# ----------------------------------------------------------------------
# Fast-forwarding the container's own refs/heads/<source>
#
# The transport writes only the refs/jailbee/* namespace, so a container's
# local `dev` stayed at whatever the clone had — making an in-container
# `git rebase dev` silently use a stale base. `ff_container_branch` advances
# it, strictly fast-forward and best-effort.
# ----------------------------------------------------------------------

_FAILED = object()
"""Sentinel for `_container_git_stub`: this git subcommand exits non-zero."""


def _container_git_stub(outputs):
    """Build an `incus.exec` side_effect dispatching on the git subcommand.

    Keys are the first git argument after `git -C <dir>` (e.g. "rev-parse").
    A string value is that command's stdout; `_FAILED` makes it exit
    non-zero, which the real `Incus.exec` surfaces as `IncusError`.
    Unlisted subcommands return empty stdout.
    """
    from jailbee.incus import IncusError

    def side_effect(container, cmd, **kwargs):
        sub = cmd[3] if len(cmd) > 3 else ""
        out = outputs.get(sub, "")
        if out is _FAILED:
            raise IncusError(f"git {sub} exited non-zero")
        return out

    return side_effect


def _git_calls(incus_mock):
    """The git argv of every incus.exec call, minus the leading `git -C <dir>`."""
    return [call.args[1][3:] for call in incus_mock.exec.call_args_list]


def test_ff_container_branch_creates_the_branch_when_absent(mocker):
    """A clone of a host whose HEAD was `main` has no local `dev` at all."""
    from jailbee.sync import ff_container_branch

    incus = mocker.MagicMock()
    # `rev-parse --verify --quiet` exits non-zero on a ref that doesn't resolve.
    incus.exec.side_effect = _container_git_stub(
        {"symbolic-ref": "feat/foo\n", "rev-parse": _FAILED}
    )

    result = ff_container_branch(
        incus, "p-feat-foo", "/home/dev/repo", branch="dev", new_oid="new1", uid=1000
    )

    assert result.status == "created"
    assert result.old_oid is None
    assert result.new_oid == "new1"
    assert ["update-ref", "refs/heads/dev", "new1"] in _git_calls(incus)


def test_ff_container_branch_fast_forwards_with_a_compare_and_swap(mocker):
    """The 3-arg update-ref form: a concurrent container-side commit can't be clobbered."""
    from jailbee.sync import ff_container_branch

    incus = mocker.MagicMock()
    incus.exec.side_effect = _container_git_stub(
        {"symbolic-ref": "feat/foo\n", "rev-parse": "old1\n"}
    )

    result = ff_container_branch(
        incus, "p-feat-foo", "/home/dev/repo", branch="dev", new_oid="new1", uid=1000
    )

    assert result.status == "fast-forwarded"
    assert result.old_oid == "old1"
    assert ["merge-base", "--is-ancestor", "old1", "new1"] in _git_calls(incus)
    assert ["update-ref", "refs/heads/dev", "new1", "old1"] in _git_calls(incus)


def test_ff_container_branch_reports_up_to_date_without_writing(mocker):
    from jailbee.sync import ff_container_branch

    incus = mocker.MagicMock()
    incus.exec.side_effect = _container_git_stub(
        {"symbolic-ref": "feat/foo\n", "rev-parse": "new1\n"}
    )

    result = ff_container_branch(
        incus, "p-feat-foo", "/home/dev/repo", branch="dev", new_oid="new1", uid=1000
    )

    assert result.status == "up-to-date"
    assert not [c for c in _git_calls(incus) if c[0] == "update-ref"]


def test_ff_container_branch_skips_the_checked_out_branch(mocker):
    """`receive.denyCurrentBranch` aside, moving HEAD's branch would desync the worktree.

    This is the dev-basella-oleva-dev-kontti case and the `jb push --pr` case
    (a PR container's branch *is* the head ref, cli.py:1016). `--merge`
    already handles both with its --ff-only merge.
    """
    from jailbee.sync import ff_container_branch

    incus = mocker.MagicMock()
    incus.exec.side_effect = _container_git_stub({"symbolic-ref": "dev\n"})

    result = ff_container_branch(
        incus, "p-dev", "/home/dev/repo", branch="dev", new_oid="new1", uid=1000
    )

    assert result.status == "checked-out"
    assert _git_calls(incus) == [["symbolic-ref", "--quiet", "--short", "HEAD"]]


def test_ff_container_branch_refuses_to_rewind_a_diverged_branch(mocker):
    from jailbee.sync import ff_container_branch

    incus = mocker.MagicMock()
    incus.exec.side_effect = _container_git_stub(
        {"symbolic-ref": "feat/foo\n", "rev-parse": "old1\n", "merge-base": _FAILED}
    )

    result = ff_container_branch(
        incus, "p-feat-foo", "/home/dev/repo", branch="dev", new_oid="new1", uid=1000
    )

    assert result.status == "diverged"
    assert result.old_oid == "old1"
    assert not [c for c in _git_calls(incus) if c[0] == "update-ref"]


def test_ff_container_branch_reports_a_failed_update_ref(mocker):
    """A lost CAS race (someone committed on dev mid-push) must not read as success."""
    from jailbee.sync import ff_container_branch

    incus = mocker.MagicMock()
    incus.exec.side_effect = _container_git_stub(
        {"symbolic-ref": "feat/foo\n", "rev-parse": "old1\n", "update-ref": _FAILED}
    )

    result = ff_container_branch(
        incus, "p-feat-foo", "/home/dev/repo", branch="dev", new_oid="new1", uid=1000
    )

    assert result.status == "failed"


def test_ff_container_branch_survives_a_dead_container(mocker):
    """Never raises: a refresh problem must not fail the surrounding push."""
    from jailbee.incus import IncusError
    from jailbee.sync import ff_container_branch

    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("container is not running")

    result = ff_container_branch(
        incus, "p-feat-foo", "/home/dev/repo", branch="dev", new_oid="new1", uid=1000
    )

    assert result.status == "failed"


def test_push_to_container_fast_forwards_the_pushed_local_branch(mocker, make_cfg, tmp_path):
    """Wiring: the transport reports what happened to the container's own branch."""
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    def container_git(container, cmd, **kwargs):
        args = cmd[3:]
        if args[0] == "symbolic-ref":
            return "feat/foo\n"
        if args[0] == "rev-parse":
            return {
                "refs/jailbee/host/main": "prior-host-oid\n",
                "refs/heads/main": "container-main\n",
            }.get(args[3], "")
        return ""

    incus.exec.side_effect = container_git

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.fetch_remote_ref")
    mocker.patch(
        "jailbee.sync.git.rev_parse",
        side_effect=lambda root, ref: "host-oid" if ref == "refs/heads/main" else None,
    )
    mocker.patch("jailbee.sync.git.push_url")

    result = push_to_container(cfg, incus, "feat-foo")

    assert result.local_branch is not None
    assert result.local_branch.branch == "main"
    assert result.local_branch.status == "fast-forwarded"
    assert result.local_branch.old_oid == "container-main"
    assert result.local_branch.new_oid == "host-oid"


def test_push_to_container_survives_a_failed_local_branch_update(mocker, make_cfg, tmp_path):
    """The ref bookkeeping is best-effort — a failure must not fail the push."""
    from jailbee.incus import IncusError
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    def container_git(container, cmd, **kwargs):
        if cmd[3] == "update-ref":
            raise IncusError("update-ref: cannot lock ref")
        return ""

    incus.exec.side_effect = container_git

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.fetch_remote_ref")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="host-oid")
    mocker.patch("jailbee.sync.git.push_url")

    result = push_to_container(cfg, incus, "feat-foo")

    assert result.new_oid == "host-oid"
    assert result.local_branch is not None
    assert result.local_branch.status == "failed"


# ----------------------------------------------------------------------
# Source-ref preference: origin/<source> vs refs/heads/<source>
#
# A host `refs/heads/<base>` only moves on `git pull`; `git fetch` updates
# `refs/remotes/origin/<base>` alone. Pushing the local ref therefore sends
# a stale base into the container — and, when source == base_branch, force-
# moves `refs/jailbee/base/<base>` *backwards*, corrupting `gie ls` AHEAD counts.
# ----------------------------------------------------------------------


def _stub_push_env(mocker, cfg, incus, *, base_branch=None, local=True, origin=True):
    """Stub every host/container touchpoint of `push_to_container`.

    `local` / `origin` toggle which host refs exist. `rev_parse` returns a
    per-ref sentinel oid so tests can tell which ref was actually pushed.
    Returns the mocks the assertions need.
    """
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.side_effect = lambda n, k: (
        base_branch if k == "user.jailbee.base_branch" else None
    )
    incus.exec.return_value = ""
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=local)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=origin)
    mocker.patch("jailbee.sync.git.rev_parse", side_effect=lambda root, ref: f"oid:{ref}")
    return SimpleNamespace(
        full=full,
        run_capture=mocker.patch("jailbee.sync.git.run_capture", return_value=(True, "0\n")),
        fetch=mocker.patch("jailbee.sync.git.fetch_remote_ref"),
        push=mocker.patch("jailbee.sync.git.push_url"),
        push_multi=mocker.patch("jailbee.sync.git.push_url_multi"),
    )


def test_push_prefers_origin_over_local_branch(mocker, make_cfg, tmp_path):
    """Both refs exist → the remote-tracking ref wins (the freshly fetched one)."""
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus, local=True, origin=True)

    result = push_to_container(cfg, incus, "feat-foo")

    assert result.source_ref == "refs/remotes/origin/main"
    assert result.new_oid == "oid:refs/remotes/origin/main"
    assert m.push.call_args.args[2] == "+refs/remotes/origin/main:refs/jailbee/host/main"


def test_push_autofetches_origin_before_resolving(mocker, make_cfg, tmp_path):
    """The host fetch runs first, so `gie push` needs no manual `git fetch`."""
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus)

    result = push_to_container(cfg, incus, "feat-foo")

    m.fetch.assert_called_once_with(cfg.repo_root, "origin", "main")
    assert result.fetched is True
    assert result.fetch_error is None


def test_push_skips_fetch_when_fetch_false(mocker, make_cfg, tmp_path):
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus)

    result = push_to_container(cfg, incus, "feat-foo", fetch=False)

    m.fetch.assert_not_called()
    assert result.fetched is False
    assert result.source_ref == "refs/remotes/origin/main"


def test_push_skips_fetch_when_config_autofetch_false(mocker, make_cfg, tmp_path):
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path).model_copy(
        update={"push": make_cfg(tmp_path).push.model_copy(update={"autofetch": False})}
    )
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus)

    push_to_container(cfg, incus, "feat-foo")

    m.fetch.assert_not_called()


def test_push_local_pref_uses_heads_and_skips_fetch(mocker, make_cfg, tmp_path):
    """`prefer_ref='local'` restores the old behaviour and never fetches."""
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus, local=True, origin=True)

    result = push_to_container(cfg, incus, "feat-foo", prefer_ref="local")

    m.fetch.assert_not_called()
    assert result.source_ref == "refs/heads/main"
    assert m.push.call_args.args[2] == "+refs/heads/main:refs/jailbee/host/main"


def test_push_config_push_from_local_honoured(mocker, make_cfg, tmp_path):
    from jailbee.sync import push_to_container

    base = make_cfg(tmp_path)
    cfg = base.model_copy(update={"push": base.push.model_copy(update={"push_from": "local"})})
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus)

    result = push_to_container(cfg, incus, "feat-foo")

    assert result.source_ref == "refs/heads/main"
    m.fetch.assert_not_called()


def test_push_origin_pref_falls_back_to_local_when_origin_missing(mocker, make_cfg, tmp_path):
    """An unpushed local-only branch still pushes — fetch failure and all."""
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus, local=True, origin=False)

    result = push_to_container(cfg, incus, "feat-foo", source="feat/local-only")

    assert result.source_ref == "refs/heads/feat/local-only"
    assert m.push.call_args.args[2] == (
        "+refs/heads/feat/local-only:refs/jailbee/host/feat/local-only"
    )


def test_push_fetch_failure_is_recorded_not_fatal(mocker, make_cfg, tmp_path):
    """Offline host: the fetch is best-effort, the push still goes through."""
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus)
    m.fetch.side_effect = sync.git.GitFetchError("fetch failed", stderr="Could not resolve host")

    result = push_to_container(cfg, incus, "feat-foo")

    assert result.fetched is False
    assert result.fetch_error is not None
    assert "Could not resolve host" in result.fetch_error
    m.push.assert_called_once()


def test_push_counts_local_only_commits_when_pushing_origin_ref(mocker, make_cfg, tmp_path):
    """Local commits that origin lacks are reported, not silently dropped."""
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus, local=True, origin=True)
    m.run_capture.return_value = (True, "3\n")

    result = push_to_container(cfg, incus, "feat-foo")

    assert result.local_only_commits == 3
    assert m.run_capture.call_args.args[1] == [
        "rev-list",
        "--count",
        "refs/remotes/origin/main..refs/heads/main",
    ]


def test_push_no_local_only_count_when_pushing_local_ref(mocker, make_cfg, tmp_path):
    """Nothing is left behind when the local ref *is* what got pushed."""
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus, local=True, origin=True)
    m.run_capture.return_value = (True, "3\n")

    result = push_to_container(cfg, incus, "feat-foo", prefer_ref="local")

    assert result.local_only_commits == 0
    m.run_capture.assert_not_called()


def test_push_base_anchor_uses_origin_ref(mocker, make_cfg, tmp_path):
    """source == base_branch: the gie base anchor must follow origin, not local.

    A local base behind origin would otherwise force-move
    refs/jailbee/base/<base> backwards and inflate `gie ls` AHEAD.
    """
    from jailbee.sync import push_to_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    m = _stub_push_env(mocker, cfg, incus, base_branch="main")

    push_to_container(cfg, incus, "feat-foo")

    m.push.assert_not_called()
    assert m.push_multi.call_args.args[2] == [
        "+refs/remotes/origin/main:refs/jailbee/host/main",
        "+refs/remotes/origin/main:refs/jailbee/base/main",
    ]


def _exec_dispatcher(responses):
    """Build an incus.exec side_effect from a {key: value-or-callable-or-exc} dict.

    Keys: status, merge_head, rebase_merge, rebase_apply, head_branch,
    rev_parse_gie, rev_parse_local, rev_parse_head, rev_list_count,
    merge_base, update_ref, merge, rebase, reset.

    The rev_parse_local / merge_base / update_ref trio belongs to
    `ff_container_branch`; their defaults ("" = success, empty stdout) make it
    read the container's `refs/heads/<source>` as absent and create it.

    Values may be:
    - a string (returned as stdout),
    - an Exception (raised),
    - a callable (called with no args; its return is treated as a string
      or its raised exception is raised).
    """

    def side_effect(name, cmd, **kwargs):
        joined = " ".join(cmd)
        if "status --porcelain" in joined:
            key = "status"
        elif "MERGE_HEAD" in joined:
            key = "merge_head"
        elif "rebase-merge" in joined:
            key = "rebase_merge"
        elif "rebase-apply" in joined:
            key = "rebase_apply"
        elif "symbolic-ref" in joined:
            key = "head_branch"
        elif "rev-list" in joined:
            key = "rev_list_count"
        elif "update-ref" in cmd:
            key = "update_ref"
        elif "merge-base" in cmd:
            key = "merge_base"
        elif "rev-parse" in joined and "refs/jailbee/host" in joined:
            key = "rev_parse_gie"
        elif "rev-parse" in joined and "refs/heads/" in joined:
            key = "rev_parse_local"
        elif "rev-parse" in joined and "HEAD" in joined:
            key = "rev_parse_head"
        elif "ls-files" in cmd:
            key = "ls_files"
        elif "commit" in cmd:
            key = "commit"
        elif "merge" in cmd:
            key = "merge"
        elif "rebase" in cmd:
            key = "rebase"
        elif "reset" in cmd:
            key = "reset"
        else:
            raise AssertionError(f"unexpected incus.exec call: {cmd}")
        value = responses.get(key, "")
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value

    return side_effect


def _merge_head_preflight_ok_then_conflict():
    """Stateful `merge_head` value: first query (preflight) reports NO merge in
    progress; every later query (after the conflicting merge) reports one.

    `_container_has_merge_in_progress` runs `test -f MERGE_HEAD` — a raised
    IncusError means "not found", a "" return means "found".
    """
    from jailbee.incus import IncusError

    state = {"n": 0}

    def value():
        state["n"] += 1
        if state["n"] == 1:
            raise IncusError("not found")
        return ""

    return value


def _common_push_patches(mocker, cfg, full):
    """Patches used by every push_and_{merge,rebase} test to make push succeed."""
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.detect_default_branch", return_value="main")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="host-oid")
    mocker.patch("jailbee.sync.git.push_url")


def test_push_and_merge_happy_path(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    result = push_and_merge(cfg, incus, "feat-foo")
    assert result.push.source == "main"
    assert result.container_branch == "feat/foo"
    assert result.fast_forward_only is False
    assert result.head_oid == "container-head-oid"

    merge_calls = [
        call
        for call in incus.exec.call_args_list
        if "merge" in call.args[1] and "rev-parse" not in call.args[1]
    ]
    assert len(merge_calls) == 1
    merge_cmd = merge_calls[0].args[1]
    assert "merge" in merge_cmd
    assert "--ff-only" not in merge_cmd
    assert "refs/jailbee/host/main" in merge_cmd


def test_push_and_merge_runs_container_git_as_dev_user(mocker, make_cfg, tmp_path):
    """All container-side git calls during push_and_merge must run as the
    container's dev user.

    `incus exec` defaults to running as root. The clone in the container
    is owned by the dev user, so Git >= 2.35.2 refuses to operate on it
    from root with 'detected dubious ownership in repository at ...'.
    The fix is to pass `uid=cfg.container_user.uid` to incus.exec for
    every git invocation, mirroring what _build_ext_url already does
    for git upload-pack.
    """
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    push_and_merge(cfg, incus, "feat-foo")

    expected_uid = cfg.container_user.uid
    git_calls = [c for c in incus.exec.call_args_list if c.args[1] and c.args[1][0] == "git"]
    assert git_calls, "expected at least one git incus.exec call"
    for c in git_calls:
        assert c.kwargs.get("uid") == expected_uid, (
            f"git command run without uid={expected_uid}: cmd={c.args[1]} kwargs={c.kwargs}"
        )


def test_push_and_merge_sets_home_for_git_merge(mocker, make_cfg, tmp_path):
    """`git merge` inside the container must see HOME=/home/dev so it can
    read the bind-mounted ~/.gitconfig for user.name / user.email.

    `incus exec --user UID` does not derive HOME from /etc/passwd. Without
    HOME, git falls back to a synthesised identity ('dev@<container>.(none)')
    and the merge commit fails with 'Committer identity unknown'.
    """
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    push_and_merge(cfg, incus, "feat-foo")

    merge_calls = [
        call
        for call in incus.exec.call_args_list
        if "merge" in call.args[1] and "rev-parse" not in call.args[1]
    ]
    assert len(merge_calls) == 1
    env = merge_calls[0].kwargs.get("env") or {}
    assert env.get("HOME") == f"/home/{CONTAINER_USERNAME}", (
        f"merge call missing HOME=/home/{CONTAINER_USERNAME}: kwargs={merge_calls[0].kwargs}"
    )


def test_push_and_merge_same_branch_uses_ff_only(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
            "rev_parse_gie": "",
            "merge": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    result = push_and_merge(cfg, incus, "feat-foo")
    assert result.fast_forward_only is True

    merge_calls = [
        call
        for call in incus.exec.call_args_list
        if "merge" in call.args[1] and "rev-parse" not in call.args[1]
    ]
    assert "--ff-only" in merge_calls[0].args[1]


def test_push_and_merge_transports_an_explicit_source_ref(mocker, make_cfg, tmp_path):
    """`--pr --merge` must move the PR head ref, not a same-named host branch."""
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/pr-branch\n",
            "rev_parse_gie": "",
            "merge": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    push_url = mocker.patch("jailbee.sync.git.push_url")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    result = push_and_merge(
        cfg,
        incus,
        "feat-foo",
        source="feat/pr-branch",
        source_ref="refs/jailbee/pr/1234/head",
    )

    assert result.push.source_ref == "refs/jailbee/pr/1234/head"
    assert push_url.call_args.args[2] == (
        "+refs/jailbee/pr/1234/head:refs/jailbee/host/feat/pr-branch"
    )


def test_push_and_merge_forwards_the_tag_policy(mocker, make_cfg, tmp_path):
    """`--merge`'s `tags` must reach the same `push_to_container` refspec-building
    Task 4 gave the `--plain` path — not just be accepted and dropped."""
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    push_multi = mocker.patch("jailbee.sync.git.push_url_multi")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    push_and_merge(cfg, incus, "feat-foo", tags="all")

    assert "refs/tags/*:refs/tags/*" in push_multi.call_args.args[2]


def test_push_and_rebase_transports_an_explicit_source_ref(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_rebase

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/other\n",
            "rev_parse_gie": "",
            "rev_list_count": "2\n",
            "rebase": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    push_url = mocker.patch("jailbee.sync.git.push_url")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    result = push_and_rebase(
        cfg,
        incus,
        "feat-foo",
        source="feat/pr-branch",
        source_ref="refs/jailbee/pr/1234/head",
    )

    assert result.push.source_ref == "refs/jailbee/pr/1234/head"
    assert push_url.call_args.args[2] == (
        "+refs/jailbee/pr/1234/head:refs/jailbee/host/feat/pr-branch"
    )


def test_push_and_rebase_forwards_the_tag_policy(mocker, make_cfg, tmp_path):
    """`--rebase`'s `tags` must reach `push_to_container`'s refspec building."""
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_rebase

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/other\n",
            "rev_parse_gie": "",
            "rev_list_count": "2\n",
            "rebase": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    push_multi = mocker.patch("jailbee.sync.git.push_url_multi")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    push_and_rebase(cfg, incus, "feat-foo", tags="all")

    assert "refs/tags/*:refs/tags/*" in push_multi.call_args.args[2]


def test_push_and_merge_dirty_tree_raises(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": " M foo.py\n",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    with pytest.raises(SyncError, match="dirty"):
        push_and_merge(cfg, incus, "feat-foo")

    mock_push.assert_not_called()


def test_push_and_merge_merge_in_progress_raises(mocker, make_cfg, tmp_path):
    """A conflict with non-gitlink content left unresolved surfaces a SyncError."""
    from jailbee.incus import IncusError
    from jailbee.submodules import GitlinkResolution
    from jailbee.sync import SyncError, push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": _merge_head_preflight_ok_then_conflict(),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": IncusError("conflict"),
        }
    )
    _common_push_patches(mocker, cfg, full)
    mocker.patch(
        "jailbee.sync.submodules.resolve_gitlink_conflicts",
        return_value=GitlinkResolution(resolved=[], unresolved=[]),
    )
    mocker.patch("jailbee.sync.submodules._has_unmerged", return_value=True)
    mocker.patch(
        "jailbee.sync.submodules._nongitlink_unmerged_paths",
        return_value=["README.md"],
    )

    with pytest.raises(SyncError) as excinfo:
        push_and_merge(cfg, incus, "feat-foo")
    assert excinfo.value.report.nongitlink == ["README.md"]


def test_push_and_merge_resolves_gitlinks_and_commits(mocker, make_cfg, tmp_path):
    """A conflict that the resolver clears is finalized with a container-side commit."""
    from jailbee.incus import IncusError
    from jailbee.submodules import GitlinkResolution
    from jailbee.sync import push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": _merge_head_preflight_ok_then_conflict(),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": IncusError("conflict"),
            "commit": "",
            "rev_parse_head": "merged-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch(
        "jailbee.sync.submodules.resolve_gitlink_conflicts",
        return_value=GitlinkResolution(resolved=["lib/foo"], unresolved=[]),
    )
    mocker.patch("jailbee.sync.submodules._has_unmerged", return_value=False)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    result = push_and_merge(cfg, incus, "feat-foo")

    assert result.head_oid == "merged-head-oid"
    commit_calls = [c for c in incus.exec.call_args_list if "commit" in c.args[1]]
    assert len(commit_calls) == 1


def test_push_and_merge_leaves_state_when_unresolved(mocker, make_cfg, tmp_path):
    """A conflict the resolver cannot fully clear raises SyncError, no commit."""
    from jailbee.incus import IncusError
    from jailbee.submodules import GitlinkResolution, UnresolvedSub
    from jailbee.sync import SyncError, push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": _merge_head_preflight_ok_then_conflict(),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": IncusError("conflict"),
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch(
        "jailbee.sync.submodules.resolve_gitlink_conflicts",
        return_value=GitlinkResolution(
            resolved=["lib/foo"],
            unresolved=[UnresolvedSub("vendor/baz", "content-conflict", "CONFLICT (content): x")],
        ),
    )
    mocker.patch("jailbee.sync.submodules._has_unmerged", return_value=True)
    mocker.patch("jailbee.sync.submodules._nongitlink_unmerged_paths", return_value=[])

    with pytest.raises(SyncError) as excinfo:
        push_and_merge(cfg, incus, "feat-foo")

    # Same structured report as the pull path, so the CLI renders one block.
    exc = excinfo.value
    assert isinstance(exc, sync.MergeConflictError)
    assert exc.report.resolution.resolved == ["lib/foo"]
    assert [u.path for u in exc.report.resolution.unresolved] == ["vendor/baz"]
    assert "jailbee shell feat-foo" in exc.report.location

    commit_calls = [c for c in incus.exec.call_args_list if "commit" in c.args[1]]
    assert commit_calls == []


def test_push_and_merge_rebase_in_progress_raises(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": "",
            "head_branch": "feat/foo\n",
        }
    )

    _common_push_patches(mocker, cfg, full)

    with pytest.raises(SyncError, match="rebase in progress"):
        push_and_merge(cfg, incus, "feat-foo")


def test_push_and_merge_detached_head_raises(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": IncusError("HEAD is detached"),
        }
    )

    _common_push_patches(mocker, cfg, full)

    with pytest.raises(SyncError, match="detached HEAD"):
        push_and_merge(cfg, incus, "feat-foo")


def test_push_and_merge_conflict_emits_resolution_hint(mocker, make_cfg, tmp_path):
    """End-to-end through the REAL resolver: a non-gitlink content conflict has
    no gitlink to auto-resolve, so the superproject is left in merge state with a
    summary naming the conflicting file."""
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    # MERGE_HEAD must be absent during preflight and present after the
    # failing merge — flip a flag in the merge action.
    state = {"merge_head_exists": False}

    def merge_head_response():
        if state["merge_head_exists"]:
            return ""
        raise IncusError("not found")

    def merge_action():
        state["merge_head_exists"] = True
        raise IncusError("CONFLICT (content)")

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": merge_head_response,
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": merge_action,
            # only a plain-file conflict remains — no gitlink for the resolver
            "ls_files": "100644 aaaa 2\tREADME.md\n100644 bbbb 3\tREADME.md\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    with pytest.raises(SyncError) as excinfo:
        push_and_merge(cfg, incus, "feat-foo")

    block = sync.render_submodule_report(conflict=excinfo.value.report)
    assert block is not None
    assert "non-submodule conflicts: README.md" in block
    assert "jailbee shell feat-foo" in block


def test_push_and_merge_reports_a_plain_merge_failure(mocker, make_cfg, tmp_path):
    """A `git merge` failure that is neither an index-lock nor a conflict (no
    MERGE_HEAD appears afterwards — e.g. the container ran out of disk) must
    surface as a plain SyncError naming the container, not fall through to the
    gitlink conflict resolver."""
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": IncusError("fatal: unable to write new index file"),
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    with pytest.raises(SyncError) as excinfo:
        push_and_merge(cfg, incus, "feat-foo")

    assert type(excinfo.value) is SyncError, (
        "a merge failure with no MERGE_HEAD is not a conflict — it must not "
        "become a MergeConflictError"
    )
    assert "git merge failed in container 'feat-foo'" in str(excinfo.value)
    assert "unable to write new index file" in str(excinfo.value)


def test_push_and_rebase_happy_path(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_rebase

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "rebase": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    result = push_and_rebase(cfg, incus, "feat-foo")
    assert result.container_branch == "feat/foo"
    assert result.head_oid == "container-head-oid"

    rebase_calls = [
        call
        for call in incus.exec.call_args_list
        if "rebase" in call.args[1] and call.args[1][0] != "test"
    ]
    assert len(rebase_calls) == 1
    assert "refs/jailbee/host/main" in rebase_calls[0].args[1]


def test_push_and_rebase_sets_home_for_git_rebase(mocker, make_cfg, tmp_path):
    """`git rebase` inside the container must see HOME=/home/dev so the
    replayed commits can be authored with the user's identity from the
    bind-mounted ~/.gitconfig. See test_push_and_merge_sets_home_for_git_merge
    for the underlying reason.
    """
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_rebase

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "rebase": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    push_and_rebase(cfg, incus, "feat-foo")

    rebase_calls = [
        call
        for call in incus.exec.call_args_list
        if "rebase" in call.args[1] and call.args[1][0] != "test"
    ]
    assert len(rebase_calls) == 1
    env = rebase_calls[0].kwargs.get("env") or {}
    assert env.get("HOME") == f"/home/{CONTAINER_USERNAME}", (
        f"rebase call missing HOME=/home/{CONTAINER_USERNAME}: kwargs={rebase_calls[0].kwargs}"
    )


def test_push_and_rebase_dirty_tree_raises(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_rebase

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": " M foo.py\n",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    with pytest.raises(SyncError, match="dirty"):
        push_and_rebase(cfg, incus, "feat-foo")

    mock_push.assert_not_called()


def test_push_and_rebase_conflict_emits_rebase_hint(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_rebase

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    state = {"rebase_dir_exists": False}

    def rebase_merge_response():
        if state["rebase_dir_exists"]:
            return ""
        raise IncusError("not found")

    def rebase_action():
        state["rebase_dir_exists"] = True
        raise IncusError("CONFLICT")

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": rebase_merge_response,
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "rebase": rebase_action,
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    with pytest.raises(SyncError, match="Conflict during rebase") as excinfo:
        push_and_rebase(cfg, incus, "feat-foo")
    assert "git rebase --continue" in str(excinfo.value)


# --- push_and_reset ----------------------------------------------------------


def test_push_and_reset_happy_path(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
            "rev_parse_gie": "",
            "rev_parse_head": "old-branch-oid\n",
            "rev_list_count": "0\n",
            "reset": "",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")

    result = push_and_reset(cfg, incus, "feat-foo")
    assert result.push.source == "main"
    assert result.container_branch == "main"
    assert result.head_oid == "old-branch-oid"
    assert result.discarded_commits == 0
    assert result.old_branch_oid == "old-branch-oid"

    reset_calls = [
        call
        for call in incus.exec.call_args_list
        if "reset" in call.args[1] and call.args[1][0] == "git"
    ]
    assert len(reset_calls) == 1
    reset_cmd = reset_calls[0].args[1]
    assert "--hard" in reset_cmd
    assert "refs/jailbee/host/main" in reset_cmd


def test_push_and_reset_forwards_the_tag_policy(mocker, make_cfg, tmp_path):
    """`--force`'s `tags` must reach `push_to_container`'s refspec building."""
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
            "rev_parse_gie": "",
            "rev_parse_head": "old-branch-oid\n",
            "rev_list_count": "0\n",
            "reset": "",
        }
    )

    _common_push_patches(mocker, cfg, full)
    push_multi = mocker.patch("jailbee.sync.git.push_url_multi")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")

    push_and_reset(cfg, incus, "feat-foo", tags="all")

    assert "refs/tags/*:refs/tags/*" in push_multi.call_args.args[2]


def test_push_and_reset_different_branch_refuses(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")

    with pytest.raises(SyncError, match="only replaces the same"):
        push_and_reset(cfg, incus, "feat-foo")

    reset_calls = [
        c for c in incus.exec.call_args_list if "reset" in c.args[1] and c.args[1][0] == "git"
    ]
    assert reset_calls == []


def test_push_and_reset_dirty_tree_refuses(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": " M foo.py\n",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    with pytest.raises(SyncError, match="dirty"):
        push_and_reset(cfg, incus, "feat-foo")

    mock_push.assert_not_called()


def test_push_and_reset_mount_mode_refuses(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = "mount"

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)

    with pytest.raises(SyncError, match="mount mode"):
        push_and_reset(cfg, incus, "feat-foo")


def test_push_and_reset_detached_head_refuses(mocker, make_cfg, tmp_path):
    # Representative of the shared _run_container_preflights path (the
    # in-progress merge/rebase cases are exercised by the push_and_merge /
    # push_and_rebase suites against the same helper). symbolic-ref failing
    # signals detached HEAD.
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": IncusError("detached"),
        }
    )

    _common_push_patches(mocker, cfg, full)
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    with pytest.raises(SyncError, match="detached HEAD"):
        push_and_reset(cfg, incus, "feat-foo")

    mock_push.assert_not_called()


def test_push_and_reset_reports_discarded_commits(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
            "rev_parse_gie": "",
            "rev_parse_head": "doomed-oid\n",
            "rev_list_count": "3\n",
            "reset": "",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")

    result = push_and_reset(cfg, incus, "feat-foo")
    assert result.discarded_commits == 3
    assert result.old_branch_oid == "doomed-oid"

    count_calls = [c for c in incus.exec.call_args_list if "rev-list" in c.args[1]]
    assert len(count_calls) == 1
    assert "refs/jailbee/host/main..doomed-oid" in count_calls[0].args[1]


def test_push_and_reset_syncs_submodules(mocker, make_cfg, tmp_path):
    """push_and_reset must transport submodule objects before the push and
    check out submodule working trees after the reset, exactly like its
    merge/rebase siblings — otherwise a force-reset that moves a submodule
    pointer leaves the container's working tree stale."""
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
            "rev_parse_gie": "",
            "rev_parse_head": "old-branch-oid\n",
            "rev_list_count": "0\n",
            "reset": "",
        }
    )

    _common_push_patches(mocker, cfg, full)
    order = mocker.MagicMock()
    order.attach_mock(
        mocker.patch("jailbee.sync.submodules.transport_submodules_to_container"),
        "transport",
    )
    order.attach_mock(
        mocker.patch("jailbee.sync.submodules.update_submodules_in_container"),
        "update",
    )

    push_and_reset(cfg, incus, "feat-foo")

    # Both run, and transport precedes update (transport-before-push,
    # update-after-reset is guaranteed by code order).
    assert [c[0] for c in order.mock_calls] == ["transport", "update"]


def test_push_and_reset_advances_head(mocker, make_cfg, tmp_path):
    """The two `rev-parse HEAD` reads must observe HEAD moving: the pre-reset
    `old_branch_oid` and the post-reset `head_oid` are distinct when commits
    are discarded."""
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    # First rev-parse HEAD (old tip) -> doomed-oid; second (post-reset) -> the
    # pushed ref's oid.
    state = {"n": 0}

    def head_oids():
        state["n"] += 1
        return "doomed-oid\n" if state["n"] == 1 else "reset-target-oid\n"

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
            "rev_parse_gie": "",
            "rev_parse_head": head_oids,
            "rev_list_count": "2\n",
            "reset": "",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")

    result = push_and_reset(cfg, incus, "feat-foo")
    assert result.old_branch_oid == "doomed-oid"
    assert result.head_oid == "reset-target-oid"
    assert result.head_oid != result.old_branch_oid
    assert result.discarded_commits == 2


def test_push_and_reset_reset_failure_raises(mocker, make_cfg, tmp_path):
    """A failing `git reset --hard` is wrapped as SyncError, not leaked as IncusError."""
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
            "rev_parse_gie": "",
            "rev_parse_head": "old-branch-oid\n",
            "rev_list_count": "0\n",
            "reset": IncusError("fatal: ..."),
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mock_update = mocker.patch("jailbee.sync.submodules.update_submodules_in_container")

    with pytest.raises(SyncError, match="git reset --hard failed"):
        push_and_reset(cfg, incus, "feat-foo")

    # A failed reset must not proceed to the submodule checkout.
    mock_update.assert_not_called()


def test_push_and_reset_empty_rev_list_count_is_zero(mocker, make_cfg, tmp_path):
    """Empty `rev-list --count` output falls back to 0 discarded commits."""
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
            "rev_parse_gie": "",
            "rev_parse_head": "old-branch-oid\n",
            "rev_list_count": "",
            "reset": "",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")

    result = push_and_reset(cfg, incus, "feat-foo")
    assert result.discarded_commits == 0


@pytest.mark.parametrize(
    "count_value",
    [
        pytest.param(None, id="exec-fails"),  # replaced with IncusError below
        "not-a-number\n",
    ],
)
def test_push_and_reset_bad_rev_list_does_not_abort(mocker, make_cfg, tmp_path, count_value):
    """A failing or non-numeric `rev-list --count` must not abort the reset;
    the informational discard count just falls back to 0."""
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_reset

    if count_value is None:
        count_value = IncusError("rev-list blew up")

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
            "rev_parse_gie": "",
            "rev_parse_head": "old-branch-oid\n",
            "rev_list_count": count_value,
            "reset": "",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mock_update = mocker.patch("jailbee.sync.submodules.update_submodules_in_container")

    result = push_and_reset(cfg, incus, "feat-foo")
    assert result.discarded_commits == 0
    # The reset still ran and submodules were checked out.
    reset_calls = [
        c for c in incus.exec.call_args_list if "reset" in c.args[1] and c.args[1][0] == "git"
    ]
    assert len(reset_calls) == 1
    mock_update.assert_called_once()


def test_push_and_reset_not_running_refuses(mocker, make_cfg, tmp_path):
    """A stopped container is refused before any push/reset work."""
    from jailbee.sync import SyncError, push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_stopped(incus, full)
    incus.config_get.return_value = None

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    with pytest.raises(SyncError, match="not running"):
        push_and_reset(cfg, incus, "feat-foo")

    mock_push.assert_not_called()


# --- _should_run_cleanup_step (policy-based) ---------------------------------


def test_should_run_step_runs_when_always(mocker):
    from jailbee.sync import _should_run_cleanup_step

    mock_input = mocker.patch("builtins.input")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)

    result = _should_run_cleanup_step(prompt="x? ", policy="always")

    assert result is True
    mock_input.assert_not_called()


def test_should_run_step_never_skips(mocker):
    from jailbee.sync import _should_run_cleanup_step

    mock_input = mocker.patch("builtins.input")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)

    result = _should_run_cleanup_step(prompt="x? ", policy="never")

    assert result is False
    mock_input.assert_not_called()


def test_should_run_step_prompt_skips_in_non_tty(mocker):
    from jailbee.sync import _should_run_cleanup_step

    mock_input = mocker.patch("builtins.input")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = _should_run_cleanup_step(prompt="x? ", policy="prompt")

    assert result is False
    mock_input.assert_not_called()


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("y", True),
        ("yes", True),
        ("Y", True),
        ("YES", True),
        ("n", False),
        ("", False),
        ("no", False),
        ("maybe", False),
    ],
)
def test_should_run_step_prompt_in_tty_uses_input(mocker, answer, expected):
    from jailbee.sync import _should_run_cleanup_step

    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value=answer)

    assert _should_run_cleanup_step(prompt="x? ", policy="prompt") is expected


def test_cleanup_destroy_only_branch_never(mocker, make_cfg, tmp_path):
    """destroy_policy='always', branch_policy='never' → destroy but keep branch."""
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.is_merged_into", return_value=True)
    mock_delete = mocker.patch("jailbee.sync.git.delete_branch")
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="always",
        branch_policy="never",
    )

    mock_destroy.assert_called_once()
    mock_delete.assert_not_called()
    assert result.destroyed is True
    assert result.deleted_branch is False


def test_cleanup_branch_only_destroy_never(mocker, make_cfg, tmp_path):
    """destroy_policy='never', branch_policy='always' → keep container, delete branch."""
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.is_merged_into", return_value=True)
    mock_delete = mocker.patch("jailbee.sync.git.delete_branch")
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mocker.patch("jailbee.sync._stdin_is_interactive", return_value=False)

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(make_cfg, tmp_path),
        destroy_policy="never",
        branch_policy="always",
    )

    mock_destroy.assert_not_called()
    mock_delete.assert_called_once()
    assert result.destroyed is False
    assert result.deleted_branch is True


def test_cleanup_head_not_moved_overrides_always(mocker, make_cfg, tmp_path):
    """The 'merge did not move HEAD' guard wins over 'always' policies.

    Even with both policies set to always, a no-op merge must skip
    cleanup — destroying the container could lose uncommitted work.
    """
    from jailbee.sync import run_post_merge_cleanup

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mock_destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    mock_delete = mocker.patch("jailbee.sync.git.delete_branch")

    result = run_post_merge_cleanup(
        cfg,
        incus,
        "feat-foo",
        _merge_result(
            make_cfg,
            tmp_path,
            commits_added=0,
            pre_merge_head="f00ba12",
            head_oid="f00ba12",
        ),
        destroy_policy="always",
        branch_policy="always",
    )

    mock_destroy.assert_not_called()
    mock_delete.assert_not_called()
    assert result.skipped_reason is not None
    assert "did not move HEAD" in result.skipped_reason


# ---- diff_from_container ----


def _stub_diff_env(mocker, cfg, full: str, *, mode: str = "clone", running: bool = True):
    """Common setup for diff_from_container tests."""
    incus = mocker.MagicMock()
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    incus.config_get.side_effect = lambda n, k: {
        "user.jailbee.mode": mode,
        "user.jailbee.repo_dir": "/home/dev/repo",
    }.get(k)
    if running:
        _mock_container_running(incus, full)
    else:
        _mock_container_stopped(incus, full)
    return incus


def test_diff_from_container_committed_falls_back_to_origin_default(mocker, make_cfg, tmp_path):
    """When no user.jailbee.base_branch label is set, base falls back to refs/remotes/origin/<default>."""  # noqa: E501
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat"
    incus = _stub_diff_env(mocker, cfg, full)
    # No base_branch label → _stub_diff_env returns None for user.jailbee.base_branch.
    # First exec: _resolves(refs/remotes/origin/main) → truthy; second: diff output.
    incus.exec.side_effect = ["abc1234\n", "diff --git output"]

    out = diff_from_container(cfg, incus, "feat", mode="committed", color=False)

    cmd = incus.exec.call_args_list[-1].args[1]
    assert "diff" in cmd
    assert "refs/remotes/origin/main...HEAD" in cmd
    assert "--color=always" not in cmd
    assert "--stat" not in cmd
    assert out == "diff --git output"


def test_diff_from_container_wt_uses_head_only(mocker, make_cfg, tmp_path):
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat"
    incus = _stub_diff_env(mocker, cfg, full)
    incus.exec.return_value = "wt diff"
    mocker.patch("jailbee.sync.git.rev_parse", return_value="abc")

    diff_from_container(cfg, incus, "feat", mode="wt", color=False)

    cmd = incus.exec.call_args.args[1]
    assert "HEAD" in cmd
    assert "...HEAD" not in " ".join(cmd)


def test_diff_from_container_stat_only_uses_snippet(mocker, make_cfg, tmp_path):
    """stat_only=True routes through the bash grouping snippet (not git diff --stat)."""
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat"
    incus = _stub_diff_env(mocker, cfg, full)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="abc")
    incus.exec.side_effect = ["abc\n", " app.py | 1 +\n"]

    diff_from_container(cfg, incus, "feat", mode="committed", stat_only=True, color=False)
    cmd = incus.exec.call_args_list[-1].args[1]
    # stat_only now uses the bash snippet, not git diff --stat
    assert "bash" in cmd
    assert "--stat" not in cmd


def test_diff_from_container_includes_submodule_diff(mocker, make_cfg, tmp_path):
    """git diff is run with --submodule=diff so submodule content shows inline."""
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat"
    incus = _stub_diff_env(mocker, cfg, full)
    # No base_branch label → base resolves via incus.exec (origin/main check), not git.rev_parse.
    incus.exec.side_effect = ["abc1234\n", "diff output"]

    diff_from_container(cfg, incus, "feat", mode="committed", color=False)

    cmd = incus.exec.call_args_list[-1].args[1]
    assert "--submodule=diff" in cmd


def test_diff_from_container_wt_includes_submodule_diff(mocker, make_cfg, tmp_path):
    """Working-tree diff also runs with --submodule=diff."""
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat"
    incus = _stub_diff_env(mocker, cfg, full)
    incus.exec.return_value = "wt diff"

    diff_from_container(cfg, incus, "feat", mode="wt", color=False)

    cmd = incus.exec.call_args.args[1]
    assert "--submodule=diff" in cmd


def test_diff_from_container_color_adds_color_always(mocker, make_cfg, tmp_path):
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat"
    incus = _stub_diff_env(mocker, cfg, full)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="abc")
    incus.exec.side_effect = ["abc\n", ""]

    diff_from_container(cfg, incus, "feat", mode="committed", color=True)
    cmd = incus.exec.call_args_list[-1].args[1]
    assert "--color=always" in cmd


def test_diff_from_container_rejects_mount_mode(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, diff_from_container

    cfg = make_cfg(tmp_path)
    full = f"{cfg.container_prefix}-mount"
    incus = _stub_diff_env(mocker, cfg, full, mode="mount")

    with pytest.raises(SyncError, match="mount mode"):
        diff_from_container(cfg, incus, "mount", mode="committed")


def test_diff_from_container_rejects_stopped(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, diff_from_container

    cfg = make_cfg(tmp_path)
    full = f"{cfg.container_prefix}-feat"
    incus = _stub_diff_env(mocker, cfg, full, running=False)

    with pytest.raises(SyncError, match="not running"):
        diff_from_container(cfg, incus, "feat", mode="committed")


def test_diff_from_container_all_mode_combines_wt_and_committed(mocker, make_cfg, tmp_path):
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat"
    incus = _stub_diff_env(mocker, cfg, full)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="abc")
    # base-check + committed diff + WT diff
    incus.exec.side_effect = ["abc\n", "COMMITTED_DIFF\n", "WT_DIFF\n"]

    out = diff_from_container(cfg, incus, "feat", mode="all", color=False)

    assert "WT_DIFF" in out
    assert "COMMITTED_DIFF" in out


def test_diff_from_container_no_base_raises(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat"
    incus = _stub_diff_env(mocker, cfg, full)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="abc")
    incus.exec.side_effect = IncusError("nope")

    with pytest.raises(SyncError, match="Cannot resolve base"):
        diff_from_container(cfg, incus, "feat", mode="committed")


def test_diff_committed_uses_base_branch(mocker, make_cfg, tmp_path):
    """mode='committed' resolves base from user.jailbee.base_branch (not host HEAD)."""
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat-x"
    incus = mocker.MagicMock()
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    incus.config_get.side_effect = lambda n, k: {
        "user.jailbee.mode": "clone",
        "user.jailbee.repo_dir": "/home/dev/repo",
        "user.jailbee.base_branch": "dev",
    }.get(k)
    _mock_container_running(incus, full)
    # first exec: _resolves(refs/jailbee/base/dev) → empty (absent); second:
    # _resolves(origin/dev) → succeeds; third: the diff
    incus.exec.side_effect = ["", "abc123\n", "DIFFTEXT"]

    out = diff_from_container(cfg, incus, "feat-x", mode="committed", color=False)

    assert out == "DIFFTEXT"
    diff_cmd = incus.exec.call_args_list[-1].args[1]
    assert any("refs/remotes/origin/dev...HEAD" in part for part in diff_cmd)


def test_diff_prefers_gie_base_ref(mocker, make_cfg, tmp_path):
    """diff_from_container resolves base to refs/jailbee/base/<base> first."""
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat-x"
    incus = mocker.MagicMock()
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    incus.config_get.side_effect = lambda n, k: {
        "user.jailbee.mode": "clone",
        "user.jailbee.repo_dir": "/home/dev/repo",
        "user.jailbee.base_branch": "dev",
    }.get(k)
    _mock_container_running(incus, full)
    # first exec: _resolves(refs/jailbee/base/dev) → succeeds; second exec: the diff
    incus.exec.side_effect = ["abc123\n", "DIFFTEXT"]

    out = diff_from_container(cfg, incus, "feat-x", mode="committed", color=False)

    assert out == "DIFFTEXT"
    diff_cmd = incus.exec.call_args_list[-1].args[1]
    assert any("refs/jailbee/base/dev...HEAD" in part for part in diff_cmd)


def test_diff_stat_uses_grouping_snippet(mocker, make_cfg, tmp_path):
    """stat_only=True for committed mode uses the bash grouping snippet, not git diff --stat."""
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat-x"
    incus = mocker.MagicMock()
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    incus.config_get.side_effect = lambda n, k: {
        "user.jailbee.mode": "clone",
        "user.jailbee.repo_dir": "/repo",
        "user.jailbee.base_branch": "main",
    }.get(k)
    _mock_container_running(incus, full)
    # base resolution probe (rev-parse) resolves the first candidate:
    incus.exec.side_effect = [
        "abc123\n",  # _resolves(refs/jailbee/base/main) -> truthy
        "=== superproject ===\n app.py | 2 +-\n=== deps/libfoo ===\n foo.py | 9 +++\n",
    ]

    out = diff_from_container(cfg, incus, "feat-x", mode="committed", stat_only=True, color=False)

    assert "=== deps/libfoo ===" in out
    # The last exec call must be the bash stat snippet, not a plain `git diff`:
    last_call = incus.exec.call_args_list[-1]
    assert "bash" in last_call.args[1]


def test_diff_stat_passes_mode_committed_without_submodules(mocker, make_cfg, tmp_path):
    """Guard: when snippet returns plain stat output (no === headers), it is returned as-is."""
    from jailbee.sync import diff_from_container

    cfg = make_cfg(tmp_path, default_branch="main")
    full = f"{cfg.container_prefix}-feat-x"
    incus = mocker.MagicMock()
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    incus.config_get.side_effect = lambda n, k: {
        "user.jailbee.mode": "clone",
        "user.jailbee.repo_dir": "/repo",
        "user.jailbee.base_branch": "main",
    }.get(k)
    _mock_container_running(incus, full)
    plain_stat = " app.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n"
    incus.exec.side_effect = [
        "abc123\n",  # _resolves(refs/jailbee/base/main) -> truthy
        plain_stat,  # stat snippet returns plain output (no submodules)
    ]

    out = diff_from_container(cfg, incus, "feat-x", mode="committed", stat_only=True, color=False)

    # Output is returned unchanged — no "=== superproject ===" wrapper injected
    assert out == plain_stat
    assert "=== superproject ===" not in out
    last_call = incus.exec.call_args_list[-1]
    assert "bash" in last_call.args[1]


# ---- Fix-2 regression: pre_merge_head for FF / checkout paths -----------


def test_merge_ff_path_pre_merge_head_is_target_old_tip(mocker, make_cfg, tmp_path):
    """FF path: pre_merge_head must be the target branch's OLD tip, not HEAD.

    When current='other' and target='dev', HEAD points at 'other'. But
    run_post_merge_cleanup compares pre_merge_head with head_oid (the new
    tip of 'dev') to decide whether the merge moved anything — so
    pre_merge_head must be dev's old tip, not HEAD.
    """
    from jailbee import sync
    from jailbee.sync import MergeResult

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.base_branch": "dev"}.get(k)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=_fake_fetch("feat/x"))
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-x")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="other")
    # rev_parse is called once: refs/heads/dev (old tip before FF).
    mocker.patch(
        "jailbee.sync.git.rev_parse",
        side_effect=lambda root, ref: "dev-old-tip" if ref == "refs/heads/dev" else "fetched-tip",
    )
    mocker.patch("jailbee.sync.git.fast_forward_branch", return_value=True)

    result = sync.merge_from_container(cfg, incus, "feat-x")

    assert isinstance(result, MergeResult)
    assert result.pre_merge_head == "dev-old-tip"
    assert result.into_branch == "dev"


def test_merge_from_container_updates_host_submodules(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-x"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=sync.FetchResult(
            branch="feat/x", old_oid=None, new_oid="new", base_oid="old", commits_added=1
        ),
    )
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", side_effect=["pre", "head"])
    mocker.patch("jailbee.sync.git.merge_ref")
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    sync.merge_from_container(cfg, incus, "feat-x")

    upd.assert_called_once_with(cfg.repo_root, branch="main")


# test_checkout_new_branch_updates_host_submodules removed (Minor 7): it was
# an exact duplicate of test_checkout_delegates_to_sync_refs_and_then_switches's
# update_submodules_on_host assertion for the "created" status — the
# new-vs-existing branch split no longer changes this layer's own
# contribution, so one test per status (that one, and
# test_checkout_existing_branch_updates_host_submodules below) covers it.


def test_checkout_existing_branch_updates_host_submodules(mocker, make_cfg, tmp_path):
    """Existing-branch ff path of checkout_from_container calls update_submodules_on_host."""
    from jailbee.sync import checkout_from_container

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch(
        "jailbee.sync.sync_refs_from_container",
        return_value=_synced_result(status="fast-forwarded", old_oid="oldsha"),
    )
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="newsha")
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    checkout_from_container(cfg, incus, "feat-foo")

    upd.assert_called_once_with(cfg.repo_root, branch="feat/foo")


def test_push_and_merge_updates_container_submodules(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-x"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    incus.config_get.return_value = "clone"
    mocker.patch("jailbee.sync._container_is_running", return_value=True)
    mocker.patch("jailbee.sync._run_container_preflights", return_value="feat/x")
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=sync.PushResult(
            source="feat/x",
            source_ref="refs/heads/feat/x",
            container_ref="refs/jailbee/host/feat/x",
            old_oid=None,
            new_oid="new",
        ),
    )
    mocker.patch("jailbee.sync._container_head_oid", return_value="head")
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_in_container")

    sync.push_and_merge(cfg, incus, "feat-x")

    assert upd.call_count == 1
    assert upd.call_args.kwargs["repo_dir"] == "/home/dev/repo"


def test_push_and_rebase_updates_container_submodules(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-x"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    incus.config_get.return_value = "clone"
    mocker.patch("jailbee.sync._container_is_running", return_value=True)
    mocker.patch("jailbee.sync._run_container_preflights", return_value="feat/x")
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=sync.PushResult(
            source="feat/x",
            source_ref="refs/heads/feat/x",
            container_ref="refs/jailbee/host/feat/x",
            old_oid=None,
            new_oid="new",
        ),
    )
    mocker.patch("jailbee.sync._container_head_oid", return_value="head")
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    sync.push_and_rebase(cfg, incus, "feat-x")

    assert upd.call_count == 1
    assert upd.call_args.kwargs["repo_dir"] == "/home/dev/repo"


def test_merge_from_container_transports_submodules_before_update(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-x"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=sync.FetchResult(
            branch="feat/x", old_oid=None, new_oid="new", base_oid="old", commits_added=1
        ),
    )
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", side_effect=["pre", "head"])
    mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    tr = mocker.patch("jailbee.sync.submodules.transport_submodules_to_host")

    sync.merge_from_container(cfg, incus, "feat-x")

    tr.assert_called_once_with(cfg, incus, full, "feat-x", repo_dir="/home/dev/repo")


def test_push_and_merge_transports_submodules_to_container(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-x"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    incus.config_get.return_value = "clone"
    mocker.patch("jailbee.sync._container_is_running", return_value=True)
    mocker.patch("jailbee.sync._run_container_preflights", return_value="feat/x")
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=sync.PushResult(
            source="feat/x",
            source_ref="refs/heads/feat/x",
            container_ref="refs/jailbee/host/feat/x",
            old_oid=None,
            new_oid="new",
        ),
    )
    mocker.patch("jailbee.sync._container_head_oid", return_value="head")
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    tr = mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")

    sync.push_and_merge(cfg, incus, "feat-x")

    tr.assert_called_once_with(cfg, incus, full, repo_dir="/home/dev/repo")


# test_checkout_from_container_transports_submodules removed (Task 7): the
# submodule object transport moved entirely into `sync_refs_from_container`
# (checkout_from_container no longer calls transport_submodules_to_host
# itself), so this is now exactly
# `test_sync_refs_transports_submodule_objects_and_returns_the_whole_picture`'s
# coverage. Re-adding it here would mean not mocking `sync_refs_from_container`
# in a checkout-level test and reproducing that test's setup a second time.


def test_merge_in_place_resolves_gitlinks_and_commits(mocker, make_cfg, tmp_path):
    from jailbee.git import GitError
    from jailbee.submodules import GitlinkResolution
    from jailbee.sync import MergeResult, merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker, head_oid="merged-oid")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    # first merge_ref raises a conflict; resolver clears it; commit finalizes
    mocker.patch("jailbee.sync.git.merge_ref", side_effect=GitError("conflict"))
    resolve = mocker.patch(
        "jailbee.sync.submodules.resolve_gitlink_conflicts",
        return_value=GitlinkResolution(resolved=["lib"], unresolved=[]),
    )
    mocker.patch("jailbee.sync.submodules._has_unmerged", return_value=False)
    commit = mocker.patch("jailbee.sync.git.run_capture", return_value=(True, ""))
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    result = merge_from_container(cfg, incus, "feat-foo")

    assert isinstance(result, MergeResult)
    resolve.assert_called_once()
    # the finalize commit went through run_capture
    assert any("commit" in c.args[1] for c in commit.call_args_list)


def test_merge_in_place_leaves_state_when_unresolved(mocker, make_cfg, tmp_path):
    from jailbee import sync as sync_mod
    from jailbee.git import GitError
    from jailbee.submodules import GitlinkResolution, UnresolvedSub
    from jailbee.sync import merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker, head_oid="merged-oid")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.merge_ref", side_effect=GitError("conflict"))
    mocker.patch(
        "jailbee.sync.submodules.resolve_gitlink_conflicts",
        return_value=GitlinkResolution(
            resolved=[], unresolved=[UnresolvedSub("lib", "content-conflict", "CONFLICT x")]
        ),
    )
    mocker.patch("jailbee.sync.submodules._has_unmerged", return_value=True)
    mocker.patch("jailbee.sync.submodules._nongitlink_unmerged_paths", return_value=[])
    commit = mocker.patch("jailbee.sync.git.run_capture", return_value=(True, ""))

    with pytest.raises(sync_mod.MergeConflictError) as exc_info:
        merge_from_container(cfg, incus, "feat-foo")
    assert exc_info.value.report.resolution.unresolved[0].path == "lib"

    assert not any("commit" in c.args[1] for c in commit.call_args_list)


def test_ff_only_pull_never_invokes_resolver(mocker, make_cfg, tmp_path):
    from jailbee.sync import merge_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker, head_oid="ff-oid")
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.merge_ref")  # ff-only succeeds, no GitError
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    resolve = mocker.patch("jailbee.sync.submodules.resolve_gitlink_conflicts")

    merge_from_container(cfg, incus, "feat-foo", ff="always")

    resolve.assert_not_called()


def test_push_and_rebase_never_invokes_resolver(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_rebase

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("nf"),
            "rebase_merge": IncusError("nf"),
            "rebase_apply": IncusError("nf"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "rebase": "",
            "rev_parse_head": "rebased-oid\n",
        }
    )
    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    resolve = mocker.patch("jailbee.sync.submodules.resolve_gitlink_conflicts")

    push_and_rebase(cfg, incus, "feat-foo")

    resolve.assert_not_called()


def test_refresh_container_base_pushes_expected_refspec(mocker, make_cfg, tmp_path):
    from jailbee.sync import refresh_container_base

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-x"
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="base-oid")
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    ok = refresh_container_base(cfg, incus, full, base_branch="main")

    assert ok is True
    mock_push.assert_called_once()
    args = mock_push.call_args.args
    assert args[0] == cfg.repo_root
    assert "git receive-pack /home/dev/repo" in args[1]
    assert args[2] == "+refs/heads/main:refs/jailbee/base/main"


def test_refresh_container_base_skips_when_host_base_missing(mocker, make_cfg, tmp_path):
    from jailbee.sync import refresh_container_base

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)  # base absent
    mock_push = mocker.patch("jailbee.sync.git.push_url")

    ok = refresh_container_base(cfg, incus, "p-feat-x", base_branch="main")

    assert ok is False
    mock_push.assert_not_called()


def test_refresh_container_base_swallows_push_error(mocker, make_cfg, tmp_path):
    from jailbee import git
    from jailbee.sync import refresh_container_base

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="base-oid")
    mocker.patch("jailbee.sync.git.push_url", side_effect=git.GitError("boom"))

    # Must not raise — refresh is best-effort.
    ok = refresh_container_base(cfg, incus, "p-feat-x", base_branch="main")
    assert ok is False


# ---------------------------------------------------------------------------
# publish_branch_from_container
# ---------------------------------------------------------------------------


def _stub_publish_fetch(mocker, branch: str = "feat/foo"):
    """Patch fetch_from_container to a canned FetchResult."""
    from jailbee.sync import FetchResult

    return mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=FetchResult(
            branch=branch,
            old_oid="abc1234",
            new_oid="def5678",
            base_oid="abc1234",
            commits_added=2,
        ),
    )


def test_publish_happy_path_pushes_gie_ref_to_origin(mocker, make_cfg, tmp_path):
    from jailbee.sync import publish_branch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    incus.exec.return_value = ""  # status --porcelain → clean
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    _stub_publish_fetch(mocker)
    push = mocker.patch("jailbee.sync.git.push_to_remote")

    result = publish_branch_from_container(cfg, incus, "feat-foo")

    assert result.fetch.branch == "feat/foo"
    assert result.fetch.new_oid == "def5678"
    assert result.fetch.commits_added == 2
    assert result.dirty is False
    push.assert_called_once_with(
        cfg.repo_root, "origin", "refs/jailbee/feat-foo/feat/foo", "feat/foo", force_with_lease=None
    )


def test_publish_passes_branch_override_to_fetch(mocker, make_cfg, tmp_path):
    from jailbee.sync import publish_branch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    fetch = _stub_publish_fetch(mocker, branch="feat/other")
    mocker.patch("jailbee.sync.git.push_to_remote")

    publish_branch_from_container(cfg, incus, "feat-foo", branch="feat/other")

    assert fetch.call_args.kwargs["branch"] == "feat/other"


def test_publish_reports_dirty_tree_but_proceeds(mocker, make_cfg, tmp_path):
    from jailbee.sync import publish_branch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = " M src/app.py\n"  # dirty
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    _stub_publish_fetch(mocker)
    push = mocker.patch("jailbee.sync.git.push_to_remote")

    result = publish_branch_from_container(cfg, incus, "feat-foo")

    assert result.dirty is True
    push.assert_called_once()  # dirty does NOT block the publish


def test_publish_wraps_push_failure_in_sync_error(mocker, make_cfg, tmp_path):
    from jailbee.git import GitError
    from jailbee.sync import SyncError, publish_branch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    _stub_publish_fetch(mocker)
    mocker.patch(
        "jailbee.sync.git.push_to_remote",
        side_effect=GitError("git push failed (exit 1)"),
    )

    with pytest.raises(SyncError, match="force-with-lease"):
        publish_branch_from_container(cfg, incus, "feat-foo")


def test_publish_retries_the_push_when_the_user_accepts(mocker, make_cfg, tmp_path):
    """A confirmed retry re-runs only the push — not the container fetch."""
    from jailbee.git import GitError
    from jailbee.sync import publish_branch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    fetch = _stub_publish_fetch(mocker)
    push = mocker.patch(
        "jailbee.sync.git.push_to_remote",
        side_effect=[GitError("git push failed (exit 128)"), None],
    )
    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=True)
    mocker.patch("builtins.input", return_value="y")
    reported = mocker.patch("jailbee.retry.error")

    result = publish_branch_from_container(cfg, incus, "feat-foo")

    assert result.publish_name == "feat/foo"
    assert push.call_count == 2
    fetch.assert_called_once()  # the retry did NOT re-fetch from the container
    reported.assert_not_called()  # quiet variant: git already printed the failure


def test_publish_push_retry_is_not_offered_off_tty(mocker, make_cfg, tmp_path):
    from jailbee.git import GitError
    from jailbee.sync import SyncError, publish_branch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    _stub_publish_fetch(mocker)
    push = mocker.patch(
        "jailbee.sync.git.push_to_remote",
        side_effect=GitError("git push failed (exit 128)"),
    )
    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=False)
    prompt = mocker.patch("builtins.input")

    with pytest.raises(SyncError, match="force-with-lease"):
        publish_branch_from_container(cfg, incus, "feat-foo")

    push.assert_called_once()
    prompt.assert_not_called()


def test_publish_push_failure_hint_has_no_device_specific_wording(mocker, make_cfg, tmp_path):
    from jailbee.git import GitError
    from jailbee.sync import SyncError, publish_branch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    _stub_publish_fetch(mocker)
    mocker.patch(
        "jailbee.sync.git.push_to_remote",
        side_effect=GitError("git push failed (exit 128)"),
    )
    mocker.patch("jailbee.retry._stdin_is_interactive", return_value=False)

    with pytest.raises(SyncError) as excinfo:
        publish_branch_from_container(cfg, incus, "feat-foo")

    message = str(excinfo.value)
    assert "security key" not in message
    assert "touch" not in message
    assert message == message.rstrip()  # no dangling trailing newline


def test_publish_propagates_fetch_preflight_errors(mocker, make_cfg, tmp_path):
    """Mount-mode / stopped / no-clone guards all live in fetch_from_container;
    publish must not swallow them."""
    from jailbee.sync import SyncError, publish_branch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        side_effect=SyncError("container 'feat-foo' is in mount mode — ..."),
    )

    with pytest.raises(SyncError, match="mount mode"):
        publish_branch_from_container(cfg, incus, "feat-foo")


def test_publish_pushes_under_publish_name(mocker, make_cfg, tmp_path):
    from jailbee import sync
    from jailbee.sync import FetchResult

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    fetch = FetchResult(branch="dev-1", old_oid=None, new_oid="n", base_oid=None, commits_added=1)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=fetch)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-dev-1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.sync._container_status_dirty", return_value=False)
    push = mocker.patch("jailbee.git.push_to_remote")

    result = sync.publish_branch_from_container(cfg, incus, "dev-1", publish_name="user/nice")

    push.assert_called_once_with(
        cfg.repo_root, "origin", "refs/jailbee/dev-1/dev-1", "user/nice", force_with_lease=None
    )
    assert result.publish_name == "user/nice"
    assert result.forced is False


def test_publish_force_uses_lease(mocker, make_cfg, tmp_path):
    from jailbee import sync
    from jailbee.sync import FetchResult

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    fetch = FetchResult(branch="dev-1", old_oid=None, new_oid="n", base_oid=None, commits_added=1)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=fetch)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-dev-1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.sync._container_status_dirty", return_value=False)
    mocker.patch("jailbee.git.remote_branch_sha", return_value="oldsha")
    push = mocker.patch("jailbee.git.push_to_remote")

    result = sync.publish_branch_from_container(
        cfg, incus, "dev-1", publish_name="user/nice", force=True
    )

    push.assert_called_once_with(
        cfg.repo_root, "origin", "refs/jailbee/dev-1/dev-1", "user/nice", force_with_lease="oldsha"
    )
    assert result.forced is True


def test_publish_defaults_to_container_branch(mocker, make_cfg, tmp_path):
    from jailbee import sync
    from jailbee.sync import FetchResult

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    fetch = FetchResult(
        branch="feat/foo", old_oid=None, new_oid="n", base_oid=None, commits_added=1
    )
    mocker.patch("jailbee.sync.fetch_from_container", return_value=fetch)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-foo")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.sync._container_status_dirty", return_value=False)
    push = mocker.patch("jailbee.git.push_to_remote")

    result = sync.publish_branch_from_container(cfg, incus, "feat-foo")

    push.assert_called_once_with(
        cfg.repo_root, "origin", "refs/jailbee/feat-foo/feat/foo", "feat/foo", force_with_lease=None
    )
    assert result.publish_name == "feat/foo"


def test_publish_runs_the_hook_before_the_push(mocker, make_cfg, tmp_path):
    """`on_before_push` fires after the fetch and *before* the push.

    That order is the whole point: `git push` inherits its output and prints
    nothing until the remote answers, so the caller's report of the fetch has
    to reach the terminal first — otherwise a push blocked on remote
    authentication is indistinguishable from a hung fetch.
    """
    from jailbee import sync
    from jailbee.sync import FetchResult, PublishResult

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    fetch = FetchResult(
        branch="dev-1", old_oid="old", new_oid="new", base_oid="old", commits_added=1
    )
    mocker.patch("jailbee.sync.fetch_from_container", return_value=fetch)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-dev-1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.sync._container_status_dirty", return_value=True)
    order: list[str] = []
    mocker.patch("jailbee.git.push_to_remote", side_effect=lambda *a, **k: order.append("push"))
    seen: list[PublishResult] = []

    def hook(result: PublishResult) -> None:
        order.append("hook")
        seen.append(result)

    result = sync.publish_branch_from_container(
        cfg, incus, "dev-1", publish_name="user/nice", on_before_push=hook
    )

    assert order == ["hook", "push"]
    assert seen[0] is result  # the same object, already fully resolved
    assert seen[0].publish_name == "user/nice"
    assert seen[0].dirty is True
    assert seen[0].fetch is fetch


def test_publish_runs_the_hook_even_when_the_push_fails(mocker, make_cfg, tmp_path):
    """The fetch report belongs on screen above the push error, not lost with it."""
    from jailbee import sync
    from jailbee.git import GitError

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    _stub_publish_fetch(mocker)
    mocker.patch("jailbee.sync.git.push_to_remote", side_effect=GitError("git push failed"))
    hook = mocker.MagicMock()

    with pytest.raises(sync.SyncError):
        sync.publish_branch_from_container(cfg, incus, "feat-foo", on_before_push=hook)

    hook.assert_called_once()


# ---- retarget ------------------------------------------------------------


def _retarget_setup(mocker, *, old_base="feat/a", mode=None, running=True, bg_op=None):
    """Common mocks for retarget_container tests. Returns the incus mock."""
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {
        "user.jailbee.mode": mode,
        "user.jailbee.base_branch": old_base,
    }.get(k)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-b")
    mocker.patch("jailbee.lifecycle.lookup_background_job", return_value=bg_op)
    mocker.patch("jailbee.sync._container_is_running", return_value=running)
    mocker.patch("jailbee.sync._build_receive_url", return_value="ext::receive")
    return incus


def test_retarget_pushes_new_base_and_deletes_old(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = _retarget_setup(mocker)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="abc1234")
    push_multi = mocker.patch("jailbee.sync.git.push_url_multi")

    result = sync.retarget_container(cfg, incus, "feat-b", "main")

    push_multi.assert_called_once_with(
        cfg.repo_root,
        "ext::receive",
        ["+refs/heads/main:refs/jailbee/base/main", ":refs/jailbee/base/feat/a"],
    )
    incus.config_set.assert_called_once_with("p-feat-b", "user.jailbee.base_branch", "main")
    assert result.old_base == "feat/a"
    assert result.new_base == "main"
    assert result.base_oid == "abc1234"


def test_retarget_without_old_base_pushes_only_new(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = _retarget_setup(mocker, old_base=None)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="abc1234")
    push_multi = mocker.patch("jailbee.sync.git.push_url_multi")

    result = sync.retarget_container(cfg, incus, "feat-b", "main")

    push_multi.assert_called_once_with(
        cfg.repo_root, "ext::receive", ["+refs/heads/main:refs/jailbee/base/main"]
    )
    assert result.old_base is None


def test_retarget_label_not_set_when_push_fails(mocker, make_cfg, tmp_path):
    from jailbee import git, sync

    cfg = make_cfg(tmp_path)
    incus = _retarget_setup(mocker)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="abc1234")
    mocker.patch("jailbee.sync.git.push_url_multi", side_effect=git.GitError("boom"))

    with pytest.raises(git.GitError):
        sync.retarget_container(cfg, incus, "feat-b", "main")
    incus.config_set.assert_not_called()


def test_retarget_refuses_same_base(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = _retarget_setup(mocker, old_base="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="abc1234")

    with pytest.raises(sync.SyncError, match="already targets"):
        sync.retarget_container(cfg, incus, "feat-b", "main")


def test_retarget_refuses_missing_host_branch(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = _retarget_setup(mocker)
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)

    with pytest.raises(sync.SyncError, match="does not exist on host"):
        sync.retarget_container(cfg, incus, "feat-b", "nope")


def test_retarget_refuses_mount_mode(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = _retarget_setup(mocker, mode="mount")

    with pytest.raises(sync.SyncError, match="mount mode"):
        sync.retarget_container(cfg, incus, "feat-b", "main")


def test_retarget_refuses_live_background_job(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    op = mocker.MagicMock()
    op.phase = "cloning"
    op.pid = 4242
    op.op_kind = "create"
    incus = _retarget_setup(mocker, bg_op=op)
    mocker.patch("jailbee.background.worker_alive", return_value=True)

    with pytest.raises(sync.SyncError, match=r"background job \(cloning\)"):
        sync.retarget_container(cfg, incus, "feat-b", "main")


@pytest.mark.parametrize(
    "phase,worker_alive",
    [
        ("failed", True),  # terminal phase: dead regardless of worker
        ("cloning", False),  # non-terminal phase, but the worker died
    ],
)
def test_retarget_ignores_a_dead_background_job_row(
    mocker, make_cfg, tmp_path, phase, worker_alive
):
    """A dead row (the branch's central scenario: autostart failed, the user
    fixed it by hand, kept working) must not block retarget forever."""
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    op = mocker.MagicMock()
    op.phase = phase
    op.pid = 4242
    op.op_kind = "create"
    incus = _retarget_setup(mocker, bg_op=op)
    mocker.patch("jailbee.background.worker_alive", return_value=worker_alive)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="abc1234")
    mocker.patch("jailbee.sync.git.push_url_multi")

    result = sync.retarget_container(cfg, incus, "feat-b", "main")

    assert result.new_base == "main"


def test_retarget_refuses_stopped_container(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = _retarget_setup(mocker, running=False)

    with pytest.raises(sync.SyncError, match="not running"):
        sync.retarget_container(cfg, incus, "feat-b", "main")


def test_retarget_source_ref_anchors_the_new_base_elsewhere(mocker, make_cfg, tmp_path):
    """`jailbee pr --stacked` retargets onto a PR head, which deliberately
    lives in no branch — so the anchor comes from `source_ref`, while the
    label still records the branch name the PR head belongs to."""
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = _retarget_setup(mocker)
    rev_parse = mocker.patch("jailbee.sync.git.rev_parse", return_value="abc1234")
    push_multi = mocker.patch("jailbee.sync.git.push_url_multi")

    result = sync.retarget_container(
        cfg, incus, "feat-b", "feat/x", source_ref="refs/jailbee/pr/1234/head"
    )

    rev_parse.assert_called_once_with(cfg.repo_root, "refs/jailbee/pr/1234/head")
    push_multi.assert_called_once_with(
        cfg.repo_root,
        "ext::receive",
        [
            "+refs/jailbee/pr/1234/head:refs/jailbee/base/feat/x",
            ":refs/jailbee/base/feat/a",
        ],
    )
    incus.config_set.assert_called_once_with("p-feat-b", "user.jailbee.base_branch", "feat/x")
    assert result.new_base == "feat/x"
    assert result.base_oid == "abc1234"


def test_retarget_missing_source_ref_names_the_ref_not_a_branch(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = _retarget_setup(mocker)
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)

    with pytest.raises(sync.SyncError, match=r"refs/jailbee/pr/1234/head.*does not exist"):
        sync.retarget_container(
            cfg, incus, "feat-b", "feat/x", source_ref="refs/jailbee/pr/1234/head"
        )


# ---------------------------------------------------------------------------
# SubmoduleMove + compute_submodule_moves
# ---------------------------------------------------------------------------


def test_compute_submodule_moves_parses_gitlink_diff(mocker, tmp_path):
    cfg_root = tmp_path
    raw = (
        ":160000 160000 1111111111111111111111111111111111111111 "
        "2222222222222222222222222222222222222222 M\tdeps/libfoo\n"
        ":100644 100644 aaaa bbbb M\tapp.py\n"  # non-gitlink — ignored
    )

    def fake_run_capture(cwd, args):
        if args[:2] == ["diff", "--raw"]:
            return True, raw
        if args[:1] == ["rev-list"]:
            return True, "2\n"
        if args[:1] == ["diff"] and "--shortstat" in args:
            return True, " 1 file changed, 42 insertions(+), 7 deletions(-)\n"
        return True, ""

    mocker.patch("jailbee.sync.git.run_capture", side_effect=fake_run_capture)
    moves = sync.compute_submodule_moves(cfg_root, "old" + "0" * 37, "new" + "0" * 37)
    assert moves == [
        sync.SubmoduleMove(
            path="deps/libfoo",
            old_sha="1111111111111111111111111111111111111111",
            new_sha="2222222222222222222222222222222222222222",
            status="modified",
            commits=2,
            ins=42,
            dels=7,
        )
    ]


def test_compute_submodule_moves_empty_when_equal(mocker, tmp_path):
    assert sync.compute_submodule_moves(tmp_path, "abc", "abc") == []
    assert sync.compute_submodule_moves(tmp_path, None, "abc") == []


def test_compute_submodule_moves_new_submodule_commits_zero(mocker, tmp_path):
    """A newly added submodule (old SHA all-zeros) yields commits=0.

    The mock returns a nonzero count for rev-list so the test proves that
    _count is NOT called for the 'new' path (the 0 comes from the code
    path, not from the mock returning 0).
    """
    ns_sha = "e" * 40
    raw = f":000000 160000 {'0' * 40} {ns_sha} A\tvendor/new-sub\n"

    def fake_run_capture(cwd, args):
        if args[:2] == ["diff", "--raw"]:
            return True, raw
        if args[:1] == ["rev-list"]:
            # Return a nonzero count — if _count were called this would reach
            # the SubmoduleMove and the assertion below would fail.
            return True, "9999\n"
        return True, ""

    mocker.patch("jailbee.sync.git.run_capture", side_effect=fake_run_capture)
    moves = sync.compute_submodule_moves(tmp_path, "old" + "0" * 37, "new" + "0" * 37)
    assert len(moves) == 1
    m = moves[0]
    assert m.status == "new"
    assert m.commits == 0
    assert m.old_sha is None
    assert m.new_sha == ns_sha


# ---------------------------------------------------------------------------
# render_submodule_report
# ---------------------------------------------------------------------------


def test_render_submodule_report_success():
    moves = [
        sync.SubmoduleMove("deps/libfoo", "a" * 40, "d" * 40, "modified", 2, 42, 7),
        sync.SubmoduleMove("vendor/bar", None, "e" * 40, "new", 5, 0, 0),
    ]
    out = sync.render_submodule_report(moves=moves)
    assert out is not None
    assert "Submodules" in out
    assert "deps/libfoo" in out
    assert "aaaaaaa..ddddddd" in out
    assert "(2 commits, +42 -7)" in out
    assert "new → eeeeeee" in out


def test_render_submodule_report_empty_is_none():
    assert sync.render_submodule_report(moves=[]) is None
    assert sync.render_submodule_report() is None


# ---------------------------------------------------------------------------
# ConflictReport + MergeConflictError — Task 6
# ---------------------------------------------------------------------------


def test_render_conflict_report_lists_resolved_and_unresolved():
    from jailbee import submodules

    report = sync.ConflictReport(
        resolution=submodules.GitlinkResolution(
            resolved=["deps/libfoo"],
            unresolved=[
                submodules.UnresolvedSub(
                    "vendor/bar", "content-conflict", "CONFLICT (content): foo.c"
                )
            ],
        ),
        nongitlink=["app.py"],
        branch="feat/x",
        location="cd /repo",
    )
    out = sync.render_submodule_report(conflict=report)
    assert out is not None
    assert "✓ deps/libfoo" in out
    assert "auto-merged" in out
    assert "✗ vendor/bar" in out
    assert "CONFLICT (content): foo.c" in out
    assert "app.py" in out
    assert "merge state" in out


def _conflict_report_all_outcomes():
    from jailbee import submodules

    return sync.ConflictReport(
        resolution=submodules.GitlinkResolution(
            resolved=["deps/libfoo", "lib/inner"],
            unresolved=[
                submodules.UnresolvedSub("lib", "nested-conflict", ""),
                submodules.UnresolvedSub(
                    "vendor/baz", "content-conflict", "CONFLICT (content): Merge conflict in x.c"
                ),
                submodules.UnresolvedSub("tools/sdk", "dirty", ""),
                submodules.UnresolvedSub("old/dep", "deleted-side", ""),
            ],
        ),
        nongitlink=["README.md"],
        branch="feat/x",
        location="cd /repo\n# on 'main' in merge state",
    )


def test_render_conflict_report_groups_outcomes_with_counts():
    out = sync.render_submodule_report(conflict=_conflict_report_all_outcomes())
    assert out is not None
    assert "auto-merged (2):" in out
    assert "in merge state — resolve these (2):" in out
    assert "skipped, not touched (2):" in out


def test_render_conflict_report_separates_merge_state_from_skipped():
    """A dirty/one-sided submodule was never touched — it must not be listed
    among the ones awaiting `git add && git commit`."""
    out = sync.render_submodule_report(conflict=_conflict_report_all_outcomes())
    assert out is not None
    in_merge = out.split("in merge state")[1].split("skipped, not touched")[0]
    assert "lib" in in_merge
    assert "vendor/baz" in in_merge
    assert "tools/sdk" not in in_merge
    assert "old/dep" not in in_merge

    skipped = out.split("skipped, not touched")[1]
    assert "tools/sdk" in skipped
    assert "commit or stash" in skipped
    assert "old/dep" in skipped
    assert "one side" in skipped


def test_render_conflict_report_omits_empty_groups():
    from jailbee import submodules

    report = sync.ConflictReport(
        resolution=submodules.GitlinkResolution(resolved=[], unresolved=[]),
        nongitlink=["README.md"],
        branch="feat/x",
        location="cd /repo",
    )
    out = sync.render_submodule_report(conflict=report)
    assert out is not None
    assert "auto-merged" not in out
    assert "in merge state — resolve these" not in out
    assert "skipped, not touched" not in out
    assert "README.md" in out


def test_render_conflict_report_indents_multiline_location():
    out = sync.render_submodule_report(conflict=_conflict_report_all_outcomes())
    assert out is not None
    assert "    cd /repo" in out
    assert "    # on 'main' in merge state" in out


def test_merge_conflict_error_carries_report():
    from jailbee import submodules

    report = sync.ConflictReport(
        resolution=submodules.GitlinkResolution([], []),
        nongitlink=[],
        branch="feat/x",
        location="cd /repo",
    )
    err = sync.MergeConflictError("conflicts", report=report)
    assert isinstance(err, sync.SyncError)
    assert err.report is report


# ---------------------------------------------------------------------------
# _do_single_pull wiring — submodule report printed on success
# ---------------------------------------------------------------------------


def test_do_single_pull_prints_submodule_report(mocker, make_cfg, tmp_path):
    from rich.console import Console

    from jailbee import cli, sync

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    result = sync.MergeResult(
        fetch=mocker.MagicMock(),
        branch="feat/x",
        head_oid="d" * 40,
        into_branch="main",
        pre_merge_head="a" * 40,
    )
    mocker.patch("jailbee.sync.merge_from_container", return_value=result)
    mocker.patch("jailbee.cli._print_fetch_summary")
    mocker.patch(
        "jailbee.sync.run_post_merge_cleanup",
        return_value=sync.CleanupResult(False, False, None, None),
    )
    mocker.patch(
        "jailbee.sync.compute_submodule_moves",
        return_value=[sync.SubmoduleMove("deps/libfoo", "a" * 40, "d" * 40, "modified", 2, 42, 7)],
    )
    recording = Console(record=True)
    mocker.patch("jailbee.tui.console", recording)

    cli._do_single_pull(
        cfg,
        incus,
        "feat-x",
        branch=None,
        ff="never",
        into=None,
        allow_checkout=False,
        destroy_policy="never",
        branch_policy="never",
        tags="reachable",
    )

    out = recording.export_text()
    assert "deps/libfoo" in out


# ---------------------------------------------------------------------------
# _emit_pull_conflict_report — CLI conflict report helper (Task 6)
# ---------------------------------------------------------------------------


def test_pull_prints_conflict_report(mocker):
    from rich.console import Console

    from jailbee import cli, submodules, sync

    report = sync.ConflictReport(
        resolution=submodules.GitlinkResolution(resolved=["deps/libfoo"], unresolved=[]),
        nongitlink=[],
        branch="feat/x",
        location="cd /repo",
    )
    exc = sync.MergeConflictError("conflicts", report=report)
    recording = Console(record=True)
    mocker.patch("jailbee.tui.console", recording)
    cli._emit_conflict_report(exc)
    out = recording.export_text()
    assert "deps/libfoo" in out


def test_emit_conflict_report_ignores_other_errors(mocker):
    from rich.console import Console

    from jailbee import cli, sync

    recording = Console(record=True)
    mocker.patch("jailbee.tui.console", recording)
    cli._emit_conflict_report(sync.SyncError("plain failure"))
    assert recording.export_text().strip() == ""


# ---- submodule anchor re-pin on refresh/retarget --------------------------


def test_refresh_container_base_repins_submodule_anchors(mocker, make_cfg, tmp_path):
    from jailbee.sync import refresh_container_base

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    incus.config_get.return_value = "feat/foo"  # user.jailbee.branch
    mocker.patch("jailbee.sync.git.rev_parse", return_value="oid")
    mocker.patch("jailbee.sync._build_receive_url", return_value="ext::x")
    mocker.patch("jailbee.sync.git.push_url")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    seed = mocker.patch("jailbee.submodules.seed_submodule_base_anchors")

    assert refresh_container_base(cfg, incus, full, base_branch="main") is True

    seed.assert_called_once()
    assert seed.call_args.kwargs["base_branch"] == "main"
    assert seed.call_args.kwargs["container_branch"] == "feat/foo"


def test_retarget_repins_new_and_deletes_old_submodule_anchors(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = _retarget_setup(mocker, old_base="feat/a")
    # _retarget_setup sets user.jailbee.mode and user.jailbee.base_branch only;
    # _refresh_submodule_base_anchors also needs user.jailbee.branch.
    incus.config_get.side_effect = lambda name, key: {
        "user.jailbee.mode": None,
        "user.jailbee.base_branch": "feat/a",
        "user.jailbee.branch": "feat/b",
    }.get(key)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="baseoid")
    mocker.patch("jailbee.sync.git.push_url_multi")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    seed = mocker.patch("jailbee.submodules.seed_submodule_base_anchors")
    delete = mocker.patch("jailbee.submodules.delete_submodule_base_anchors")

    sync.retarget_container(cfg, incus, "feat-b", "main")

    seed.assert_called_once()
    assert seed.call_args.kwargs["base_branch"] == "main"
    delete.assert_called_once()
    assert delete.call_args.args[2] == "feat/a"


# ---- local submodule checkout orchestration -------------------------------


def test_checkout_submodules_on_host_resolves_current_branch(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    mocker.patch(
        "jailbee.sync.submodules.report_submodule_branches",
        return_value=[("lib", "feat/foo")],
    )

    resolved, report = sync.checkout_submodules_on_host(cfg)

    assert resolved == "feat/foo"
    upd.assert_called_once_with(cfg.repo_root, branch="feat/foo")
    assert report == [("lib", "feat/foo")]


def test_checkout_submodules_on_host_detached_without_override_raises(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value=None)

    with pytest.raises(sync.SyncError, match="detached HEAD"):
        sync.checkout_submodules_on_host(cfg)


def test_checkout_submodules_on_host_branch_override_wins(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    gc = mocker.patch("jailbee.sync.git.get_current_branch")
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    mocker.patch("jailbee.sync.submodules.report_submodule_branches", return_value=[])

    resolved, _ = sync.checkout_submodules_on_host(cfg, branch="feat/x")

    assert resolved == "feat/x"
    gc.assert_not_called()
    upd.assert_called_once_with(cfg.repo_root, branch="feat/x")


def test_checkout_submodules_on_host_does_not_switch_by_default(mocker, make_cfg, tmp_path):
    """The superproject stays where it is unless the caller asks for the switch."""
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    co = mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    mocker.patch("jailbee.sync.submodules.report_submodule_branches", return_value=[])

    sync.checkout_submodules_on_host(cfg, branch="feat/x")

    co.assert_not_called()


def test_checkout_submodules_on_host_switches_superproject_before_aligning(
    mocker, make_cfg, tmp_path
):
    """The superproject checkout must land first: it is what rewrites the
    gitlinks that the submodule alignment then checks out."""
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    order: list[str] = []
    co = mocker.patch(
        "jailbee.sync.git.checkout_branch", side_effect=lambda *a, **k: order.append("checkout")
    )
    upd = mocker.patch(
        "jailbee.sync.submodules.update_submodules_on_host",
        side_effect=lambda *a, **k: order.append("align"),
    )
    mocker.patch(
        "jailbee.sync.submodules.report_submodule_branches", return_value=[("lib", "feat/x")]
    )

    resolved, report = sync.checkout_submodules_on_host(
        cfg, branch="feat/x", switch_superproject=True
    )

    assert order == ["checkout", "align"]
    co.assert_called_once_with(cfg.repo_root, "feat/x")
    upd.assert_called_once_with(cfg.repo_root, branch="feat/x")
    assert (resolved, report) == ("feat/x", [("lib", "feat/x")])


def test_checkout_submodules_on_host_switch_resolves_current_branch(mocker, make_cfg, tmp_path):
    """With no override the switch targets the branch already checked out —
    a no-op checkout, but it must not target something else."""
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    co = mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    mocker.patch("jailbee.sync.submodules.report_submodule_branches", return_value=[])

    sync.checkout_submodules_on_host(cfg, switch_superproject=True)

    co.assert_called_once_with(cfg.repo_root, "feat/foo")


def test_checkout_submodules_on_host_switch_failure_skips_alignment(mocker, make_cfg, tmp_path):
    """A refused checkout (dirty tree, unknown branch) must not leave the
    submodules aligned to a branch the superproject is not on."""
    from jailbee import git, sync

    cfg = make_cfg(tmp_path)
    mocker.patch(
        "jailbee.sync.git.checkout_branch",
        side_effect=git.GitError("git checkout failed (exit 1)"),
    )
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    with pytest.raises(sync.SyncError, match="feat/x"):
        sync.checkout_submodules_on_host(cfg, branch="feat/x", switch_superproject=True)

    upd.assert_not_called()


def test_checkout_submodules_in_container_places_and_reports(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {
        "user.jailbee.mode": "clone",
        "user.jailbee.branch": "feat/foo",
    }.get(k)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-foo")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync._container_is_running", return_value=True)
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch(
        "jailbee.sync.submodules.report_submodule_branches",
        return_value=[("lib", "feat/foo")],
    )

    resolved, report = sync.checkout_submodules_in_container(cfg, incus, "feat-foo")

    assert resolved == "feat/foo"
    assert upd.call_args.kwargs["branch"] == "feat/foo"
    assert upd.call_args.kwargs["repo_dir"] == "/home/dev/repo"
    assert report == [("lib", "feat/foo")]


def test_checkout_submodules_in_container_refuses_mount_mode(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.mode": "mount"}.get(k)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-foo")

    with pytest.raises(sync.SyncError, match="mount mode"):
        sync.checkout_submodules_in_container(cfg, incus, "feat-foo")


def test_checkout_submodules_in_container_refuses_stopped(mocker, make_cfg, tmp_path):
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda n, k: {"user.jailbee.mode": "clone"}.get(k)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-feat-foo")
    mocker.patch("jailbee.sync._container_is_running", return_value=False)

    with pytest.raises(sync.SyncError, match="not running"):
        sync.checkout_submodules_in_container(cfg, incus, "feat-foo")


# ---- bridge plans ----


def _wire_plan_container(mocker, incus, cfg, full: str, *, repo_dir: str = "/home/dev/app"):
    """Common plumbing for plan_* tests: name resolution, repo dir, running state."""
    _mock_container_running(incus, full)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value=repo_dir)


def test_plan_push_happy_path(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)

    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="a1b2c3d4" * 5)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Bump deps")
    mocker.patch("jailbee.sync.git.run_capture", return_value=(True, "4\n"))
    mocker.patch("jailbee.sync._count_local_only_commits", return_value=0)

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return "feat/foo\n"
        if cmd[:1] == ["rev-parse"]:
            return "9f8e7d6c" * 5 + "\n"
        if cmd[:1] == ["log"]:
            return "WIP parser\n"
        if cmd[:1] == ["status"]:
            return ""
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_push(cfg, incus, "feat-foo", source="main", action="merge")

    assert plan.direction == "push"
    assert plan.container_short == "feat-foo"
    assert plan.container_full == full
    assert plan.container_state == "Running"
    assert plan.source.label == "origin/main"
    assert plan.source.subject == "Bump deps"
    assert plan.target.label == "feat/foo"
    assert plan.target.subject == "WIP parser"
    assert plan.action == "merge"
    assert plan.incoming == 4
    assert plan.notes == ()


def test_plan_push_incoming_is_none_without_a_previous_push_anchor(mocker, make_cfg, tmp_path):
    """refs/jailbee/host/<source> absent (first push) -> no count, no crash.

    action="merge" (not "plain") deliberately: with "plain" the M3 gate
    already forces incoming=None regardless of the anchor, which would make
    this test pass for the wrong reason and pin the anchor-degradation
    behavior nowhere. "merge" keeps the anchor lookup on the only path that
    exercises it.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)

    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="a" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Subject")
    mocker.patch("jailbee.sync._count_local_only_commits", return_value=0)
    run_capture = mocker.patch("jailbee.sync.git.run_capture")

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return "feat/foo\n"
        if cmd[:2] == ["rev-parse", "--verify"] and cmd[-1].startswith("refs/jailbee/host/"):
            return ""  # anchor missing
        if cmd[:1] == ["rev-parse"]:
            return "b" * 40 + "\n"
        if cmd[:1] == ["log"]:
            return "WIP\n"
        if cmd[:1] == ["status"]:
            return ""
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_push(cfg, incus, "feat-foo", source="main", action="merge")

    assert plan.source.label == "main"
    assert plan.incoming is None
    run_capture.assert_not_called()


def test_plan_push_notes_missing_source_and_dirty_tree(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)

    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return "feat/foo\n"
        if cmd[:1] == ["rev-parse"]:
            return "c" * 40 + "\n"
        if cmd[:1] == ["log"]:
            return "WIP\n"
        if cmd[:1] == ["status"]:
            return " M src/app.py\n"
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_push(cfg, incus, "feat-foo", source="nope", action="merge")

    joined = " | ".join(plan.notes)
    assert "'nope' does not exist on the host" in joined
    assert "working tree is dirty" in joined
    assert plan.source.oid is None


def test_plan_push_notes_local_only_commits(mocker, make_cfg, tmp_path):
    """Source resolves (prefer=origin) and local-only commits are non-zero.

    Distinct from the missing-source case above: here `source_ref` is not
    None, so the `elif prefer == "origin"` branch — not the "does not
    exist" branch — is what has to produce the note.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)

    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="a" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Subject")
    mocker.patch("jailbee.sync.git.run_capture", return_value=(True, "0\n"))
    mocker.patch("jailbee.sync._count_local_only_commits", return_value=3)

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return "feat/foo\n"
        if cmd[:1] == ["rev-parse"]:
            return "b" * 40 + "\n"
        if cmd[:1] == ["log"]:
            return "WIP\n"
        if cmd[:1] == ["status"]:
            return ""
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    assert cfg.push.push_from == "origin"  # the elif branch this test targets requires it

    plan = sync.plan_push(cfg, incus, "feat-foo", source="main", action="plain")

    joined = " | ".join(plan.notes)
    assert "3" in joined
    assert "will NOT travel" in joined


def test_plan_push_notes_successful_fetch(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)

    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="a" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Subject")
    mocker.patch("jailbee.sync.git.run_capture", return_value=(True, "0\n"))
    mocker.patch("jailbee.sync._count_local_only_commits", return_value=0)
    incus.exec.side_effect = lambda _n, args, **_kw: (
        "feat/foo\n" if args[3] == "symbolic-ref" else "e" * 40 + "\n"
    )

    plan = sync.plan_push(
        cfg,
        incus,
        "feat-foo",
        source="main",
        action="plain",
        fetch_note=(True, None),
    )

    assert any("fetched origin/main first" in n for n in plan.notes)


def test_plan_push_notes_detached_head(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)

    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="a" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Subject")
    mocker.patch("jailbee.sync.git.run_capture", return_value=(True, "0\n"))
    mocker.patch("jailbee.sync._count_local_only_commits", return_value=0)

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return ""  # detached HEAD: no branch name
        if cmd[:1] == ["rev-parse"]:
            return "b" * 40 + "\n"
        if cmd[:1] == ["log"]:
            return "WIP\n"
        if cmd[:1] == ["status"]:
            return ""
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_push(cfg, incus, "feat-foo", source="main", action="plain")

    assert plan.target.label == "(detached HEAD)"
    assert any("detached" in n for n in plan.notes)


def test_plan_push_reports_the_hoisted_fetch_outcome(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)

    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="d" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Subject")
    mocker.patch("jailbee.sync.git.run_capture", return_value=(True, "0\n"))
    mocker.patch("jailbee.sync._count_local_only_commits", return_value=0)
    incus.exec.side_effect = lambda _n, args, **_kw: (
        "feat/foo\n" if args[3] == "symbolic-ref" else "e" * 40 + "\n"
    )

    plan = sync.plan_push(
        cfg,
        incus,
        "feat-foo",
        source="main",
        action="plain",
        fetch_note=(False, "fatal: could not read from remote"),
    )

    assert any("could not read from remote" in n for n in plan.notes)


def test_prefetch_push_source_fetches_in_origin_mode(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    fetch = mocker.patch("jailbee.sync.git.fetch_remote_ref")

    assert sync.prefetch_push_source(cfg, source="main", prefer="origin", fetch=True) == (
        True,
        None,
    )
    fetch.assert_called_once_with(cfg.repo_root, "origin", "main")


def test_prefetch_push_source_skips_in_local_mode(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    fetch = mocker.patch("jailbee.sync.git.fetch_remote_ref")

    assert sync.prefetch_push_source(cfg, source="main", prefer="local", fetch=True) == (
        False,
        None,
    )
    fetch.assert_not_called()


def test_prefetch_push_source_reports_a_failure_without_raising(mocker, make_cfg, tmp_path):
    from jailbee.git import GitFetchError

    cfg = make_cfg(tmp_path)  # push.autofetch defaults to True, so fetch=None fetches
    mocker.patch(
        "jailbee.sync.git.fetch_remote_ref",
        side_effect=GitFetchError("fetch failed", stderr="fatal: unable to access\n"),
    )

    fetched, err = sync.prefetch_push_source(cfg, source="main", prefer="origin", fetch=None)

    assert fetched is False
    assert err == "fatal: unable to access"


def test_plan_pull_targets_the_base_branch_label(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)
    incus.config_get.side_effect = lambda _n, key: {
        "user.jailbee.base_branch": "main",
        "user.jailbee.branch": "feat/foo",
    }.get(key)

    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="a" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Release 1.2")

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return "feat/foo\n"
        if cmd[:2] == ["rev-parse", "--verify"]:
            return "b" * 40 + "\n"  # every base candidate resolves
        if cmd[:1] == ["rev-parse"]:
            return "c" * 40 + "\n"
        if cmd[:1] == ["log"]:
            return "WIP parser\n"
        if cmd[:2] == ["rev-list", "--count"]:
            return "3\n"
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_pull(cfg, incus, "feat-foo", branch=None, into=None, ff="never")

    assert plan.direction == "pull"
    assert plan.source.label == "feat/foo"
    assert plan.source.subject == "WIP parser"
    assert plan.target.label == "main"
    assert plan.target.subject == "Release 1.2"
    assert plan.action == "merge"
    assert plan.incoming == 3


def test_plan_pull_into_overrides_the_label_and_notes_a_branch_switch(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)
    incus.config_get.side_effect = lambda _n, key: (
        "main" if key == "user.jailbee.base_branch" else None
    )

    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.commit_subject", return_value=None)
    incus.exec.side_effect = lambda _n, args, **_kw: (
        "feat/foo\n" if args[3] == "symbolic-ref" else ""
    )

    plan = sync.plan_pull(cfg, incus, "feat-foo", branch=None, into="develop", ff="always")

    assert plan.target.label == "develop"
    assert plan.target.oid is None
    assert plan.action == "ff-only"
    assert plan.incoming is None
    # I3: the old wording ("needs --checkout") asserted a requirement that
    # doesn't exist — merge_from_container fast-forwards refs/heads/<target>
    # in place without a checkout; --checkout is only needed on divergence.
    assert any(
        "will be fast-forwarded in place" in n
        and "--checkout is needed only if it has diverged" in n
        for n in plan.notes
    )


def test_plan_checkout_targets_the_same_branch_name_on_the_host(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)
    incus.config_get.side_effect = lambda _n, key: (
        "main" if key == "user.jailbee.base_branch" else None
    )

    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.commit_subject", return_value=None)
    incus.exec.side_effect = lambda _n, args, **_kw: (
        "feat/foo\n" if args[3] == "symbolic-ref" else ""
    )

    plan = sync.plan_checkout(cfg, incus, "feat-foo", branch=None)

    assert plan.direction == "checkout"
    assert plan.source.label == "feat/foo"
    assert plan.target.label == "feat/foo"
    assert plan.action == "ff-only"
    assert any("will be created on the host" in n for n in plan.notes)


def test_plan_checkout_uses_the_pr_branch_label_when_present(mocker, make_cfg, tmp_path):
    """I1: checkout_from_container's real host target is
    `_container_pr_branch(...) or <resolved container branch>` — plan_checkout
    must resolve its target the same way, not `branch or container_branch`.

    Reverting the fix (target = branch/container_branch, ignoring the PR
    label) makes this fail: plan.target.label would come back 'feat/foo'
    instead of the PR head the checkout actually targets.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)
    incus.config_get.side_effect = lambda _n, key: {
        "user.jailbee.pr_branch": "pr-123-head",
        "user.jailbee.branch": "feat/foo",
    }.get(key)

    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="a" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="PR head bump")

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return "feat/foo\n"
        if cmd[:1] == ["rev-parse"]:
            return "b" * 40 + "\n"
        if cmd[:1] == ["log"]:
            return "WIP parser\n"
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_checkout(cfg, incus, "feat-foo", branch=None)

    assert plan.source.label == "feat/foo"  # what gets fetched from the container
    assert plan.target.label == "pr-123-head"  # what checkout_from_container really targets


def test_plan_checkout_shows_the_as_name_as_the_host_target(mocker, make_cfg, tmp_path):
    """The confirmation block must name the branch the checkout really writes:
    with `--as`, that is the given name — outranking the PR label, exactly as
    `checkout_from_container` resolves it.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)
    incus.config_get.side_effect = lambda _n, key: {
        "user.jailbee.pr_branch": "pr-123-head",
        "user.jailbee.branch": "feat/foo",
    }.get(key)

    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.commit_subject", return_value=None)
    incus.exec.side_effect = lambda _n, args, **_kw: (
        "feat/foo\n" if args[3] == "symbolic-ref" else ""
    )

    plan = sync.plan_checkout(cfg, incus, "feat-foo", branch=None, as_name="local-review")

    assert plan.source.label == "feat/foo"
    assert plan.target.label == "local-review"
    assert any("'local-review' will be created on the host" in n for n in plan.notes)


def test_plan_pull_explicit_branch_reads_that_refs_tip_not_head(mocker, make_cfg, tmp_path):
    """I2 trigger 1: `-b <other>` on an auto-selected container must read
    oid/subject/count from refs/heads/<other> inside the container, not from
    the literal HEAD — the real fetch reads refs/heads/<branch>, and the host
    is on a different branch than <other> here.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)
    incus.config_get.side_effect = lambda _n, key: {"user.jailbee.base_branch": "main"}.get(key)

    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="c" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Release 1.2")

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return "feat/foo\n"  # container is actually ON feat/foo, not 'other'
        if cmd == ["rev-parse", "--verify", "--quiet", "HEAD"]:
            raise AssertionError("must read refs/heads/other, not the literal HEAD")
        if cmd[:2] == ["rev-parse", "--verify"] and cmd[-1] == "refs/heads/other":
            return "d" * 40 + "\n"
        if cmd[:2] == ["rev-parse", "--verify"]:
            return "b" * 40 + "\n"  # base anchor candidate resolves
        if cmd[:1] == ["log"] and cmd[-1] == "refs/heads/other":
            return "Other branch subject\n"
        if cmd[:2] == ["rev-list", "--count"]:
            return "7\n"
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_pull(cfg, incus, "feat-foo", branch="other", into=None, ff="never")

    assert plan.source.label == "other"
    assert plan.source.oid == "d" * 40
    assert plan.source.subject == "Other branch subject"
    assert plan.incoming == 7
    assert not any("detached" in n for n in plan.notes)


def test_plan_checkout_explicit_branch_reads_that_refs_tip_not_head(mocker, make_cfg, tmp_path):
    """I2 trigger 1, checkout side: same bug, same fix, via plan_checkout."""
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)
    incus.config_get.side_effect = lambda _n, key: None  # no PR label, no base label

    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)
    mocker.patch("jailbee.sync.git.commit_subject", return_value=None)

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return "feat/foo\n"
        if cmd == ["rev-parse", "--verify", "--quiet", "HEAD"]:
            raise AssertionError("must read refs/heads/other, not the literal HEAD")
        if cmd[:2] == ["rev-parse", "--verify"] and cmd[-1] == "refs/heads/other":
            return "e" * 40 + "\n"
        if cmd[:1] == ["log"] and cmd[-1] == "refs/heads/other":
            return "Other subject\n"
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_checkout(cfg, incus, "feat-foo", branch="other")

    assert plan.source.label == "other"
    assert plan.source.oid == "e" * 40
    assert plan.source.subject == "Other subject"
    assert plan.target.label == "other"
    assert not any("detached" in n for n in plan.notes)


def test_plan_pull_detached_head_with_branch_label_still_notes_detached(mocker, make_cfg, tmp_path):
    """I2 trigger 2: HEAD detached, `user.jailbee.branch` label set. `_resolve_branch`
    falls back to the label, but the container is NOT checked out on it — the
    "container HEAD is detached" note must still fire, and the source must
    read the label's own ref tip (refs/heads/<label>), not the detached
    commit misattributed to that name.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)
    incus.config_get.side_effect = lambda _n, key: {
        "user.jailbee.branch": "feat/foo",
        "user.jailbee.base_branch": "main",
    }.get(key)

    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="f" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Release 1.2")

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return ""  # detached: no symbolic ref
        if cmd[:2] == ["rev-parse", "--verify"] and cmd[-1] == "refs/heads/feat/foo":
            return "1" * 40 + "\n"
        if cmd[:2] == ["rev-parse", "--verify"]:
            return "2" * 40 + "\n"  # base anchor candidates
        if cmd[:1] == ["log"] and cmd[-1] == "refs/heads/feat/foo":
            return "Label branch subject\n"
        if cmd[:2] == ["rev-list", "--count"]:
            return "1\n"
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_pull(cfg, incus, "feat-foo", branch=None, into=None, ff="never")

    assert plan.source.label == "feat/foo"
    assert plan.source.oid == "1" * 40
    assert plan.source.subject == "Label branch subject"
    assert any("detached" in n for n in plan.notes)


def test_plan_checkout_detached_head_with_branch_label_still_notes_detached(
    mocker, make_cfg, tmp_path
):
    """I2 trigger 2, checkout side: same bug, same fix, via plan_checkout."""
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)
    incus.config_get.side_effect = lambda _n, key: {"user.jailbee.branch": "feat/foo"}.get(key)

    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value="a" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Label branch subject")

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return ""  # detached
        if cmd[:2] == ["rev-parse", "--verify"] and cmd[-1] == "refs/heads/feat/foo":
            return "3" * 40 + "\n"
        if cmd[:1] == ["log"]:
            return "Label branch subject\n"
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_checkout(cfg, incus, "feat-foo", branch=None)

    assert plan.source.label == "feat/foo"
    assert plan.source.oid == "3" * 40
    assert any("detached" in n for n in plan.notes)


def test_plan_push_plain_action_suppresses_the_commit_count(mocker, make_cfg, tmp_path):
    """M3: 'plain' only writes refs/jailbee/host/<source> — it applies nothing to
    the container's branch — so 'N commit(s) to apply' would overstate what
    happens. `incoming` must stay None for 'plain' even when the anchor
    resolves and would otherwise yield a nonzero count.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)

    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="a" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Subject")
    mocker.patch("jailbee.sync.git.run_capture", return_value=(True, "5\n"))
    mocker.patch("jailbee.sync._count_local_only_commits", return_value=0)

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return "feat/foo\n"
        if cmd[:1] == ["rev-parse"]:
            return "b" * 40 + "\n"  # anchor resolves
        if cmd[:1] == ["log"]:
            return "WIP\n"
        if cmd[:1] == ["status"]:
            return ""
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_push(cfg, incus, "feat-foo", source="main", action="plain")

    assert plan.action == "plain"
    assert plan.incoming is None


def test_plan_push_fetch_failure_note_suppressed_when_source_is_local_only(
    mocker, make_cfg, tmp_path
):
    """M1: mirrors the gate in cli._print_push_summary — a failed host fetch of
    origin/<source> is noise when source_ref fell back to
    refs/heads/<source> (branch not on origin at all, the normal stacked-PR
    case). Only warn when the origin-tracking ref is what actually travelled.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _wire_plan_container(mocker, incus, cfg, full)

    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.rev_parse", return_value="a" * 40)
    mocker.patch("jailbee.sync.git.commit_subject", return_value="Subject")
    mocker.patch("jailbee.sync._count_local_only_commits", return_value=0)

    def _exec(_name, args, **_kw):
        cmd = args[3:]
        if cmd[:1] == ["symbolic-ref"]:
            return "feat/foo\n"
        if cmd[:1] == ["rev-parse"]:
            return "b" * 40 + "\n"
        if cmd[:1] == ["log"]:
            return "WIP\n"
        if cmd[:1] == ["status"]:
            return ""
        raise AssertionError(f"unexpected exec: {cmd}")

    incus.exec.side_effect = _exec

    plan = sync.plan_push(
        cfg,
        incus,
        "feat-foo",
        source="feat/stacked",
        action="plain",
        fetch_note=(False, "fatal: could not read from remote"),
    )

    assert not any("could not read from remote" in n for n in plan.notes)
    assert not any("fetch" in n for n in plan.notes)


# --- .git/index.lock contention ------------------------------------------
#
# A container-side `git merge` / `rebase` / `reset --hard` fails outright when
# another git process in the container holds `.git/index.lock`. Observed in the
# wild: `jailbee git push --merge` died with "Unable to create
# '/home/dev/<repo>/.git/index.lock': File exists" and succeeded on an
# immediate retry — the lock was transient, held by a concurrent git.

_LOCK_STDERR = (
    "`incus exec c --user 53023 -- git -C /home/dev/repo merge` failed (exit 1): "
    "error: Unable to create '/home/dev/repo/.git/index.lock': File exists.\n"
    "\n"
    "Another git process seems to be running in this repository"
)


def _lock_error():
    from jailbee.incus import IncusError

    return IncusError(_LOCK_STDERR)


def test_index_lock_held_recognises_gits_lock_message():
    assert sync._index_lock_held(_lock_error()) is True


def test_index_lock_held_ignores_unrelated_failures():
    from jailbee.incus import IncusError

    assert sync._index_lock_held(IncusError("CONFLICT (content): Merge conflict in a.txt")) is False


def _failing_then_ok(exc_factory, attempts):
    """Dispatcher value: raise `exc_factory()` until `attempts` is long enough."""

    def value():
        attempts.append(1)
        if len(attempts) < 2:
            raise exc_factory()
        return ""

    return value


def test_push_and_merge_retries_while_the_index_lock_is_held(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    attempts: list[int] = []

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": _failing_then_ok(_lock_error, attempts),
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    sleep = mocker.patch("jailbee.sync.time.sleep")

    result = push_and_merge(cfg, incus, "feat-foo")

    assert result.head_oid == "container-head-oid"
    assert len(attempts) == 2, "the locked merge must be retried, not reported as a failure"
    assert sleep.call_count == 1, "a retry must back off, not spin"


def test_push_and_merge_reports_a_stuck_index_lock_without_the_raw_exec_dump(
    mocker, make_cfg, tmp_path
):
    from jailbee.incus import IncusError
    from jailbee.sync import SyncError, push_and_merge

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    attempts: list[int] = []

    def always_locked():
        attempts.append(1)
        raise _lock_error()

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "merge": always_locked,
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mocker.patch("jailbee.sync.time.sleep")

    with pytest.raises(SyncError) as excinfo:
        push_and_merge(cfg, incus, "feat-foo")

    assert len(attempts) == sync._INDEX_LOCK_ATTEMPTS
    message = str(excinfo.value)
    assert "another git process" in message
    assert "/home/dev/repo/.git/index.lock" in message
    assert "jailbee shell feat-foo" in message
    assert "incus exec" not in message, "the raw exec command line is noise, not a diagnosis"


def test_push_and_rebase_retries_while_the_index_lock_is_held(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_rebase

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    attempts: list[int] = []

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "feat/foo\n",
            "rev_parse_gie": "",
            "rebase": _failing_then_ok(_lock_error, attempts),
            "rev_parse_head": "container-head-oid\n",
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mocker.patch("jailbee.sync.time.sleep")

    result = push_and_rebase(cfg, incus, "feat-foo")

    assert result.head_oid == "container-head-oid"
    assert len(attempts) == 2


def test_push_and_reset_retries_while_the_index_lock_is_held(mocker, make_cfg, tmp_path):
    from jailbee.incus import IncusError
    from jailbee.sync import push_and_reset

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    attempts: list[int] = []

    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": "main\n",
            "rev_parse_gie": "",
            "rev_parse_head": "old-branch-oid\n",
            "rev_list_count": "0\n",
            "reset": _failing_then_ok(_lock_error, attempts),
        }
    )

    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    mocker.patch("jailbee.sync.time.sleep")

    result = push_and_reset(cfg, incus, "feat-foo")

    assert result.head_oid == "old-branch-oid"
    assert len(attempts) == 2


def test_container_status_preflight_does_not_take_the_index_lock(mocker):
    """`git status --porcelain` refreshes and rewrites the index, so the
    read-only dirty-tree preflight both takes the lock and fails on one held
    by someone else. GIT_OPTIONAL_LOCKS=0 removes both halves.
    """
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    sync._container_status_dirty(incus, "c", "/home/dev/repo", uid=53023)
    env = incus.exec.call_args.kwargs.get("env") or {}
    assert env.get("GIT_OPTIONAL_LOCKS") == "0"


def test_container_status_preflight_is_bounded_by_a_timeout(mocker):
    """The probe must not be able to hang forever.

    `jailbee pr` runs it between the container fetch and the push, where an
    `incus exec` that never returns leaves the terminal silent after git's
    fetch output — the most misleading place in the flow to stall.
    """
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    sync._container_status_dirty(incus, "c", "/home/dev/repo", uid=53023)
    assert incus.exec.call_args.kwargs.get("timeout") == sync._STATUS_PROBE_TIMEOUT_S
    assert sync._STATUS_PROBE_TIMEOUT_S > 0


def test_container_status_preflight_timeout_becomes_a_sync_error(mocker):
    """A timed-out probe reports as a SyncError, carrying incus's own detail."""
    from jailbee.incus import IncusTimeoutError

    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusTimeoutError("`incus exec c ...` timed out after 60s")
    with pytest.raises(sync.SyncError, match="timed out after 60s"):
        sync._container_status_dirty(incus, "c", "/home/dev/repo", uid=53023)


def test_merge_container_into_container_relays_through_the_host(mocker, make_cfg, tmp_path):
    """Source container -> host -> target container, with the source's own namespaces.

    The superproject ref lands under `from/<source>` (Design Ruling R1) so it can
    never collide with `refs/jailbee/host/*` or `refs/jailbee/base/*`; the
    submodule refs keep the bare `<source>` namespace `transport_submodules_to_host`
    already wrote. The host-side ref that `fetch_from_container` produced is bare
    too — the asymmetry is deliberate.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        side_effect=lambda c, i, s: f"{cfg.container_prefix}-{s}",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    incus.config_get.return_value = None
    mocker.patch("jailbee.sync._container_is_running", return_value=True)
    mocker.patch("jailbee.sync._run_container_preflights", return_value="feat/b")
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=sync.FetchResult(
            branch="feat/a", old_oid=None, new_oid="asha", base_oid=None, commits_added=2
        ),
    )
    to_host = mocker.patch("jailbee.submodules.transport_submodules_to_host")
    sub_paths = mocker.patch("jailbee.submodules._container_submodule_paths", return_value=["sub"])
    to_container = mocker.patch("jailbee.submodules.transport_submodules_to_container")
    call_order: list[str] = []
    to_host.side_effect = lambda *a, **k: call_order.append("to_host")
    to_container.side_effect = lambda *a, **k: call_order.append("to_container")
    push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=sync.PushResult(
            source="feat/a",
            source_ref="refs/jailbee/c1/feat/a",
            container_ref="refs/jailbee/from/c1/feat/a",
            old_oid=None,
            new_oid="asha",
        ),
    )
    merge = mocker.patch("jailbee.sync._merge_ref_in_container", return_value="mergedsha")

    result = sync.merge_container_into_container(cfg, incus, "c1", "c2")

    to_host.assert_called_once()
    # Which container is which. Every one of these travels positionally, so a
    # swap (relaying INTO the source, or merging INSIDE the source) is
    # invisible to the kwargs assertions below. `container_repo_dir` is mocked
    # to "/repo" for both, so the container name is the only discriminator.
    assert to_host.call_args.args[2] == f"{cfg.container_prefix}-c1"
    assert to_host.call_args.args[3] == "c1"
    assert sub_paths.call_args.args[1] == f"{cfg.container_prefix}-c1"
    assert to_container.call_args.args[2] == f"{cfg.container_prefix}-c2"
    assert push.call_args.args[2] == "c2"
    assert merge.call_args.args[1] == f"{cfg.container_prefix}-c2"
    assert merge.call_args.kwargs.get("short") == "c2"
    # The source container's submodule refs must be relayed under ITS namespace.
    assert to_container.call_args.kwargs.get("source_ns") == "c1"
    assert to_container.call_args.kwargs.get("paths") == ["sub"]
    assert push.call_args.kwargs.get("namespace") == "from/c1"
    assert push.call_args.kwargs.get("source_ref") == "refs/jailbee/c1/feat/a"
    assert merge.call_args.kwargs.get("ref") == "refs/jailbee/from/c1/feat/a"
    # target_branch ("feat/b") differs from the fetched branch ("feat/a"), so
    # the merge must not be pinned to fast-forward-only.
    assert merge.call_args.kwargs["ff_only"] is False
    # Host first, then container — not arbitrary sequence-pinning. For a
    # submodule born inside the source container the host has no sub-repo yet;
    # `transport_submodules_to_host` clones one, and only then can
    # `transport_submodules_to_container`'s creation path read
    # `_submodule_upstream_url(repo_root / path)` to give the new container-side
    # sub-repo an origin. Reversed, that origin is silently empty and nothing
    # downstream in this call path errors.
    assert call_order == ["to_host", "to_container"]
    assert result.head_oid == "mergedsha"


def _merge_relay_wiring(mocker, make_cfg, tmp_path, *, target_branch="feat/b"):
    """The common `merge_container_into_container` mocks: names, transport, push.

    Everything up to and including the push is stubbed; what the caller is left
    free to drive is the merge itself and the container-side git the report
    reads. Returns `(cfg, incus)`.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        side_effect=lambda c, i, s: f"{cfg.container_prefix}-{s}",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    incus.config_get.return_value = None
    mocker.patch("jailbee.sync._container_is_running", return_value=True)
    mocker.patch("jailbee.sync._run_container_preflights", return_value=target_branch)
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=sync.FetchResult(
            branch="feat/a", old_oid=None, new_oid="asha", base_oid=None, commits_added=2
        ),
    )
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.submodules._container_submodule_paths", return_value=["deps/libfoo"])
    mocker.patch("jailbee.submodules.transport_submodules_to_container")
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=sync.PushResult(
            source="feat/a",
            source_ref="refs/jailbee/c1/feat/a",
            container_ref="refs/jailbee/from/c1/feat/a",
            old_oid=None,
            new_oid="asha",
        ),
    )
    return cfg, incus


def test_merge_container_into_container_reports_submodule_moves(mocker, make_cfg, tmp_path):
    """The gitlinks that moved are read from inside the TARGET container.

    Neither superproject commit exists on the host: the merge commit is created
    inside the target, and the target's pre-merge HEAD was never fetched. A
    host-side diff would silently report nothing at all, which is why this
    cannot reuse `compute_submodule_moves`' host entry point.

    `submodules._container_runner` is left real so the sub-repo really is
    queried at a *container* path — `/repo/deps/libfoo`, never a host one.
    """
    cfg, incus = _merge_relay_wiring(mocker, make_cfg, tmp_path)
    mocker.patch("jailbee.sync._container_head_oid", return_value="1111111preheadoid")
    mocker.patch("jailbee.sync._merge_ref_in_container", return_value="2222222mergedoid")
    raw = (
        ":160000 160000 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb M\tdeps/libfoo\n"
        ":100644 100644 cccc dddd M\tapp.py\n"  # not a gitlink — ignored
    )
    asked: list[tuple[str, list[str]]] = []

    def exec_side_effect(name, cmd, **kwargs):
        assert cmd[:2] == ["git", "-C"], cmd
        asked.append((cmd[2], cmd[3:]))
        joined = " ".join(cmd)
        if "diff --raw" in joined:
            return raw
        if "rev-list" in joined:
            return "3\n"
        if "--shortstat" in joined:
            return " 2 files changed, 12 insertions(+), 5 deletions(-)\n"
        return ""

    incus.exec.side_effect = exec_side_effect

    result = sync.merge_container_into_container(cfg, incus, "c1", "c2")

    assert list(result.submodule_moves) == [
        sync.SubmoduleMove(
            path="deps/libfoo",
            old_sha="a" * 40,
            new_sha="b" * 40,
            status="modified",
            commits=3,
            ins=12,
            dels=5,
        )
    ]
    # The superproject diff spans the target's own HEADs, not the source's.
    superproject = [args for cwd, args in asked if cwd == "/repo" and args[:2] == ["diff", "--raw"]]
    assert superproject and "1111111preheadoid..2222222mergedoid" in superproject[0]
    # The sub-repo is read inside the container, at the container's path.
    assert any(cwd == "/repo/deps/libfoo" for cwd, _args in asked)
    assert not any(str(tmp_path) in cwd for cwd, _args in asked)


def test_merge_container_into_container_plain_reports_no_submodule_moves(
    mocker, make_cfg, tmp_path
):
    """`--plain` runs no merge, so no gitlink moved and none may be reported."""
    cfg, incus = _merge_relay_wiring(mocker, make_cfg, tmp_path)
    mocker.patch("jailbee.sync._container_head_oid", return_value="1111111preheadoid")
    incus.exec.side_effect = AssertionError("a plain run must not diff the superproject")

    result = sync.merge_container_into_container(cfg, incus, "c1", "c2", plain=True)

    assert tuple(result.submodule_moves) == ()


def test_merge_container_into_container_same_branch_uses_ff_only(mocker, make_cfg, tmp_path):
    """target_branch == fetch_result.branch pins the merge to fast-forward-only.

    Mirrors `test_push_and_merge_same_branch_uses_ff_only`'s coverage of the
    same decision in the push-and-merge path.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        side_effect=lambda c, i, s: f"{cfg.container_prefix}-{s}",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    incus.config_get.return_value = None
    mocker.patch("jailbee.sync._container_is_running", return_value=True)
    mocker.patch("jailbee.sync._run_container_preflights", return_value="feat/a")
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=sync.FetchResult(
            branch="feat/a", old_oid=None, new_oid="asha", base_oid=None, commits_added=2
        ),
    )
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.submodules._container_submodule_paths", return_value=[])
    mocker.patch("jailbee.submodules.transport_submodules_to_container")
    mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=sync.PushResult(
            source="feat/a",
            source_ref="refs/jailbee/c1/feat/a",
            container_ref="refs/jailbee/from/c1/feat/a",
            old_oid=None,
            new_oid="asha",
        ),
    )
    merge = mocker.patch("jailbee.sync._merge_ref_in_container", return_value="mergedsha")

    result = sync.merge_container_into_container(cfg, incus, "c1", "c2")

    assert merge.call_args.kwargs["ff_only"] is True
    assert result.fast_forward_only is True


def test_merge_container_into_container_preflights_the_target_before_transport(
    mocker, make_cfg, tmp_path
):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        side_effect=lambda c, i, s: f"{cfg.container_prefix}-{s}",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    incus.config_get.return_value = None
    mocker.patch("jailbee.sync._container_is_running", return_value=True)
    mocker.patch(
        "jailbee.sync._run_container_preflights",
        side_effect=sync.SyncError("target has a merge in progress"),
    )
    fetch = mocker.patch("jailbee.sync.fetch_from_container")
    to_host = mocker.patch("jailbee.submodules.transport_submodules_to_host")
    to_container = mocker.patch("jailbee.submodules.transport_submodules_to_container")
    push = mocker.patch("jailbee.sync.push_to_container")

    with pytest.raises(sync.SyncError, match="merge in progress"):
        sync.merge_container_into_container(cfg, incus, "c1", "c2")

    # Nothing may be transported before the target is known to be mergeable —
    # a refusal must not leave half-populated refs behind.
    fetch.assert_not_called()
    to_host.assert_not_called()
    to_container.assert_not_called()
    push.assert_not_called()


def test_merge_container_into_container_plain_skips_the_merge(mocker, make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        side_effect=lambda c, i, s: f"{cfg.container_prefix}-{s}",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    incus.config_get.return_value = None
    mocker.patch("jailbee.sync._container_is_running", return_value=True)
    mocker.patch("jailbee.sync._run_container_preflights", return_value="feat/b")
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=sync.FetchResult(
            branch="feat/a", old_oid=None, new_oid="asha", base_oid=None, commits_added=1
        ),
    )
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.submodules._container_submodule_paths", return_value=[])
    to_container = mocker.patch("jailbee.submodules.transport_submodules_to_container")
    push = mocker.patch(
        "jailbee.sync.push_to_container",
        return_value=sync.PushResult(
            source="feat/a",
            source_ref="refs/jailbee/c1/feat/a",
            container_ref="refs/jailbee/from/c1/feat/a",
            old_oid=None,
            new_oid="asha",
        ),
    )
    mocker.patch("jailbee.sync._container_head_oid", return_value="targethead")
    merge = mocker.patch("jailbee.sync._merge_ref_in_container")

    result = sync.merge_container_into_container(cfg, incus, "c1", "c2", plain=True)

    merge.assert_not_called()
    # `plain` stops AFTER the transport — the ref must be in the target for
    # inspection, so a "plain" that skipped the push too would be wrong.
    push.assert_called_once()
    # An empty submodule list must not provoke an empty relay push either.
    to_container.assert_not_called()
    assert result.head_oid == "targethead"
    assert result.fast_forward_only is False


def test_merge_container_into_container_mount_mode_raises(mocker, make_cfg, tmp_path):
    """A mount-mode target shares the host's tree, so nothing may be relayed into it.

    The refusal must land before any transport: a guard that raised only after
    `fetch_from_container` had run would satisfy `pytest.raises` and still have
    written `refs/jailbee/*` on the host. The whole downstream is therefore
    mocked, so removing the guard reaches a clean "DID NOT RAISE" rather than
    exploding somewhere further along.
    """
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-c2"
    _mock_container_running(incus, full)
    incus.config_get.return_value = "mount"

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.sync._run_container_preflights", return_value="feat/b")
    fetch = mocker.patch("jailbee.sync.fetch_from_container")
    to_host = mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.submodules._container_submodule_paths", return_value=[])
    to_container = mocker.patch("jailbee.submodules.transport_submodules_to_container")
    push = mocker.patch("jailbee.sync.push_to_container")
    mocker.patch("jailbee.sync._merge_ref_in_container")
    mocker.patch("jailbee.sync._container_head_oid", return_value="targethead")

    with pytest.raises(sync.SyncError, match="mount mode"):
        sync.merge_container_into_container(cfg, incus, "c1", "c2")

    fetch.assert_not_called()
    to_host.assert_not_called()
    to_container.assert_not_called()
    push.assert_not_called()


def test_merge_container_into_container_stopped_raises(mocker, make_cfg, tmp_path):
    """A stopped target cannot be merged into, and is refused before any transport."""
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-c2"
    _mock_container_stopped(incus, full)
    incus.config_get.return_value = None

    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.sync._run_container_preflights", return_value="feat/b")
    fetch = mocker.patch("jailbee.sync.fetch_from_container")
    to_host = mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.submodules._container_submodule_paths", return_value=[])
    to_container = mocker.patch("jailbee.submodules.transport_submodules_to_container")
    push = mocker.patch("jailbee.sync.push_to_container")
    mocker.patch("jailbee.sync._merge_ref_in_container")
    mocker.patch("jailbee.sync._container_head_oid", return_value="targethead")

    with pytest.raises(sync.SyncError, match="not running"):
        sync.merge_container_into_container(cfg, incus, "c1", "c2")

    fetch.assert_not_called()
    to_host.assert_not_called()
    to_container.assert_not_called()
    push.assert_not_called()


# --- push_and_merge: the fast-forward decision -----------------------------
#
# `--merge` forced `--ff-only` whenever the container was already on the
# branch being pushed, which is *always* true for `jailbee push --pr --merge`.
# A container with its own commits and a PR head that had moved on could
# therefore not be merged at all:
#
#   ✗ git merge failed in container 'feature-15319-…':
#     fatal: Not possible to fast-forward, aborting.
#
# These pin the six-way decision (auto / --ff / --no-ff, divergent or not)
# and the prompt that makes the auto case recoverable.


def _merge_dispatch(mocker, make_cfg, tmp_path, *, head_branch, rev_list_count):
    """A push_and_merge rig whose divergence probe answers `rev_list_count`.

    `rev_list_count` is `git rev-list --left-right --count HEAD...<ref>`
    output: "<commits only on HEAD>\t<commits only on the pushed ref>".
    """
    from jailbee.incus import IncusError

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"
    _mock_container_running(incus, full)
    incus.config_get.return_value = None
    incus.exec.side_effect = _exec_dispatcher(
        {
            "status": "",
            "merge_head": IncusError("not found"),
            "rebase_merge": IncusError("not found"),
            "rebase_apply": IncusError("not found"),
            "head_branch": f"{head_branch}\n",
            "rev_parse_gie": "",
            "rev_list_count": rev_list_count,
            "merge": "",
            "rev_parse_head": "container-head-oid\n",
        }
    )
    _common_push_patches(mocker, cfg, full)
    mocker.patch("jailbee.sync.submodules.update_submodules_in_container")
    mocker.patch("jailbee.sync.submodules.transport_submodules_to_container")
    return cfg, incus


def _merge_cmd(incus):
    """The one `git merge` command run, or None if none was."""
    calls = [
        c
        for c in incus.exec.call_args_list
        if "merge" in c.args[1] and "rev-parse" not in c.args[1] and "rev-list" not in c.args[1]
    ]
    assert len(calls) <= 1, f"expected at most one merge, got {[c.args[1] for c in calls]}"
    return calls[0].args[1] if calls else None


def test_same_branch_without_divergence_still_fast_forwards(mocker, make_cfg, tmp_path):
    """The behaviour that was always right, pinned so the fix cannot widen it
    into "always make a merge commit"."""
    from jailbee.sync import push_and_merge

    cfg, incus = _merge_dispatch(
        mocker, make_cfg, tmp_path, head_branch="main", rev_list_count="0\t2\n"
    )

    result = push_and_merge(cfg, incus, "feat-foo")

    assert result.fast_forward_only is True
    assert "--ff-only" in _merge_cmd(incus)


def test_diverged_same_branch_merges_when_the_user_says_yes(mocker, make_cfg, tmp_path):
    from jailbee.sync import push_and_merge

    cfg, incus = _merge_dispatch(
        mocker, make_cfg, tmp_path, head_branch="main", rev_list_count="3\t2\n"
    )

    result = push_and_merge(cfg, incus, "feat-foo", confirm=lambda _msg: True)

    cmd = _merge_cmd(incus)
    assert "--ff-only" not in cmd
    assert "-m" in cmd
    assert result.fast_forward_only is False


def test_diverged_same_branch_aborts_when_the_user_says_no(mocker, make_cfg, tmp_path):
    from jailbee.sync import SyncError, push_and_merge

    cfg, incus = _merge_dispatch(
        mocker, make_cfg, tmp_path, head_branch="main", rev_list_count="3\t2\n"
    )

    with pytest.raises(SyncError, match="not possible"):
        push_and_merge(cfg, incus, "feat-foo", confirm=lambda _msg: False)

    assert _merge_cmd(incus) is None, "declining must not run git merge at all"


def test_diverged_same_branch_without_a_tty_names_the_flag(mocker, make_cfg, tmp_path):
    """`confirm=None` is the non-interactive path — a script, or the detached
    background worker. It must say what flag unblocks it rather than leave
    the user to read git's fast-forward hint and guess."""
    from jailbee.sync import SyncError, push_and_merge

    cfg, incus = _merge_dispatch(
        mocker, make_cfg, tmp_path, head_branch="main", rev_list_count="3\t2\n"
    )

    with pytest.raises(SyncError, match="--no-ff"):
        push_and_merge(cfg, incus, "feat-foo", confirm=None)

    assert _merge_cmd(incus) is None


def test_the_prompt_reports_both_sides_of_the_divergence(mocker, make_cfg, tmp_path, capsys):
    """The counts are the whole reason to ask rather than just fail: they are
    what tells the user whether a merge commit is what they want. A bare
    "merge anyway?" with no numbers is not an informed choice."""
    from jailbee.sync import push_and_merge

    cfg, incus = _merge_dispatch(
        mocker, make_cfg, tmp_path, head_branch="main", rev_list_count="3\t2\n"
    )
    seen: list[str] = []

    def _confirm(msg: str) -> bool:
        seen.append(msg)
        return True

    push_and_merge(cfg, incus, "feat-foo", confirm=_confirm)

    assert seen, "expected the user to be asked"
    out = capsys.readouterr().out
    assert "diverged" in out
    assert "3 commit(s) not on the pushed ref" in out
    assert "2 commit(s) not in the container" in out
    # Both branch names, so the report says which two things diverged.
    assert "'main'" in out


def test_the_non_interactive_error_carries_the_same_counts(mocker, make_cfg, tmp_path):
    """A script's operator has no prompt to read, so the counts must be in
    the exception itself — not printed alongside it and lost."""
    from jailbee.sync import SyncError, push_and_merge

    cfg, incus = _merge_dispatch(
        mocker, make_cfg, tmp_path, head_branch="main", rev_list_count="3\t2\n"
    )

    with pytest.raises(SyncError) as excinfo:
        push_and_merge(cfg, incus, "feat-foo", confirm=None)

    msg = str(excinfo.value)
    assert "3 commit(s) not on the pushed ref" in msg
    assert "2 commit(s) not in the container" in msg
    assert "--no-ff" in msg


def test_no_ff_true_makes_a_merge_commit_on_a_matching_branch(mocker, make_cfg, tmp_path):
    """`--no-ff` skips the prompt entirely — the user already answered it."""
    from jailbee.sync import push_and_merge

    cfg, incus = _merge_dispatch(
        mocker, make_cfg, tmp_path, head_branch="main", rev_list_count="3\t2\n"
    )

    def _confirm(_msg: str) -> bool:
        raise AssertionError("--no-ff must not ask")

    result = push_and_merge(cfg, incus, "feat-foo", no_ff=True, confirm=_confirm)

    cmd = _merge_cmd(incus)
    assert "--ff-only" not in cmd
    assert "-m" in cmd
    assert result.fast_forward_only is False


def test_no_ff_false_demands_a_fast_forward_across_branches(mocker, make_cfg, tmp_path):
    """`--ff` is the other half of the tri-state: it forces `--ff-only` even
    where the automatic choice would have made a merge commit."""
    from jailbee.sync import push_and_merge

    cfg, incus = _merge_dispatch(
        mocker, make_cfg, tmp_path, head_branch="feat/foo", rev_list_count="0\t2\n"
    )

    result = push_and_merge(cfg, incus, "feat-foo", no_ff=False)

    assert "--ff-only" in _merge_cmd(incus)
    assert result.fast_forward_only is True


def test_a_different_branch_still_gets_a_merge_commit_by_default(mocker, make_cfg, tmp_path):
    from jailbee.sync import push_and_merge

    cfg, incus = _merge_dispatch(
        mocker, make_cfg, tmp_path, head_branch="feat/foo", rev_list_count="3\t2\n"
    )

    def _confirm(_msg: str) -> bool:
        raise AssertionError("a cross-branch merge was never ff-only, so nothing to ask")

    result = push_and_merge(cfg, incus, "feat-foo", confirm=_confirm)

    assert "--ff-only" not in _merge_cmd(incus)
    assert result.fast_forward_only is False


def test_an_unreadable_divergence_keeps_the_fast_forward(mocker, make_cfg, tmp_path):
    """An unreadable probe means "cannot tell", never "no divergence".

    Silently making a merge commit because a probe failed would rewrite the
    container's history on the strength of an error. Falling through to
    `--ff-only` leaves git to produce its own diagnosis, which is what
    happened before the probe existed.
    """
    from jailbee.sync import push_and_merge

    cfg, incus = _merge_dispatch(
        mocker, make_cfg, tmp_path, head_branch="main", rev_list_count="not a count\n"
    )

    def _confirm(_msg: str) -> bool:
        raise AssertionError("an unknown divergence must not prompt")

    result = push_and_merge(cfg, incus, "feat-foo", confirm=_confirm)

    assert "--ff-only" in _merge_cmd(incus)
    assert result.fast_forward_only is True


# ---------------------------------------------------------------------------
# tag policy threading (container -> host)
# ---------------------------------------------------------------------------


def _fetch_stub(mocker, tmp_path, make_cfg):
    """Wire enough mocks for fetch_from_container to reach git.fetch_url."""
    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    mocker.patch("jailbee.sync.assert_container_publishable", return_value="c-feat-foo")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/x")
    mocker.patch("jailbee.sync._resolve_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync._assert_container_has_branch")
    mocker.patch("jailbee.sync._build_ext_url", return_value="ext::x")
    mocker.patch("jailbee.git.rev_parse", return_value="abc1234")
    return cfg, incus


def test_fetch_from_container_forwards_the_tag_policy(mocker, tmp_path, make_cfg):
    from jailbee import sync

    cfg, incus = _fetch_stub(mocker, tmp_path, make_cfg)
    fetch_url = mocker.patch("jailbee.git.fetch_url")

    sync.fetch_from_container(cfg, incus, "feat-foo", tags="all")

    assert fetch_url.call_args.kwargs["tags"] == "all"


def test_fetch_from_container_defaults_to_reachable(mocker, tmp_path, make_cfg):
    from jailbee import sync

    cfg, incus = _fetch_stub(mocker, tmp_path, make_cfg)
    fetch_url = mocker.patch("jailbee.git.fetch_url")

    sync.fetch_from_container(cfg, incus, "feat-foo")

    assert fetch_url.call_args.kwargs["tags"] == "reachable"


def test_sync_refs_from_container_forwards_the_tag_policy(mocker, make_cfg, tmp_path):
    """`sync_refs_from_container` forwards `tags` to its own `fetch_from_container`
    call unconditionally — a dropped `tags=tags` there would silently fall back
    to the default and pass every other test, since the parameter has one.

    Reuses `_sync_refs_setup`, the existing mock surface for this function's
    happy path (`test_sync_refs_creates_the_host_branch_without_checking_it_out`
    is built on the same wiring).
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus, _ = _sync_refs_setup(mocker, cfg)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", return_value=None)  # branch absent
    mocker.patch("jailbee.sync.git.update_ref", return_value=True)
    mocker.patch("jailbee.sync.git.checkout_branch")
    mocker.patch("jailbee.submodules.place_branches_from_commit", return_value=[])

    sync.sync_refs_from_container(cfg, incus, "feat-foo", tags="all")

    assert sync.fetch_from_container.call_args.kwargs["tags"] == "all"


def test_merge_from_container_forwards_the_tag_policy(mocker, make_cfg, tmp_path):
    """`merge_from_container` forwards `tags` to its own `fetch_from_container`
    call unconditionally — same rationale as the `sync_refs_from_container`
    case above.

    Reuses the mock surface from `test_merge_from_container_updates_host_
    submodules`, the existing happy-path test for this function's in-place
    merge branch.
    """
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-x"
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch(
        "jailbee.sync.fetch_from_container",
        return_value=sync.FetchResult(
            branch="feat/x", old_oid=None, new_oid="new", base_oid="old", commits_added=1
        ),
    )
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mocker.patch("jailbee.sync.git.rev_parse", side_effect=["pre", "head"])
    mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    sync.merge_from_container(cfg, incus, "feat-x", tags="all")

    assert sync.fetch_from_container.call_args.kwargs["tags"] == "all"


def test_container_to_container_relay_carries_no_tags(mocker, make_cfg, tmp_path):
    """Decision 7: the host's tag set must not leak into a relay target.

    Behavioural, not a source-text check: `_merge_relay_wiring` already builds
    the full mock surface needed to reach `merge_container_into_container`'s
    own `fetch_from_container` call (`tests/test_cli_git_merge.py` cannot — it
    mocks `sync.merge_container_into_container` itself at the CLI boundary, so
    it never reaches the code under test here). Reusing that helper — instead
    of copying its body — means a future dependency `merge_container_into_
    container` picks up is covered here too, with no drift to keep in sync.
    """
    from jailbee import sync

    cfg, incus = _merge_relay_wiring(mocker, make_cfg, tmp_path)
    mocker.patch("jailbee.sync._merge_ref_in_container", return_value="mergedsha")

    sync.merge_container_into_container(cfg, incus, "c1", "c2")

    assert sync.fetch_from_container.call_args.kwargs["tags"] == "none"


def test_container_to_container_relay_push_leg_is_pinned_to_none(mocker, make_cfg, tmp_path):
    """Decision 7's other half: the push into the target is pinned too.

    `test_container_to_container_relay_carries_no_tags` above covers the fetch
    leg; `push_to_container`'s own default is also `"none"`, so an explicit
    pin here was previously unverified — this closes that gap and makes the
    "both legs are pinned" claim in `merge_container_into_container`'s
    Decision 7 comment literally checked, not just asserted in prose.
    """
    from jailbee import sync

    cfg, incus = _merge_relay_wiring(mocker, make_cfg, tmp_path)
    mocker.patch("jailbee.sync._merge_ref_in_container", return_value="mergedsha")

    sync.merge_container_into_container(cfg, incus, "c1", "c2")

    assert sync.push_to_container.call_args.kwargs["tags"] == "none"


def test_publish_to_origin_pushes_only_the_branch_refspec(mocker, make_cfg, tmp_path):
    """Decision 8: tags reach the host, never the GitHub origin.

    Behavioural, not a source-text check: `_stub_publish_fetch` (above) already
    builds the mock surface `publish_branch_from_container` needs to reach
    `git.push_to_remote` (`tests/test_cli_pr.py` cannot — it mocks
    `sync.publish_branch_from_container` itself at the CLI boundary). Passing
    `tags="all"` here proves the tag policy that reached the container-to-host
    fetch does not also reach the push to origin: `push_to_remote` takes no
    `tags` parameter at all, and `assert_called_once_with` fails on any extra
    argument, including one smuggled through as a second refspec.
    """
    from jailbee.sync import publish_branch_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.exec.return_value = ""  # status --porcelain -> clean
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{cfg.container_prefix}-feat-foo",
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    _stub_publish_fetch(mocker)
    push = mocker.patch("jailbee.sync.git.push_to_remote")

    publish_branch_from_container(cfg, incus, "feat-foo", tags="all")

    push.assert_called_once_with(
        cfg.repo_root, "origin", "refs/jailbee/feat-foo/feat/foo", "feat/foo", force_with_lease=None
    )


def _drive_push(mocker, tmp_path, make_cfg, *, tags="none"):
    """Call push_to_container with an explicit source_ref, which is the branch
    that skips origin/local resolution entirely — fewer mocks, same refspecs."""
    from jailbee import sync

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    # mode label -> not "mount"; base_branch label -> None, so the base-advance
    # refspec stays out of the way of the tag assertions.
    incus.config_get.return_value = None
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="myrepo-feat-a")
    mocker.patch("jailbee.sync._container_is_running", return_value=True)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/x")
    mocker.patch("jailbee.sync._container_ref_oid", return_value=None)
    mocker.patch("jailbee.sync._build_receive_url", return_value="ext::x")
    mocker.patch("jailbee.git.rev_parse", return_value="abc1234")
    return sync.push_to_container(
        cfg, incus, "feat-a", source="feat/a", source_ref="refs/heads/feat/a", tags=tags
    )


def test_push_to_container_sends_no_tag_refspec_by_default(mocker, tmp_path, make_cfg):

    push_multi = mocker.patch("jailbee.git.push_url_multi")
    push_one = mocker.patch("jailbee.git.push_url")
    _drive_push(mocker, tmp_path, make_cfg)

    assert not push_multi.called or all(
        "refs/tags/" not in spec for spec in push_multi.call_args[0][2]
    )
    if push_one.called:
        assert "refs/tags/" not in push_one.call_args[0][2]


def test_push_to_container_all_appends_the_wildcard_refspec(mocker, tmp_path, make_cfg):

    push_multi = mocker.patch("jailbee.git.push_url_multi")
    _drive_push(mocker, tmp_path, make_cfg, tags="all")

    assert "refs/tags/*:refs/tags/*" in push_multi.call_args[0][2]


def test_push_to_container_reachable_appends_one_refspec_per_tag(mocker, tmp_path, make_cfg):

    mocker.patch("jailbee.git.tags_reachable_from", return_value=["v1.0", "v1.1"])
    push_multi = mocker.patch("jailbee.git.push_url_multi")
    _drive_push(mocker, tmp_path, make_cfg, tags="reachable")

    specs = push_multi.call_args[0][2]
    assert "refs/tags/v1.0:refs/tags/v1.0" in specs
    assert "refs/tags/v1.1:refs/tags/v1.1" in specs


def test_push_to_container_never_forces_a_tag_refspec(mocker, tmp_path, make_cfg):
    """Decision 4: no tag refspec carries '+', in any policy."""

    mocker.patch("jailbee.git.tags_reachable_from", return_value=["v1.0"])
    push_multi = mocker.patch("jailbee.git.push_url_multi")
    _drive_push(mocker, tmp_path, make_cfg, tags="reachable")

    for spec in push_multi.call_args[0][2]:
        if "refs/tags/" in spec:
            assert not spec.startswith("+"), f"tag refspec must not be forced: {spec}"


def test_every_tag_policy_value_is_handled_in_both_transports(mocker, tmp_path, make_cfg):
    """A fourth TagPolicy value must not fall through either transport."""
    from typing import get_args

    from jailbee import sync
    from jailbee.config.models_behaviour import TagPolicy

    for policy in get_args(TagPolicy):
        fetch_url = mocker.patch("jailbee.git.fetch_url")
        cfg, incus = _fetch_stub(mocker, tmp_path, make_cfg)
        sync.fetch_from_container(cfg, incus, "feat-foo", tags=policy)
        assert fetch_url.call_args.kwargs["tags"] == policy

        mocker.patch("jailbee.git.tags_reachable_from", return_value=["v1.0"])
        push_multi = mocker.patch("jailbee.git.push_url_multi")
        push_one = mocker.patch("jailbee.git.push_url")
        _drive_push(mocker, tmp_path, make_cfg, tags=policy)
        specs = push_multi.call_args[0][2] if push_multi.called else [push_one.call_args[0][2]]
        has_tag_spec = any("refs/tags/" in spec for spec in specs)
        assert has_tag_spec is (policy != "none"), (
            f"policy {policy!r} produced tag refspecs={has_tag_spec}"
        )
