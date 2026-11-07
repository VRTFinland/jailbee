"""The PR count of `jb ls`, the dashboards and the destroy guard reads the host store."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from jailbee.destroy_guard import assess
from jailbee.git_status import GitStatus
from jailbee.lifecycle import list_containers, stamp_pr_outbox
from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox.publication_db import PublicationStore
from jailbee.outbox.store_db import OutboxStore
from jailbee.outbox_io import proposal_digest
from tests.test_lifecycle import _container

MANIFEST = json.dumps(
    {
        "version": 1,
        "repo": "acme/widgets",
        "pr": 3,
        "head_sha": None,
        "actions": [{"type": "comment", "body": "x"}],
    }
)


def _stage(root, uuid: str, count: int) -> list[str]:
    store = OutboxStore(BrokerStore(store_path(root)))
    return [
        store.put_proposal(uuid, "pr", slug, lambda stem: (MANIFEST, {}), now=1.0).name
        for slug in "abcdef"[:count]
    ]


def _publish(root, uuid: str, name: str) -> None:
    """Journal the proposal's only action as applied, leaving the row (a cleanup
    that did not finish): it is finished, so no longer pending."""
    pubs = PublicationStore(BrokerStore(store_path(root)))
    digest = proposal_digest(name, MANIFEST, {})
    with pubs.claim(uuid, "pr", name) as claim:
        pubs.begin(claim, digest=digest, action_count=1, container_full="c", now=1.0)
        pubs.mark_prepared(claim, digest, 0, repo="acme/widgets", now=1.0)
    # Settle without finalizing: mark applied straight in the journal table.
    with BrokerStore(store_path(root)).transaction() as con:
        con.execute("UPDATE outbox_journal_action SET state='applied', url='u'")


def test_listing_reads_pr_counts_from_the_store_journal_aware(make_cfg, tmp_path, mocker):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    root = tmp_path / "state" / "orchestration"
    root.mkdir(parents=True)
    names = _stage(root, "a", 3)
    _publish(root, "a", names[0])
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
    probed = GitStatus("?", "?", "?", "?", pending_pr_actions=9)
    mocker.patch(
        "jailbee.lifecycle.probe_many_parallel",
        return_value={"myrepo-a": probed, "myrepo-b": probed},
    )
    rows = {c.name: c for c in list_containers(cfg, incus, with_git_status=True)}
    assert rows["myrepo-a"].git_status.pending_pr_actions == 2
    assert rows["myrepo-b"].git_status.pending_pr_actions == 0


def test_the_destroy_guard_warns_about_store_pr_proposals(tmp_path, mocker, make_cfg):
    root = tmp_path / "state" / "orchestration"
    root.mkdir(parents=True)
    _stage(root, "a", 2)
    mocker.patch("jailbee.db.state_dir", return_value=root.parent)
    ci = MagicMock()
    ci.uuid = "a"
    ci.display_name = "a"
    ci.git_status = GitStatus("clean", "0", "0", "0", pending_pr_actions=0)
    stamp_pr_outbox(ci)
    summary = assess(make_cfg(tmp_path), ci)
    assert summary is not None
    assert "2 unapplied PR actions" in summary.line


def test_an_unreadable_store_leaves_the_pr_count_unknown(tmp_path, mocker):
    mocker.patch("jailbee.db.state_dir", return_value=tmp_path)
    mocker.patch("jailbee.outbox.store_db.read_pending_counts", side_effect=OSError("locked"))
    ci = MagicMock()
    ci.uuid = "a"
    ci.git_status = GitStatus("clean", "0", "0", "0", pending_pr_actions=4)
    stamp_pr_outbox(ci)
    assert ci.git_status.pending_pr_actions is None
