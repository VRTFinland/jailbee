"""Dropping, destroy semantics and pending counts of the host outbox store."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox import store_db
from jailbee.outbox.store_db import OutboxStore, ProposalChangedError, ProposalLockedError


def _build(stem: str) -> tuple[str, dict[str, str]]:
    return '{"version":1,"actions":[]}', {f"{stem}-1.md": "body"}


@pytest.fixture
def ob(tmp_path: Path) -> OutboxStore:
    return OutboxStore(BrokerStore(store_path(tmp_path)))


def _journal(ob: OutboxStore, uuid: str, name: str, states: list[str]) -> None:
    with ob._store.transaction() as con:
        con.execute(
            "INSERT INTO outbox_journal(uuid, kind, proposal_name, digest, container_full, "
            "action_count, created_at, archived_at) VALUES(?,?,?,?,?,?,?,NULL)",
            (uuid, "issue", name, "d", "c", len(states), 1.0),
        )
        for i, state in enumerate(states):
            con.execute(
                "INSERT INTO outbox_journal_action(uuid, kind, proposal_name, digest, idx, "
                "state, repo, at) VALUES(?,?,?,?,?,?,?,?)",
                (uuid, "issue", name, "d", i, state, ".", 1.0),
            )


def test_drop_removes_the_proposal_and_its_bodies(ob: OutboxStore, tmp_path: Path) -> None:
    row = ob.put_proposal("u1", "issue", "a", _build, now=1.0)
    ob.put_proposal("u1", "issue", "b", _build, now=1.0)
    ob.drop_proposal("u1", "issue", row.name, row.revision)
    assert [p.name for p in store_db.read_proposals(tmp_path, "u1")] == ["002-b.json"]
    with ob._store.transaction(write=False) as con:
        (bodies,) = con.execute("SELECT COUNT(*) FROM outbox_body").fetchone()
    assert bodies == 1


def test_drop_with_a_stale_row_version_is_refused(ob: OutboxStore) -> None:
    row = ob.put_proposal("u1", "issue", "a", _build, now=1.0)
    ob.edit_proposal("u1", "issue", row.name, 1, '{"version":1,"actions":[1]}')
    with pytest.raises(ProposalChangedError):
        ob.drop_proposal("u1", "issue", row.name, 1)
    edited = ob.get_proposal("u1", "issue", row.name)
    assert edited is not None
    assert edited.revision == 2
    ob.drop_proposal("u1", "issue", row.name, 2)
    with pytest.raises(ProposalChangedError):
        ob.drop_proposal("u1", "issue", row.name)


def test_a_proposal_with_recorded_progress_cannot_be_dropped(ob: OutboxStore) -> None:
    row = ob.put_proposal("u1", "issue", "a", _build, now=1.0)
    _journal(ob, "u1", row.name, ["applied", "uncertain"])
    with pytest.raises(ProposalLockedError):
        ob.drop_proposal("u1", "issue", row.name)


def test_destroy_deletes_proposals_and_drafts_but_keeps_journals(
    ob: OutboxStore, tmp_path: Path
) -> None:
    row = ob.put_proposal("u1", "issue", "a", _build, now=1.0)
    ob.put_proposal("u2", "issue", "a", _build, now=1.0)
    _journal(ob, "u1", row.name, ["uncertain"])
    with ob._store.transaction() as con:
        con.execute(
            "INSERT INTO outbox_draft(uuid, kind, name, manifest, bodies, created_at, "
            "updated_at) VALUES('u1','issue','d','{}','{}',1,1)"
        )
    assert ob.delete_container("u1") == 1
    assert store_db.read_proposals(tmp_path, "u1") == []
    assert len(store_db.read_proposals(tmp_path, "u2")) == 1
    with ob._store.transaction(write=False) as con:
        assert con.execute("SELECT COUNT(*) FROM outbox_draft").fetchone() == (0,)
        assert con.execute("SELECT state FROM outbox_journal_action").fetchall() == [
            ("uncertain",)
        ]


def test_pending_counts_are_journal_aware(ob: OutboxStore, tmp_path: Path) -> None:
    names = [ob.put_proposal("u1", "issue", s, _build, now=1.0).name for s in "abc"]
    ob.put_proposal("u2", "issue", "a", _build, now=1.0)
    _journal(ob, "u1", names[0], ["applied"])  # fully applied: finished
    _journal(ob, "u1", names[1], ["applied", "uncertain"])  # still needs attention
    assert store_db.read_pending_counts(tmp_path) == {"u1": 2, "u2": 1}


def test_pending_counts_of_a_missing_store_are_empty(tmp_path: Path) -> None:
    assert store_db.read_pending_counts(tmp_path) == {}
