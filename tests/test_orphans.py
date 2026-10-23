"""Tests for `jailbee.orphans`: containers whose repo can no longer be located."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from jailbee import orphans
from jailbee.incus import IncusError
from jailbee.lifecycle import ContainerInfo


def _ci(name: str, repo: str, state: str = "Running") -> ContainerInfo:
    return ContainerInfo(name, state, "strict", None, None, repo=repo)


def test_covered_prefixes_skips_unloadable_roots(tmp_path, mocker, make_cfg):
    good = make_cfg(tmp_path / "alpha")
    bad_root = tmp_path / "broken"

    def fake_load(root):
        if root == bad_root:
            raise ValueError("bad yaml")
        return good

    mocker.patch.object(orphans, "load_repo_config", side_effect=fake_load)
    assert orphans.covered_prefixes([tmp_path / "alpha", bad_root]) == {good.container_prefix}


def test_list_orphans_drops_covered_and_own_prefix(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "own")
    mocker.patch.object(orphans, "covered_prefixes", return_value={"alpha"})
    mocker.patch.object(
        orphans,
        "list_containers",
        return_value=[
            _ci("alpha-one", "alpha"),
            _ci(f"{cfg.container_prefix}-x", cfg.container_prefix),
            _ci("gamma-x", "gamma"),
        ],
    )
    got = orphans.list_orphans(cfg, MagicMock(), [tmp_path / "alpha"])
    assert [c.name for c in got] == ["gamma-x"]


def test_resolve_orphan_rejects_a_container_with_a_repo(tmp_path, mocker, make_cfg):
    cfg = make_cfg(tmp_path / "own")
    mocker.patch.object(orphans, "list_orphans", return_value=[_ci("gamma-x", "gamma")])
    mocker.patch.object(orphans, "list_containers", return_value=[_ci("alpha-one", "alpha")])
    with pytest.raises(ValueError, match="belongs to repo 'alpha'"):
        orphans.resolve_orphan(cfg, MagicMock(), [], "alpha-one")
    with pytest.raises(ValueError, match="No jailbee container named 'nope'"):
        orphans.resolve_orphan(cfg, MagicMock(), [], "nope")
    assert orphans.resolve_orphan(cfg, MagicMock(), [], "gamma-x").name == "gamma-x"


def test_container_ids_reads_the_dev_user_inside():
    incus = MagicMock()
    incus.exec.side_effect = ["1234\n", "4321\n"]
    assert orphans.container_ids(incus, "gamma-x") == (1234, 4321)
    incus.exec.assert_any_call("gamma-x", ["id", "-u", "dev"])
    incus.exec.assert_any_call("gamma-x", ["id", "-g", "dev"])


def test_container_ids_falls_back_to_host_ids():
    incus = MagicMock()
    incus.exec.side_effect = IncusError("no such user")
    assert orphans.container_ids(incus, "gamma-x") == (os.getuid(), os.getgid())


def test_attach_tmux_uses_the_repo_dir_label(mocker):
    incus = MagicMock()
    incus.config_get.return_value = "/home/dev/gamma"
    incus.exec_interactive.return_value = 0
    mocker.patch.object(orphans, "container_ids", return_value=(1000, 1000))
    ensure = mocker.patch("jailbee.tmux.ensure_session")
    assert orphans.attach_tmux(incus, "gamma-x") == 0
    ensure.assert_called_once_with(incus, "gamma-x", start_dir="/home/dev/gamma")
    args, kwargs = incus.exec_interactive.call_args
    assert args == ("gamma-x", ["tmux", "attach", "-t", "autostart"])
    assert kwargs["uid"] == 1000 and kwargs["init_groups"] is True


def test_attach_shell_falls_back_to_home_without_label(mocker):
    incus = MagicMock()
    incus.config_get.return_value = None
    incus.exec_interactive.return_value = 0
    mocker.patch.object(orphans, "container_ids", return_value=(1000, 1000))
    orphans.attach_shell(incus, "gamma-x", "dev")
    args, kwargs = incus.exec_interactive.call_args
    assert "cd /home/dev 2>/dev/null" in args[1][2]
    assert kwargs["uid"] == 1000


def test_attach_shell_as_root(mocker):
    incus = MagicMock()
    incus.config_get.return_value = None
    ids = mocker.patch.object(orphans, "container_ids")
    orphans.attach_shell(incus, "gamma-x", "root")
    ids.assert_not_called()
    assert incus.exec_interactive.call_args.kwargs["uid"] == 0


def test_destroy_orphan_stops_deletes_and_cleans(mocker):
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "gamma-x", "status": "Running"}]
    stop = mocker.patch("jailbee.stopping.stop_container")
    drop = mocker.patch("jailbee.egress_scope.drop_container_acl")
    job = mocker.patch.object(orphans, "_delete_job_row")
    orphans.destroy_orphan(incus, "gamma-x", force=False)
    stop.assert_called_once()
    incus.delete.assert_called_once_with("gamma-x", force=False)
    drop.assert_called_once_with(incus, "gamma-x")
    job.assert_called_once_with("gamma-x")


def test_destroy_orphan_skips_stop_when_stopped_and_survives_acl_failure(mocker):
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "gamma-x", "status": "Stopped"}]
    stop = mocker.patch("jailbee.stopping.stop_container")
    mocker.patch("jailbee.egress_scope.drop_container_acl", side_effect=IncusError("busy"))
    mocker.patch.object(orphans, "_delete_job_row")
    orphans.destroy_orphan(incus, "gamma-x", force=True)
    stop.assert_not_called()
    incus.delete.assert_called_once_with("gamma-x", force=True)


def test_destroy_orphan_never_touches_a_repo(mocker):
    """No transfer-ref pruning: an orphan has no repo, and guessing one could
    delete refs in an unrelated repo with a matching short name."""
    incus = MagicMock()
    incus.list_containers.return_value = [{"name": "gamma-x", "status": "Stopped"}]
    mocker.patch("jailbee.egress_scope.drop_container_acl")
    mocker.patch.object(orphans, "_delete_job_row")
    refs = mocker.patch("jailbee.git.list_refs")
    orphans.destroy_orphan(incus, "gamma-x", force=True)
    refs.assert_not_called()
