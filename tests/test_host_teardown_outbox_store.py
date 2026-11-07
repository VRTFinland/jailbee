"""Destroy deletes a container's store proposals and drafts, never its journals."""

from __future__ import annotations

from unittest.mock import MagicMock

from jailbee import host_teardown
from jailbee.db import state_dir
from jailbee.outbox import store_db


def _incus(name: str, uuid: str) -> MagicMock:
    incus = MagicMock()
    incus.list_containers.return_value = [
        {
            "name": name,
            "created_at": "2026-11-01T10:00:00Z",
            "config": {"volatile.uuid": uuid},
        }
    ]
    return incus


def _build(stem: str) -> tuple[str, dict[str, str]]:
    return '{"version":1,"actions":[]}', {f"{stem}-1.md": "body"}


def test_destroy_deletes_proposals_and_drafts_and_keeps_an_uncertain_journal() -> None:
    root = state_dir() / "orchestration"
    ob = store_db.host_store(root)
    row = ob.put_proposal("u-dead", "issue", "a", _build, now=1.0)
    ob.put_proposal("u-live", "issue", "a", _build, now=1.0)
    with ob._store.transaction() as con:
        con.execute(
            "INSERT INTO outbox_draft(uuid, kind, name, manifest, bodies, created_at, "
            "updated_at) VALUES('u-dead','issue','d','{}','{}',1,1)"
        )
        con.execute(
            "INSERT INTO outbox_journal(uuid, kind, proposal_name, digest, container_full, "
            "action_count, created_at) VALUES('u-dead','issue',?,'d','foo',1,1)",
            (row.name,),
        )
        con.execute(
            "INSERT INTO outbox_journal_action(uuid, kind, proposal_name, digest, idx, state, "
            "repo, at) VALUES('u-dead','issue',?,'d',0,'uncertain','.',1)",
            (row.name,),
        )

    host_teardown.cleanup(host_teardown.snapshot(_incus("foo", "u-dead"), "foo"))

    assert store_db.read_proposals(root, "u-dead") == []
    assert len(store_db.read_proposals(root, "u-live")) == 1
    with ob._store.transaction(write=False) as con:
        assert con.execute("SELECT COUNT(*) FROM outbox_draft").fetchone() == (0,)
        assert con.execute("SELECT state FROM outbox_journal_action").fetchall() == [("uncertain",)]
