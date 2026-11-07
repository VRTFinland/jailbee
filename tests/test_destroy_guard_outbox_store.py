"""The destroy guard counts staged proposals from the host store for every container.

The outbox data is host-side, so a stopped or mount-mode container (whose
git state is never probed) must still warn before a destroy deletes it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from jailbee.destroy_guard import assess
from jailbee.lifecycle import ContainerInfo, list_containers, stamp_issue_outbox, stamp_pr_outbox
from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox.store_db import OutboxStore
from tests.test_lifecycle import _container


def _build(stem: str) -> tuple[str, dict[str, str]]:
    return '{"version":1,"actions":[]}', {}


def _stage(root, uuid: str, count: int) -> None:
    store = OutboxStore(BrokerStore(store_path(root)))
    for slug in "abcdef"[:count]:
        store.put_proposal(uuid, "issue", slug, _build, now=1.0)


def _store(tmp_path, mocker, uuid: str, count: int):
    root = tmp_path / "state" / "orchestration"
    root.mkdir(parents=True)
    _stage(root, uuid, count)
    mocker.patch("jailbee.db.state_dir", return_value=root.parent)


def _info(*, state: str = "Stopped", mode: str | None = None, uuid: str = "a") -> ContainerInfo:
    return ContainerInfo(
        name="myrepo-a",
        state=state,
        network="strict",
        ip=None,
        memory_limit=None,
        repo="myrepo",
        mode=mode,
        uuid=uuid,
    )


def test_stamping_a_never_probed_container_reads_the_store(tmp_path, mocker, make_cfg):
    _store(tmp_path, mocker, "a", 2)
    ci = _info()
    stamp_issue_outbox(ci)
    stamp_pr_outbox(ci)
    assert ci.git_status is None  # git state stays unknown
    summary = assess(make_cfg(tmp_path), ci)
    assert summary is not None
    assert summary.reasons == ("2 unapplied issue actions",)


def test_listing_counts_a_stopped_container_from_the_store(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    _store(tmp_path, mocker, "a", 3)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-a",
            state="Stopped",
            user_config={"user.jailbee.repo_dir": "/repo", "volatile.uuid": "a"},
        ),
    ]
    row = list_containers(cfg, incus, with_git_status=True)[0]
    assert row.git_status is None
    summary = assess(cfg, row)
    assert summary is not None
    assert "3 unapplied issue actions" in summary.line


def test_post_merge_cleanup_guard_warns_for_a_stopped_container(mocker, make_cfg, tmp_path):
    from jailbee.sync import _warn_before_container_destroy

    cfg = make_cfg(tmp_path)
    _store(tmp_path, mocker, "a", 2)
    full_name = f"{cfg.container_prefix}-feat-foo"
    ci = _info()
    ci.name = full_name
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[ci])
    confirm = mocker.patch("jailbee.tui.confirm_destroy_risk", return_value=False)

    assert not _warn_before_container_destroy(cfg, MagicMock(), full_name, "feat-foo")
    summaries = confirm.call_args.args[1]
    assert [s.reasons for s in summaries] == [("2 unapplied issue actions",)]


def test_destroy_cli_guard_warns_for_a_mount_mode_container(mocker, make_cfg, tmp_path):
    from typer.testing import CliRunner

    from jailbee.cli import app

    cfg = make_cfg(tmp_path)
    _store(tmp_path, mocker, "a", 2)
    full_name = f"{cfg.container_prefix}-feat-foo"
    ci = _info(state="Running", mode="mount")
    ci.name = full_name
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.lifecycle.resolve_container_name", return_value=full_name)
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[ci])
    destroy = mocker.patch("jailbee.lifecycle.destroy_container")
    warn = mocker.patch("jailbee.cli._warn_before_destroy", return_value=False)

    CliRunner().invoke(app, ["destroy", "feat-foo"])

    infos = warn.call_args.args[1]
    assert assess(cfg, infos[0]) is not None
    destroy.assert_not_called()
