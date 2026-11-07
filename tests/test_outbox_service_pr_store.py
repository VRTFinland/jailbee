"""`jb outbox` sees and deletes PR source proposals held in the host store."""

from __future__ import annotations

import json

from jailbee.db import state_dir
from jailbee.outbox import publication_db, service, store_db
from jailbee.outbox.delete import DeleteSelection, plan_delete
from jailbee.outbox.models import ProposalId
from jailbee.outbox_io import proposal_digest
from tests.outbox_support import IDENTITY, pr_files, store

UUID = "uuid-1"
SOURCE = {
    "version": 1,
    "repo": "acme/widgets",
    "pr": None,
    "head_sha": None,
    "source": "refs/heads/topic",
    "source_sha": "a" * 40,
    "base": "main",
    "actions": [{"type": "description", "title": "Title", "body_file": "b.md"}],
}


def _env(mocker, make_cfg, tmp_path, files=None):
    incus = mocker.Mock()
    incus.list_containers.return_value = [
        {
            "name": IDENTITY.full_name,
            "created_at": IDENTITY.created_at,
            "config": {"volatile.uuid": UUID},
        }
    ]
    mocker.patch.object(service, "read_store", return_value=store("pr", files or {}))
    mutate = mocker.patch.object(service, "mutate_store")
    return make_cfg(tmp_path), incus, mutate


def _stage() -> str:
    def build(stem: str) -> tuple[str, dict[str, str]]:
        manifest = json.loads(json.dumps(SOURCE))
        manifest["actions"][0]["body_file"] = f"{stem}-1.md"
        return json.dumps(manifest), {f"{stem}-1.md": "Body"}

    root = state_dir() / "orchestration"
    return store_db.host_store(root).put_proposal(UUID, "pr", "source", build, now=1.0).name


def _rows():
    return store_db.read_proposals(state_dir() / "orchestration", UUID, "pr")


def _view(cfg, incus):
    return service.load_container(cfg, incus, IDENTITY.full_name)


def test_store_sources_appear_next_to_file_proposals(mocker, make_cfg, tmp_path):
    cfg, incus, _ = _env(mocker, make_cfg, tmp_path, pr_files())
    name = _stage()
    view = _view(cfg, incus)
    assert view.available, view.error
    states = {str(p.id): p.state for p in view.proposals}
    assert states[f"pr/{name}"] == "awaiting-push"
    assert "pr/001.json" in states
    incus.exec.assert_not_called()


def test_an_uncertain_source_publication_shows_as_uncertain(mocker, make_cfg, tmp_path):
    cfg, incus, _ = _env(mocker, make_cfg, tmp_path)
    name = _stage()
    (row,) = _rows()
    digest = proposal_digest(row.name, row.manifest, row.bodies)
    pubs = publication_db.open_publications()
    with pubs.claim(UUID, "pr", name) as claim:
        pubs.begin(claim, digest=digest, action_count=1, container_full="c", now=1.0)
        pubs.mark_prepared(claim, digest, 0, repo="acme/widgets", now=1.0)
    (proposal,) = _view(cfg, incus).proposals
    assert proposal.state == "uncertain"
    assert "uncertain" in (proposal.error or "")


def test_deleting_a_store_source_removes_its_row_not_container_files(mocker, make_cfg, tmp_path):
    cfg, incus, mutate = _env(mocker, make_cfg, tmp_path)
    name = _stage()
    view = _view(cfg, incus)
    plan = plan_delete(view, ProposalId("pr", name), DeleteSelection())
    service.execute_delete(cfg, incus, IDENTITY.full_name, plan)
    assert _rows() == []
    mutate.assert_not_called()
    incus.exec.assert_not_called()
