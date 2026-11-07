"""The ISSUES count and the destroy guard read the host store, not the git probe."""

from __future__ import annotations

from unittest.mock import MagicMock

from jailbee.destroy_guard import assess
from jailbee.git_status import GitStatus
from jailbee.lifecycle import list_containers, stamp_issue_outbox
from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox.store_db import OutboxStore
from tests.test_lifecycle import _container


def _build(stem: str) -> tuple[str, dict[str, str]]:
    return '{"version":1,"actions":[]}', {}


def _stage(root, uuid: str, count: int) -> None:
    store = OutboxStore(BrokerStore(store_path(root)))
    for slug in "abcdef"[:count]:
        store.put_proposal(uuid, "issue", slug, _build, now=1.0)


def test_listing_overrides_the_probed_issue_count_with_the_store(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    root = tmp_path / "state" / "orchestration"
    root.mkdir(parents=True)
    _stage(root, "a", 2)
    mocker.patch("jailbee.db.state_dir", return_value=root.parent)
    incus = MagicMock()
    incus.list_containers.return_value = [
        _container(
            name="myrepo-a", user_config={"user.jailbee.repo_dir": "/repo", "volatile.uuid": "a"}
        ),
        _container(
            name="myrepo-b", user_config={"user.jailbee.repo_dir": "/repo", "volatile.uuid": "b"}
        ),
    ]
    probed = GitStatus("?", "?", "?", "?", pending_issue_actions=9)
    mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={"myrepo-a": probed, "myrepo-b": probed},
    )
    rows = {c.name: c for c in list_containers(cfg, incus, with_git_status=True)}
    assert rows["myrepo-a"].git_status.pending_issue_actions == 2
    assert rows["myrepo-b"].git_status.pending_issue_actions == 0
    assert rows["myrepo-a"].uuid == "a"


def test_the_destroy_guard_warns_from_the_store(tmp_path, mocker, make_cfg):
    root = tmp_path / "state" / "orchestration"
    root.mkdir(parents=True)
    _stage(root, "a", 3)
    mocker.patch("jailbee.db.state_dir", return_value=root.parent)
    ci = MagicMock()
    ci.uuid = "a"
    ci.display_name = "a"
    ci.git_status = GitStatus("clean", "0", "0", "0", pending_issue_actions=0)
    stamp_issue_outbox(ci)
    summary = assess(make_cfg(tmp_path), ci)
    assert summary is not None
    assert "3 unapplied issue actions" in summary.line


def test_an_unreadable_store_leaves_the_count_unknown(tmp_path, mocker):
    mocker.patch("jailbee.db.state_dir", return_value=tmp_path)
    mocker.patch(
        "jailbee.outbox.store_db.read_pending_counts",
        side_effect=OSError("locked"),
    )
    ci = MagicMock()
    ci.uuid = "a"
    ci.git_status = GitStatus("clean", "0", "0", "0", pending_issue_actions=4)
    stamp_issue_outbox(ci)
    assert ci.git_status.pending_issue_actions is None
