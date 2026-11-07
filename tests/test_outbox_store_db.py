"""The host outbox store in broker.db: numbering, caps, isolation, read-only reads."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox import store_db
from jailbee.outbox.store_db import OutboxStore, ProposalCapError


def _build(text: str = '{"version":1,"actions":[]}', bodies: dict[str, str] | None = None):
    def build(stem: str) -> tuple[str, dict[str, str]]:
        return text, {f"{stem}-{i}.md": b for i, b in enumerate((bodies or {}).values(), 1)}

    return build


@pytest.fixture
def ob(tmp_path: Path) -> OutboxStore:
    return OutboxStore(BrokerStore(store_path(tmp_path)))


def test_put_numbers_proposals_per_container_and_kind(ob: OutboxStore) -> None:
    first = ob.put_proposal("u1", "issue", "fix-a", _build(), now=1.0)
    second = ob.put_proposal("u1", "issue", "fix-b", _build(), now=2.0)
    other = ob.put_proposal("u2", "issue", "fix-a", _build(), now=3.0)
    pr = ob.put_proposal("u1", "pr", "review", _build(), now=4.0)
    assert [first.name, second.name, other.name, pr.name] == [
        "001-fix-a.json",
        "002-fix-b.json",
        "001-fix-a.json",
        "001-review.json",
    ]
    assert first.revision == 1


def test_numbers_are_never_reused_after_a_proposal_is_gone(ob: OutboxStore, tmp_path: Path) -> None:
    ob.put_proposal("u1", "issue", "a", _build(), now=1.0)
    with sqlite3.connect(store_path(tmp_path)) as con:
        con.execute("DELETE FROM outbox_proposal")
    assert ob.put_proposal("u1", "issue", "b", _build(), now=2.0).name == "002-b.json"


def test_bodies_are_stored_under_the_names_the_builder_chose(ob: OutboxStore) -> None:
    put = ob.put_proposal("u1", "issue", "a", _build(bodies={"x.md": "long"}), now=1.0)
    assert put.bodies == {"001-a-1.md": "long"}
    shown = ob.get_proposal("u1", "issue", "001-a.json")
    assert shown is not None and shown.bodies == {"001-a-1.md": "long"}
    assert shown.manifest == '{"version":1,"actions":[]}'


def test_cap_counts_pending_proposals_per_kind(ob: OutboxStore) -> None:
    for i in range(3):
        ob.put_proposal("u1", "issue", f"p{i}", _build(), now=1.0, cap=3)
    ob.put_proposal("u1", "pr", "p", _build(), now=1.0, cap=3)
    ob.put_proposal("u2", "issue", "p", _build(), now=1.0, cap=3)
    with pytest.raises(ProposalCapError, match="cap of 3"):
        ob.put_proposal("u1", "issue", "more", _build(), now=1.0, cap=3)
    assert len(ob.list_proposals("u1", "issue")) == 3


def test_a_failing_builder_stores_nothing(ob: OutboxStore) -> None:
    def broken(stem: str) -> tuple[str, dict[str, str]]:
        raise ValueError("bad manifest")

    with pytest.raises(ValueError, match="bad manifest"):
        ob.put_proposal("u1", "issue", "a", broken, now=1.0)
    assert ob.list_proposals("u1") == []
    assert ob.put_proposal("u1", "issue", "a", _build(), now=1.0).name == "001-a.json"


def test_concurrent_puts_get_distinct_numbers(ob: OutboxStore) -> None:
    names: list[str] = []
    lock = threading.Lock()

    def put(i: int) -> None:
        name = ob.put_proposal("u1", "issue", f"t{i}", _build(), now=1.0).name
        with lock:
            names.append(name[:3])

    threads = [threading.Thread(target=put, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(names) == [f"{n:03d}" for n in range(1, 9)]


def test_list_is_scoped_and_ordered(ob: OutboxStore) -> None:
    ob.put_proposal("u1", "issue", "b", _build(), now=1.0)
    ob.put_proposal("u1", "pr", "a", _build(), now=1.0)
    ob.put_proposal("u2", "issue", "c", _build(), now=1.0)
    ob.put_proposal("u1", "issue", "a", _build(), now=1.0)
    assert [(p.kind, p.name) for p in ob.list_proposals("u1")] == [
        ("issue", "001-b.json"),
        ("issue", "002-a.json"),
        ("pr", "001-a.json"),
    ]
    assert [p.name for p in ob.list_proposals("u1", "pr")] == ["001-a.json"]
    assert ob.get_proposal("u2", "issue", "001-b.json") is None


def test_read_proposals_never_creates_the_database(tmp_path: Path) -> None:
    assert store_db.read_proposals(tmp_path, "u1", "issue") == []
    assert not store_path(tmp_path).exists()


def test_read_proposals_reads_without_writing(ob: OutboxStore, tmp_path: Path) -> None:
    ob.put_proposal("u1", "issue", "a", _build(bodies={"b": "text"}), now=5.0)
    before = store_path(tmp_path).read_bytes()
    (proposal,) = store_db.read_proposals(tmp_path, "u1", "issue")
    assert (proposal.name, proposal.bodies, proposal.created_at) == (
        "001-a.json",
        {"001-a-1.md": "text"},
        5.0,
    )
    assert store_db.read_proposals(tmp_path, "u2", "issue") == []
    assert store_path(tmp_path).read_bytes() == before


def test_read_proposals_of_a_store_without_outbox_tables_is_empty(tmp_path: Path) -> None:
    with sqlite3.connect(store_path(tmp_path)) as con:
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    assert store_db.read_proposals(tmp_path, "u1", "issue") == []


def test_one_migration_creates_every_outbox_table(ob: OutboxStore, tmp_path: Path) -> None:
    ob.list_proposals("u1")
    with sqlite3.connect(store_path(tmp_path)) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "outbox_proposal",
        "outbox_body",
        "outbox_counter",
        "outbox_draft",
        "outbox_journal",
        "outbox_journal_action",
        "outbox_sent",
    } <= tables


def test_kind_is_checked(ob: OutboxStore) -> None:
    with pytest.raises(ValueError, match="kind"):
        ob.put_proposal("u1", "other", "a", _build(), now=1.0)  # type: ignore[arg-type]  # invalid on purpose
