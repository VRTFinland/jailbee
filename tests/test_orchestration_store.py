"""Tests for the broker message store."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from pathlib import Path

import pytest

from jailbee.orchestration import store as store_mod
from jailbee.orchestration.store import BrokerStore, StoreError, store_path


def msg(i: str, kind: str = "note", **extra: object) -> dict[str, object]:
    return {"id": i, "type": "message", "kind": kind, "ref": "r1", "from": "alice", **extra}


@pytest.fixture
def st(tmp_path: Path) -> BrokerStore:
    return BrokerStore(store_path(tmp_path))


def test_store_path(tmp_path: Path) -> None:
    assert store_path(tmp_path) == tmp_path / "broker.db"


def test_schema_version_recorded(st: BrokerStore) -> None:
    st.recipients()
    import sqlite3

    con = sqlite3.connect(st.path)
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    con.close()
    assert row[0] == str(len(store_mod._MIGRATIONS))


def test_ordering_and_fields(st: BrokerStore) -> None:
    assert st.insert_item("u", "p-b", msg("2"), now=1.0)
    assert st.insert_item(
        "u", "p-b", {"id": "1", "type": "status", "state": "done", "ref": "x"}, now=2.0
    )
    rows = st.unacked("u")
    assert [r.id for r in rows] == ["2", "1"]
    assert rows[0].kind_or_state == "note" and rows[0].from_name == "alice"
    assert rows[0].ref == "r1" and rows[0].item["id"] == "2"
    assert rows[1].kind_or_state == "done" and rows[1].type == "status"
    assert rows[0].seq < rows[1].seq and rows[0].recipient_full == "p-b"


def test_unique_insert(st: BrokerStore) -> None:
    assert st.insert_item("u", "p", msg("1"), now=1.0)
    assert not st.insert_item("u", "p", msg("1", kind="other"), now=2.0)
    assert len(st.history("u")) == 1
    assert st.insert_item("v", "q", msg("1"), now=1.0)


def test_ack_idempotent_and_unacked(st: BrokerStore) -> None:
    st.insert_item("u", "p", msg("1"), now=1.0)
    st.insert_item("u", "p", msg("2"), now=1.0)
    assert st.ack("u", "1", now=5.0)
    assert st.ack("u", "1", now=9.0)
    assert not st.ack("u", "nope", now=5.0)
    assert [r.id for r in st.unacked("u")] == ["2"]
    got = st.get("u", "1")
    assert got is not None and got.acked_at == 5.0
    assert st.get("u", "zz") is None


def test_history_includes_both(st: BrokerStore) -> None:
    st.insert_item("u", "p", msg("1"), now=1.0)
    st.insert_item("u", "p", msg("2"), now=1.0, acked=True)
    assert [r.id for r in st.history("u")] == ["1", "2"]
    assert [r.id for r in st.unacked("u")] == ["1"]
    row = st.get("u", "2")
    assert row is not None and row.acked_at == 1.0


def test_requests(st: BrokerStore) -> None:
    assert st.lookup_request("u", "a") is None
    st.record_request("u", "a", digest="d", state="done", detail="ok", item={"id": "s"}, now=1.0)
    r = st.lookup_request("u", "a")
    assert r is not None and (r.state, r.detail, r.item) == ("done", "ok", {"id": "s"})
    st.record_request("u", "b", digest=None, state="x", detail="", item=None, now=1.0)
    b = st.lookup_request("u", "b")
    assert b is not None and b.item is None


def test_recipients(st: BrokerStore) -> None:
    assert st.recipient_uuid("p") is None
    st.set_recipient("p", "u1", now=1.0)
    st.set_recipient("q", "u2", now=1.0)
    assert st.recipient_uuid("p") == "u1"
    assert st.recipients() == {"p": "u1", "q": "u2"}
    st.set_recipient("p", "u3", now=2.0)
    assert st.recipient_uuid("p") == "u3"
    st.remove_recipient("p")
    assert st.recipient_uuid("p") is None


def test_digest(st: BrokerStore) -> None:
    assert st.get_digest_at("u") is None
    st.set_digest_at("u", 3.0)
    st.set_digest_at("u", 4.0)
    assert st.get_digest_at("u") == 4.0


def test_purge_uuids(st: BrokerStore) -> None:
    for u in ("a", "b"):
        st.insert_item(u, "f" + u, msg("1"), now=1.0)
        st.ack(u, "1", now=1.0)
        st.record_request(u, "r", digest=None, state="s", detail="", item=None, now=1.0)
        st.set_digest_at(u, 1.0)
        st.set_recipient("f" + u, u, now=1.0)
    assert st.known_uuids() == {"a", "b"}
    assert st.purge_uuids({"b"}, keep_unacked_after=0.0) == 4
    assert st.history("b") == [] and st.lookup_request("b", "r") is None
    assert st.get_digest_at("b") is None and st.recipients() == {"fa": "a"}
    assert len(st.history("a")) == 1 and st.get_digest_at("a") == 1.0


def test_purge_uuids_keeps_young_unacked_items(st: BrokerStore) -> None:
    st.insert_item("b", "fb", msg("old"), now=1.0)
    st.insert_item("b", "fb", msg("young"), now=100.0)
    st.insert_item("b", "fb", msg("read"), now=100.0, acked=True)
    assert st.purge_uuids({"b"}, keep_unacked_after=50.0) == 2
    assert [r.id for r in st.history("b")] == ["young"]


def test_purge_acked_older_than(st: BrokerStore) -> None:
    st.insert_item("u", "p", msg("old"), now=1.0)
    st.insert_item("u", "p", msg("new"), now=1.0)
    st.insert_item("u", "p", msg("open"), now=1.0)
    st.ack("u", "old", now=10.0)
    st.ack("u", "new", now=100.0)
    st.record_request("u", "r1", digest=None, state="s", detail="", item=None, now=5.0)
    st.record_request("u", "r2", digest=None, state="s", detail="", item=None, now=500.0)
    assert st.purge_acked_older_than("u", 50.0) == 1
    assert sorted(r.id for r in st.history("u")) == ["new", "open"]
    assert st.lookup_request("u", "r1") is None
    assert st.lookup_request("u", "r2") is not None


def test_reopen_keeps_data(tmp_path: Path) -> None:
    BrokerStore(store_path(tmp_path)).insert_item("u", "p", msg("1"), now=1.0)
    assert [r.id for r in BrokerStore(store_path(tmp_path)).unacked("u")] == ["1"]


def test_file_mode(st: BrokerStore) -> None:
    st.recipients()
    assert stat.S_IMODE(os.stat(st.path).st_mode) == 0o600


def test_module_recipient_uuid_absent_creates_nothing(tmp_path: Path) -> None:
    assert store_mod.recipient_uuid(tmp_path, "p") is None
    assert not store_path(tmp_path).exists()


def test_module_recipient_uuid_present(tmp_path: Path) -> None:
    BrokerStore(store_path(tmp_path)).set_recipient("p", "u", now=1.0)
    assert store_mod.recipient_uuid(tmp_path, "p") == "u"
    assert store_mod.recipient_uuid(tmp_path, "zz") is None


def test_corrupt_file(tmp_path: Path) -> None:
    p = store_path(tmp_path)
    p.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(StoreError):
        BrokerStore(p).recipients()


def test_purge_uuids_empty_collection_is_a_noop(st: BrokerStore) -> None:
    st.insert_item("a", "fa", msg("1"), now=1.0)
    st.set_recipient("fa", "a", now=1.0)
    assert st.purge_uuids(set(), keep_unacked_after=0.0) == 0
    assert len(st.history("a")) == 1 and st.recipients() == {"fa": "a"}


def test_purge_acked_boundary_is_exclusive(st: BrokerStore) -> None:
    st.insert_item("u", "p", msg("edge"), now=1.0)
    st.ack("u", "edge", now=50.0)
    st.record_request("u", "r", digest=None, state="s", detail="", item=None, now=50.0)
    assert st.purge_acked_older_than("u", 50.0) == 0
    assert st.get("u", "edge") is not None and st.lookup_request("u", "r") is not None
    assert st.purge_acked_older_than("u", 50.1) == 1
    assert st.lookup_request("u", "r") is None


def test_wal_enabled(st: BrokerStore) -> None:
    st.recipients()
    con = sqlite3.connect(st.path)
    mode = con.execute("PRAGMA journal_mode").fetchone()[0]
    con.close()
    assert mode == "wal"


def test_migrations_extendable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = store_path(tmp_path)
    BrokerStore(path).insert_item("u", "p", msg("1"), now=1.0)
    extended = [
        *store_mod._MIGRATIONS,
        "ALTER TABLE digest ADD COLUMN note TEXT;\nCREATE TABLE extra(x);",
    ]
    monkeypatch.setattr(store_mod, "_MIGRATIONS", extended)
    assert [r.id for r in BrokerStore(path).history("u")] == ["1"]
    con = sqlite3.connect(path)
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    con.execute("SELECT x FROM extra")
    con.close()
    assert row[0] == "2"


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    BrokerStore(path).recipients()
    con = sqlite3.connect(path)
    con.execute(
        "UPDATE meta SET value=? WHERE key='schema_version'", (str(len(store_mod._MIGRATIONS) + 1),)
    )
    con.commit()
    con.close()
    store_mod._MIGRATED.clear()  # the process-wide cache would skip the check
    with pytest.raises(StoreError, match="newer"):
        BrokerStore(path).recipients()


def test_concurrent_inserts(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    errors: list[Exception] = []

    def work(t: int) -> None:
        s = BrokerStore(path)
        try:
            for i in range(50):
                s.insert_item("u", "p", msg(f"{t}-{i}"), now=1.0)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(t,)) for t in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == []
    assert len(BrokerStore(path).history("u")) == 200


def test_module_recipient_uuid_never_writes(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    path.write_bytes(b"")
    assert store_mod.recipient_uuid(tmp_path, "p") is None
    assert path.stat().st_size == 0
    path.write_bytes(b"garbage" * 100)
    with pytest.raises(StoreError):
        store_mod.recipient_uuid(tmp_path, "p")


def test_module_recipient_uuid_missing_table_is_none(tmp_path: Path) -> None:
    con = sqlite3.connect(store_path(tmp_path))
    con.execute("CREATE TABLE other(x)")
    con.commit()
    con.close()
    assert store_mod.recipient_uuid(tmp_path, "p") is None


def test_module_recipient_uuid_locked_db_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    BrokerStore(store_path(tmp_path)).set_recipient("p", "u", now=1.0)
    monkeypatch.setattr(store_mod, "_BUSY_TIMEOUT_MS", 50)
    lock = sqlite3.connect(store_path(tmp_path), isolation_level=None)
    lock.execute("PRAGMA locking_mode=EXCLUSIVE")
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(StoreError):
            store_mod.recipient_uuid(tmp_path, "p")
    finally:
        lock.close()


def test_migrations_run_once_per_path_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = store_path(tmp_path)
    BrokerStore(path).recipients()
    calls: list[int] = []
    real = BrokerStore._migrate
    monkeypatch.setattr(
        BrokerStore, "_migrate", staticmethod(lambda con: (calls.append(1), real(con))[1])
    )
    BrokerStore(path).recipients()
    BrokerStore(path).recipients()
    assert calls == []


def test_migration_cache_is_dropped_when_the_file_vanishes(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    BrokerStore(path).set_recipient("p", "u", now=1.0)
    path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    assert BrokerStore(path).recipients() == {}


def test_unopenable_db_path_is_a_store_error(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    with pytest.raises(StoreError):
        BrokerStore(blocker / "broker.db").recipients()


def test_history_meta_has_messages_only_without_item_json(st: BrokerStore) -> None:
    st.insert_item("u", "p", msg("m1", body="x" * 50), now=1.0)
    st.insert_item("u", "p", {"id": "s1", "type": "status", "state": "done", "ref": "m1"}, now=2.0)
    st.insert_item("u", "p", msg("m2"), now=3.0, acked=True)
    st.insert_item("other", "q", msg("m3"), now=4.0)
    rows = st.history_meta("u")
    assert [r.id for r in rows] == ["m1", "m2"]
    assert rows[0].from_name == "alice" and rows[0].kind_or_state == "note"
    assert rows[0].created_at == 1.0 and rows[0].acked_at is None
    assert rows[1].acked_at == 3.0
    assert not hasattr(rows[0], "item")


def test_has_recipient_uuid(st: BrokerStore) -> None:
    assert not st.has_recipient_uuid("u1")
    st.set_recipient("p", "u1", now=1.0)
    assert st.has_recipient_uuid("u1")
    assert not st.has_recipient_uuid("u2")
