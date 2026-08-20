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


def test_checkout_creates_new_branch_with_origin_tracking(mocker, make_cfg, tmp_path):
    from jailbee.sync import checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mock_create = mocker.patch("jailbee.sync.git.create_branch")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    checkout_from_container(cfg, incus, "feat-foo")

    mock_create.assert_called_once_with(
        cfg.repo_root,
        "feat/foo",
        start_point="refs/jailbee/feat-foo/feat/foo",
        track="origin/feat/foo",
    )


def test_checkout_creates_new_branch_without_tracking_when_origin_missing(
    mocker, make_cfg, tmp_path
):
    from jailbee.sync import checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mock_create = mocker.patch("jailbee.sync.git.create_branch")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    checkout_from_container(cfg, incus, "feat-foo")

    mock_create.assert_called_once_with(
        cfg.repo_root,
        "feat/foo",
        start_point="refs/jailbee/feat-foo/feat/foo",
        track=None,
    )


def test_checkout_existing_branch_already_current_ff(mocker, make_cfg, tmp_path):
    from jailbee.sync import checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mock_checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    mock_merge = mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    checkout_from_container(cfg, incus, "feat-foo")

    mock_checkout.assert_not_called()
    mock_merge.assert_called_once_with(
        cfg.repo_root,
        "refs/jailbee/feat-foo/feat/foo",
        message=None,
        no_ff=False,
        ff_only=True,
    )


def test_checkout_existing_branch_switches_then_ff(mocker, make_cfg, tmp_path):
    from jailbee.sync import checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="main")
    mock_checkout = mocker.patch("jailbee.sync.git.checkout_branch")
    mock_merge = mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    checkout_from_container(cfg, incus, "feat-foo")

    mock_checkout.assert_called_once_with(cfg.repo_root, "feat/foo")
    mock_merge.assert_called_once_with(
        cfg.repo_root,
        "refs/jailbee/feat-foo/feat/foo",
        message=None,
        no_ff=False,
        ff_only=True,
    )


def test_checkout_places_submodules_on_target_branch(mocker, make_cfg, tmp_path):
    from jailbee.sync import checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=False)
    mocker.patch("jailbee.sync.git.create_branch")
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    checkout_from_container(cfg, incus, "feat-foo")

    upd.assert_called_once_with(cfg.repo_root, branch="feat/foo")


def test_checkout_diverged_raises_with_hint(mocker, make_cfg, tmp_path):
    from jailbee.git import GitError
    from jailbee.sync import SyncError, checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch(
        "jailbee.sync.git.merge_ref",
        side_effect=GitError("Not possible to fast-forward"),
    )

    with pytest.raises(SyncError) as exc:
        checkout_from_container(cfg, incus, "feat-foo")
    msg = str(exc.value)
    assert "diverged" in msg.lower()
    assert "jailbee git pull feat-foo" in msg


def test_checkout_returns_checkout_result_new_branch(mocker, make_cfg, tmp_path):
    from jailbee.sync import CheckoutResult, checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker, head_oid="def5678defg")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.sync.git.create_branch")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    result = checkout_from_container(cfg, incus, "feat-foo")

    assert isinstance(result, CheckoutResult)
    assert result.branch == "feat/foo"
    assert result.head_oid == "def5678defg"
    assert result.created_new is True
    assert result.fetch.commits_added == 2


def test_checkout_returns_checkout_result_existing_branch(mocker, make_cfg, tmp_path):
    from jailbee.sync import CheckoutResult, checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker, head_oid="def5678defg")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.merge_ref")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    result = checkout_from_container(cfg, incus, "feat-foo")

    assert isinstance(result, CheckoutResult)
    assert result.branch == "feat/foo"
    assert result.head_oid == "def5678defg"
    assert result.created_new is False


def test_checkout_uses_pr_branch_label_for_host_name(mocker, make_cfg, tmp_path):
    from jailbee import sync
    from jailbee.sync import FetchResult

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: (
        "user/nice" if key == "user.jailbee.pr_branch" else None
    )
    fetch = FetchResult(branch="dev-1", old_oid=None, new_oid="n", base_oid=None, commits_added=1)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=fetch)
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-dev-1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.submodules.update_submodules_on_host")
    mocker.patch("jailbee.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.git.remote_ref_exists", return_value=False)
    created = mocker.patch("jailbee.git.create_branch")
    mocker.patch("jailbee.git.rev_parse", return_value="headoid")

    result = sync.checkout_from_container(cfg, incus, "dev-1")

    assert result.branch == "user/nice"
    assert created.call_args.args[1] == "user/nice"  # branch name arg
    # fetched ref still keyed on the CONTAINER branch:
    assert created.call_args.kwargs["start_point"] == "refs/jailbee/dev-1/dev-1"


def _wire_checkout_host(mocker, *, local_exists: bool, remote_exists: bool = False):
    """Stub the host-side git calls `checkout_from_container` makes."""
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value="p-dev-1")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/repo")
    mocker.patch("jailbee.submodules.transport_submodules_to_host")
    mocker.patch("jailbee.submodules.update_submodules_on_host")
    mocker.patch("jailbee.git.local_branch_exists", return_value=local_exists)
    mocker.patch("jailbee.git.remote_ref_exists", return_value=remote_exists)
    mocker.patch("jailbee.git.rev_parse", return_value="headoid")


def test_checkout_as_name_renames_the_host_branch(mocker, make_cfg, tmp_path):
    """`--as` names the HOST branch; the container branch still decides what
    is fetched. Without it there is no way to land a container branch under a
    different name on the host (the reported `-b` confusion).
    """
    from jailbee.sync import FetchResult

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    fetch = FetchResult(
        branch="compose-4", old_oid=None, new_oid="n", base_oid=None, commits_added=1
    )
    mocker.patch("jailbee.sync.fetch_from_container", return_value=fetch)
    _wire_checkout_host(mocker, local_exists=False)
    created = mocker.patch("jailbee.git.create_branch")

    result = sync.checkout_from_container(cfg, incus, "compose-4", as_name="compose-4-1")

    assert result.branch == "compose-4-1"
    assert created.call_args.args[1] == "compose-4-1"
    assert created.call_args.kwargs["start_point"] == "refs/jailbee/compose-4/compose-4"


def test_checkout_as_name_wins_over_the_pr_branch_label(mocker, make_cfg, tmp_path):
    """An explicit `--as` outranks the container's `user.jailbee.pr_branch` label —
    the user asked for that name by hand.
    """
    from jailbee.sync import FetchResult

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda _n, key: (
        "pr-123-head" if key == "user.jailbee.pr_branch" else None
    )
    fetch = FetchResult(branch="dev-1", old_oid=None, new_oid="n", base_oid=None, commits_added=1)
    mocker.patch("jailbee.sync.fetch_from_container", return_value=fetch)
    _wire_checkout_host(mocker, local_exists=False)
    created = mocker.patch("jailbee.git.create_branch")

    result = sync.checkout_from_container(cfg, incus, "dev-1", as_name="local-review")

    assert result.branch == "local-review"
    assert created.call_args.args[1] == "local-review"


def test_checkout_as_name_fast_forwards_an_existing_host_branch(mocker, make_cfg, tmp_path):
    """When the `--as` branch already exists, it is checked out and ff'd from
    the container ref — same contract as the auto-named path.
    """
    from jailbee.sync import FetchResult

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    fetch = FetchResult(
        branch="compose-4", old_oid=None, new_oid="n", base_oid=None, commits_added=1
    )
    mocker.patch("jailbee.sync.fetch_from_container", return_value=fetch)
    _wire_checkout_host(mocker, local_exists=True)
    mocker.patch("jailbee.git.get_current_branch", return_value="main")
    checkout = mocker.patch("jailbee.git.checkout_branch")
    merge = mocker.patch("jailbee.git.merge_ref")

    result = sync.checkout_from_container(cfg, incus, "compose-4", as_name="compose-4-1")

    assert result.branch == "compose-4-1"
    assert result.created_new is False
    checkout.assert_called_once_with(cfg.repo_root, "compose-4-1")
    assert merge.call_args.args[1] == "refs/jailbee/compose-4/compose-4"


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

    merge_from_container(cfg, incus, "feat-foo")

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

    merge_from_container(cfg, incus, "feat-foo", ff_only=True)

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
        merge_from_container(cfg, incus, "feat-foo", ff_only=True)


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

    merge_from_container(cfg, incus, "feat-foo", ff_only=True)
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
    rev_parse_gie, rev_parse_head, rev_list_count, merge, rebase, reset.
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
        elif "rev-parse" in joined and "refs/jailbee/host" in joined:
            key = "rev_parse_gie"
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


def test_checkout_new_branch_updates_host_submodules(mocker, make_cfg, tmp_path):
    """New-branch path of checkout_from_container calls update_submodules_on_host."""
    from jailbee.sync import checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.sync.git.create_branch")
    upd = mocker.patch("jailbee.sync.submodules.update_submodules_on_host")

    checkout_from_container(cfg, incus, "feat-foo")

    upd.assert_called_once_with(cfg.repo_root, branch="feat/foo")


def test_checkout_existing_branch_updates_host_submodules(mocker, make_cfg, tmp_path):
    """Existing-branch ff path of checkout_from_container calls update_submodules_on_host."""
    from jailbee.sync import checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    _stub_fetch(mocker)
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=True)
    mocker.patch("jailbee.sync.git.get_current_branch", return_value="feat/foo")
    mocker.patch("jailbee.sync.git.merge_ref")
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


def test_checkout_from_container_transports_submodules(mocker, make_cfg, tmp_path):
    """checkout_from_container calls transport_submodules_to_host before updating."""
    from jailbee.sync import checkout_from_container

    cfg = make_cfg(tmp_path)
    incus = mocker.MagicMock()
    full = f"{cfg.container_prefix}-feat-foo"

    _stub_fetch(mocker, branch="feat/foo", short="feat-foo")
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full)
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.sync.git.local_branch_exists", return_value=False)
    mocker.patch("jailbee.sync.git.remote_ref_exists", return_value=True)
    mocker.patch("jailbee.sync.git.create_branch")
    mocker.patch("jailbee.sync.submodules.update_submodules_on_host")
    tr = mocker.patch("jailbee.sync.submodules.transport_submodules_to_host")

    checkout_from_container(cfg, incus, "feat-foo")

    tr.assert_called_once_with(cfg, incus, full, "feat-foo", repo_dir="/home/dev/repo")


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

    merge_from_container(cfg, incus, "feat-foo", ff_only=True)

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
        ff_only=False,
        into=None,
        allow_checkout=False,
        destroy_policy="never",
        branch_policy="never",
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

    plan = sync.plan_pull(cfg, incus, "feat-foo", branch=None, into=None, ff_only=False)

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

    plan = sync.plan_pull(cfg, incus, "feat-foo", branch=None, into="develop", ff_only=True)

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

    plan = sync.plan_pull(cfg, incus, "feat-foo", branch="other", into=None, ff_only=False)

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

    plan = sync.plan_pull(cfg, incus, "feat-foo", branch=None, into=None, ff_only=False)

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
