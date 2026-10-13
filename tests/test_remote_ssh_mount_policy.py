"""The predicate deciding which optional mounts a restricted session may attach."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.config.models_remote import RemoteMountRoot, RemoteSSHConfig
from jailbee.remote_ssh.mount_policy import (
    MountGrant,
    grantable_kinds,
    remote_mount_grant,
    writable_cover,
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    # Root the tree at tmp_path / "t" so make_cfg's container_prefix (tmp_path.name)
    # does not collide with mount roots, and the prefix is always valid.
    base = (tmp_path / "t").resolve()
    base.mkdir()
    (base / "data").mkdir()
    (base / "data" / "sub").mkdir()
    (base / "data-evil").mkdir()
    (base / "other").mkdir()
    return base


def _cfg(make_cfg, tmp_path: Path, mounts: dict[str, dict], **kw):  # type: ignore[no-untyped-def]
    return make_cfg(tmp_path, optional_mounts=mounts, **kw)


def _policy(*roots: RemoteMountRoot, **kw) -> RemoteSSHConfig:  # type: ignore[no-untyped-def]
    return RemoteSSHConfig(mount_roots=list(roots), **kw)


def test_exact_root_is_granted_readonly(tree, make_cfg, tmp_path) -> None:
    cfg = _cfg(make_cfg, tmp_path, {"d": {"host": str(tree / "data"), "container": "/d"}})
    grant = remote_mount_grant(_policy(RemoteMountRoot(path=tree / "data")), cfg, "d")
    assert grant == MountGrant(tree / "data", readonly=True, forced_readonly=False)


def test_readonly_root_forces_a_writable_mount_readonly(tree, make_cfg, tmp_path) -> None:
    mounts = {"d": {"host": str(tree / "data"), "container": "/d", "readonly": False}}
    cfg = _cfg(make_cfg, tmp_path, mounts)
    grant = remote_mount_grant(_policy(RemoteMountRoot(path=tree / "data")), cfg, "d")
    assert grant == MountGrant(tree / "data", readonly=True, forced_readonly=True)


def test_writable_root_keeps_the_mount_own_flag(tree, make_cfg, tmp_path) -> None:
    mounts = {"d": {"host": str(tree / "data"), "container": "/d", "readonly": False}}
    cfg = _cfg(make_cfg, tmp_path, mounts)
    root = RemoteMountRoot(path=tree / "data", readonly=False)
    assert remote_mount_grant(_policy(root), cfg, "d") == MountGrant(tree / "data", False, False)


@pytest.mark.parametrize("spelling", ["{t}/data/", "{t}/other/../data", "{t}/./data"])
def test_other_spellings_of_a_root_are_granted_as_the_root(
    tree, make_cfg, tmp_path, spelling
) -> None:
    cfg = _cfg(make_cfg, tmp_path, {"d": {"host": spelling.format(t=tree), "container": "/d"}})
    grant = remote_mount_grant(_policy(RemoteMountRoot(path=tree / "data")), cfg, "d")
    assert isinstance(grant, MountGrant) and grant.source == tree / "data"


def test_symlink_to_a_root_is_granted_with_the_root_as_source(tree, make_cfg, tmp_path) -> None:
    (tree / "alias").symlink_to(tree / "data")
    cfg = _cfg(make_cfg, tmp_path, {"d": {"host": str(tree / "alias"), "container": "/d"}})
    grant = remote_mount_grant(_policy(RemoteMountRoot(path=tree / "data")), cfg, "d")
    assert isinstance(grant, MountGrant) and grant.source == tree / "data"


def test_symlink_inside_a_root_pointing_out_is_refused(tree, make_cfg, tmp_path) -> None:
    (tree / "data" / "escape").symlink_to(tree / "other")
    host = str(tree / "data" / "escape")
    cfg = _cfg(make_cfg, tmp_path, {"d": {"host": host, "container": "/d"}})
    refusal = remote_mount_grant(_policy(RemoteMountRoot(path=tree / "data")), cfg, "d")
    assert isinstance(refusal, str) and "remote.ssh.mount_roots" in refusal
    assert str(tree / "other") in refusal  # names the resolved path


@pytest.mark.parametrize("host", ["data-evil", "data/sub", "other"])
def test_sibling_prefix_subdir_and_unlisted_paths_are_refused(
    tree, make_cfg, tmp_path, host
) -> None:
    cfg = _cfg(make_cfg, tmp_path, {"d": {"host": str(tree / host), "container": "/d"}})
    refusal = remote_mount_grant(_policy(RemoteMountRoot(path=tree / "data")), cfg, "d")
    assert isinstance(refusal, str)


def test_repos_scope_refuses_another_repo(tree, make_cfg, tmp_path) -> None:
    cfg = _cfg(make_cfg, tmp_path, {"d": {"host": str(tree / "data"), "container": "/d"}})
    other = RemoteMountRoot(path=tree / "data", repos=["someone-else"])
    refusal = remote_mount_grant(_policy(other), cfg, "d")
    assert isinstance(refusal, str) and cfg.container_prefix in refusal
    # Use a valid explicit prefix (tmp dirs may not match _PREFIX_RE)
    mine = RemoteMountRoot(path=tree / "data", repos=["testrepo"])
    cfg2 = _cfg(
        make_cfg,
        tmp_path,
        {"d": {"host": str(tree / "data"), "container": "/d"}},
        container_prefix="testrepo",
    )
    assert isinstance(remote_mount_grant(_policy(mine), cfg2, "d"), MountGrant)


def test_excluded_repo_is_refused(tree, make_cfg, tmp_path) -> None:
    # Use an explicit valid prefix; tmp dirs may contain invalid characters
    cfg = _cfg(
        make_cfg,
        tmp_path,
        {"d": {"host": str(tree / "data"), "container": "/d"}},
        container_prefix="testrepo",
    )
    policy = _policy(RemoteMountRoot(path=tree / "data"), excluded_repos=["testrepo"])
    assert isinstance(remote_mount_grant(policy, cfg, "d"), str)


def test_missing_root_directory_is_refused(tree, make_cfg, tmp_path) -> None:
    gone = tree / "gone"
    cfg = _cfg(make_cfg, tmp_path, {"d": {"host": str(gone), "container": "/d"}})
    refusal = remote_mount_grant(_policy(RemoteMountRoot(path=gone)), cfg, "d")
    assert isinstance(refusal, str) and "does not exist" in refusal


def test_unknown_kind_is_refused(tree, make_cfg, tmp_path) -> None:
    cfg = _cfg(make_cfg, tmp_path, {})
    assert isinstance(remote_mount_grant(_policy(), cfg, "nope"), str)


@pytest.mark.parametrize("cover", ["data", ""])  # the root itself, and its parent
def test_writable_host_mount_covering_the_root_is_refused(tree, make_cfg, tmp_path, cover) -> None:
    cfg = _cfg(
        make_cfg,
        tmp_path,
        {"d": {"host": str(tree / "data"), "container": "/d"}},
        host_mounts=[{"host": str(tree / cover), "container": "/w"}],
    )
    refusal = remote_mount_grant(_policy(RemoteMountRoot(path=tree / "data")), cfg, "d")
    assert isinstance(refusal, str) and "host_mounts" in refusal


def test_writable_optional_mount_on_the_parent_is_refused(tree, make_cfg, tmp_path) -> None:
    mounts = {
        "d": {"host": str(tree / "data"), "container": "/d"},
        "w": {"host": str(tree), "container": "/w", "readonly": False},
    }
    cfg = _cfg(make_cfg, tmp_path, mounts)
    refusal = remote_mount_grant(_policy(RemoteMountRoot(path=tree / "data")), cfg, "d")
    assert isinstance(refusal, str) and "optional_mounts w" in refusal


def test_writable_optional_mount_on_the_same_path_refuses(tree, make_cfg, tmp_path) -> None:
    # A different read-write optional mount on the exact same host path still refuses
    mounts = {
        "d": {"host": str(tree / "data"), "container": "/d"},
        "w": {"host": str(tree / "data"), "container": "/w", "readonly": False},
    }
    cfg = _cfg(make_cfg, tmp_path, mounts)
    refusal = remote_mount_grant(_policy(RemoteMountRoot(path=tree / "data")), cfg, "d")
    assert isinstance(refusal, str) and "optional_mounts w" in refusal


def test_readonly_mounts_on_the_parent_do_not_block(tree, make_cfg, tmp_path) -> None:
    mounts = {
        "d": {"host": str(tree / "data"), "container": "/d"},
        "r": {"host": str(tree), "container": "/r"},  # optional mounts default readonly
    }
    cfg = _cfg(
        make_cfg,
        tmp_path,
        mounts,
        host_mounts=[{"host": str(tree), "container": "/h", "readonly": True}],
    )
    grant = remote_mount_grant(_policy(RemoteMountRoot(path=tree / "data")), cfg, "d")
    assert isinstance(grant, MountGrant)


def test_mounts_switch_grants_anything_as_defined(tree, make_cfg, tmp_path) -> None:
    mounts = {"x": {"host": str(tree / "other"), "container": "/x", "readonly": False}}
    cfg = _cfg(make_cfg, tmp_path, mounts)
    grant = remote_mount_grant(RemoteSSHConfig(mounts=True), cfg, "x")
    assert grant == MountGrant(tree / "other", readonly=False, forced_readonly=False)


def test_grantable_kinds_keeps_config_order(tree, make_cfg, tmp_path) -> None:
    mounts = {
        "b": {"host": str(tree / "data"), "container": "/b"},
        "no": {"host": str(tree / "other"), "container": "/n"},
        "a": {"host": str(tree / "data"), "container": "/a"},
    }
    cfg = _cfg(make_cfg, tmp_path, mounts)
    assert grantable_kinds(_policy(RemoteMountRoot(path=tree / "data")), cfg) == ("b", "a")


def test_writable_cover_reports_none_when_clear(tree, make_cfg, tmp_path) -> None:
    cfg = _cfg(make_cfg, tmp_path, {})
    assert writable_cover(cfg, tree / "data") is None
