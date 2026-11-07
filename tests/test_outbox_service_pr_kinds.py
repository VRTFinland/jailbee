"""`jb outbox` views store-held PR review/comment proposals with their journals."""

from __future__ import annotations

import json

from jailbee.db import state_dir
from jailbee.outbox import publication_db, service, store_db
from jailbee.outbox.delete import DeleteSelection, plan_delete
from jailbee.outbox.models import ProposalId
from jailbee.outbox_io import JournalStore, proposal_digest
from tests.outbox_support import IDENTITY

UUID = "uuid-1"
MANIFEST = {
    "version": 1,
    "repo": "acme/widgets",
    "pr": 12,
    "head_sha": "a" * 40,
    "actions": [
        {"type": "comment", "body": "First"},
        {"type": "reply", "comment_id": 7, "body_file": "x.md"},
    ],
}


def _env(mocker, make_cfg, tmp_path):
    incus = mocker.Mock()
    incus.list_containers.return_value = [
        {
            "name": IDENTITY.full_name,
            "created_at": IDENTITY.created_at,
            "config": {"volatile.uuid": UUID},
        }
    ]
    return make_cfg(tmp_path), incus, JournalStore(tmp_path / "j")


def _stage() -> store_db.Proposal:
    def build(stem: str) -> tuple[str, dict[str, str]]:
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["actions"][1]["body_file"] = f"{stem}-1.md"
        return json.dumps(manifest), {f"{stem}-1.md": "Reply text"}

    root = state_dir() / "orchestration"
    return store_db.host_store(root).put_proposal(UUID, "pr", "batch", build, now=1.0)


def _view(cfg, incus, journals):
    return service.load_container(cfg, incus, IDENTITY.full_name, journal_store=journals)


def _journal(row: store_db.Proposal, *, applied: bool) -> None:
    digest = proposal_digest(row.name, row.manifest, row.bodies)
    pubs = publication_db.open_publications()
    with pubs.claim(UUID, "pr", row.name) as claim:
        pubs.begin(claim, digest=digest, action_count=2, container_full="c", now=1.0)
        pubs.mark_prepared(claim, digest, 0, repo="acme/widgets", now=1.0)
        if applied:
            pubs.settle(claim, digest, 0, outcome="applied", url="https://x/1", now=2.0)


def test_a_store_held_pr_comment_proposal_is_pending(mocker, make_cfg, tmp_path):
    cfg, incus, journals = _env(mocker, make_cfg, tmp_path)
    row = _stage()
    view = _view(cfg, incus, journals)
    assert view.available, view.error
    (proposal,) = view.proposals
    assert (str(proposal.id), proposal.state) == ("pr/001-batch.json", "pending")
    assert [a.body for a in proposal.actions] == ["First", "Reply text"]
    assert proposal.edit_block is None
    assert row.name == "001-batch.json"
    incus.exec.assert_not_called()


def test_journaled_progress_shows_partial_with_its_receipt(mocker, make_cfg, tmp_path):
    cfg, incus, journals = _env(mocker, make_cfg, tmp_path)
    _journal(_stage(), applied=True)
    (proposal,) = _view(cfg, incus, journals).proposals
    assert proposal.state == "partial"
    assert [(a.state, a.receipt) for a in proposal.actions] == [
        ("applied", "https://x/1"),
        ("pending", None),
    ]
    assert proposal.edit_block == "recorded publication progress prevents editing"


def test_a_prepared_action_reads_as_uncertain(mocker, make_cfg, tmp_path):
    cfg, incus, journals = _env(mocker, make_cfg, tmp_path)
    _journal(_stage(), applied=False)
    (proposal,) = _view(cfg, incus, journals).proposals
    assert proposal.state == "uncertain"


def test_deleting_a_store_pr_proposal_removes_its_row(mocker, make_cfg, tmp_path):
    cfg, incus, journals = _env(mocker, make_cfg, tmp_path)
    row = _stage()
    view = _view(cfg, incus, journals)
    plan = plan_delete(view, ProposalId("pr", row.name), DeleteSelection())
    service.execute_delete(cfg, incus, IDENTITY.full_name, plan, journal_store=journals)
    assert store_db.read_proposals(state_dir() / "orchestration", UUID, "pr") == []
    incus.exec.assert_not_called()
