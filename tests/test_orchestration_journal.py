from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from jailbee.orchestration.journal import Journal, manifest_digest

if TYPE_CHECKING:
    from pathlib import Path

AT = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


def test_record_then_seen(tmp_path: Path) -> None:
    j = Journal(tmp_path)
    assert not j.seen("c1", "d1")
    j.record("c1", "d1", "m1", "ok", AT)
    assert j.seen("c1", "d1")


def test_seen_is_per_container(tmp_path: Path) -> None:
    j = Journal(tmp_path)
    j.record("c1", "d1", "m1", "ok", AT)
    assert not j.seen("c2", "d1")


def test_digest_changes_with_body() -> None:
    a = manifest_digest("m.yaml", "text", {"p.md": "one"})
    b = manifest_digest("m.yaml", "text", {"p.md": "two"})
    assert a != b
    assert a == manifest_digest("m.yaml", "text", {"p.md": "one"})
    assert len(a) == 64


def test_journal_survives_reload(tmp_path: Path) -> None:
    Journal(tmp_path).record("c1", "d1", "m1", "ok", AT)
    assert Journal(tmp_path).seen("c1", "d1")


def test_truncated_last_line_ignored(tmp_path: Path) -> None:
    (tmp_path / "journal.jsonl").write_text('{"container_full":"c1","digest":"d1"}\n{"cont')
    j = Journal(tmp_path)
    assert j.seen("c1", "d1")
    assert not j.seen("c1", "d2")


def test_old_entries_without_a_key_fall_back_to_the_name(tmp_path: Path) -> None:
    (tmp_path / "journal.jsonl").write_text(
        '{"container_full": "c1", "digest": "d1", "manifest_id": "m", "outcome": "ok", "at": ""}\n'
    )
    assert Journal(tmp_path).seen("c1", "d1")


def test_record_keeps_the_full_name_for_reading(tmp_path: Path) -> None:
    import sqlite3

    Journal(tmp_path).record("uuid-1", "d1", "m1", "ok", AT, container_full="proj-x")
    with sqlite3.connect(tmp_path / "broker.db") as con:
        row = con.execute("SELECT container_full, manifest_id, outcome, at FROM journal").fetchone()
    assert row == ("proj-x", "m1", "ok", AT.isoformat())


def test_existing_instance_sees_new_record_and_duplicate(tmp_path: Path) -> None:
    first = Journal(tmp_path)
    assert not first.seen("c", "d")
    second = Journal(tmp_path)
    second.record("c", "d", "m", "ok", AT)
    second.record("c", "d", "m", "ok", AT)
    assert first.seen("c", "d")


def test_legacy_import_once_and_corrupt_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text('{"container_full":"c","digest":"d"}\nnot json\n')
    assert Journal(tmp_path).seen("c", "d")
    assert path.with_name("journal.jsonl.migrated").exists()
    assert "malformed" in caplog.text
    path.write_text('{"container_full":"other","digest":"d"}\n')
    assert not Journal(tmp_path).seen("other", "d")


def test_entirely_corrupt_legacy_is_quarantined(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text("broken\n")
    assert not Journal(tmp_path).seen("c", "d")
    assert path.with_name("journal.jsonl.corrupt").exists()
    assert "malformed" in caplog.text


def test_failed_record_rolls_back_without_false_seen(tmp_path: Path) -> None:
    import sqlite3

    from jailbee.orchestration.store import StoreError

    journal = Journal(tmp_path)
    assert not journal.seen("c", "d")
    with sqlite3.connect(tmp_path / "broker.db") as con:
        con.execute(
            "CREATE TRIGGER fail_journal BEFORE INSERT ON journal "
            "BEGIN SELECT RAISE(ABORT, 'disk failure'); END"
        )
    with pytest.raises(StoreError):
        journal.record("c", "d", "m", "ok", AT)
    assert not Journal(tmp_path).seen("c", "d")
    with sqlite3.connect(tmp_path / "broker.db") as con:
        con.execute("DROP TRIGGER fail_journal")
    journal.record("c", "d", "m", "ok", AT)
    assert Journal(tmp_path).seen("c", "d")


def test_import_rename_gap_does_not_reimport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    path = tmp_path / "journal.jsonl"
    path.write_text('{"container_full":"c","digest":"d"}\n')
    replace = Path.replace

    def fail_replace(self: Path, target: Path) -> Path:
        if self == path:
            raise OSError("rename failed")
        return replace(self, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        assert Journal(tmp_path).seen("c", "d")
    path.write_text('{"container_full":"other","digest":"d"}\n')
    journal = Journal(tmp_path)
    assert journal.seen("c", "d")
    assert not journal.seen("other", "d")
    assert path.with_name("journal.jsonl.migrated").exists()


def test_unreadable_import_retries_before_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    from jailbee.orchestration.store import StoreError

    path = tmp_path / "journal.jsonl"
    path.write_text('{"container_full":"c","digest":"d"}\n')
    read_bytes = Path.read_bytes

    def fail_read(self: Path) -> bytes:
        if self == path:
            raise OSError("unreadable")
        return read_bytes(self)

    journal = Journal(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", fail_read)
        with pytest.raises(StoreError):
            journal.record("other", "d", "m", "ok", AT)
    assert journal.seen("c", "d")
    assert not journal.seen("other", "d")


def test_torn_utf8_preserves_prior_rows(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_bytes(b'{"container_full":"c","digest":"d"}\n{"bad":"\xff')
    assert Journal(tmp_path).seen("c", "d")
    assert path.with_name("journal.jsonl.migrated").exists()


def test_legacy_nonstring_metadata_keeps_identity(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    path.write_text('{"container_key":"c","digest":"d","outcome":null,"at":0}\n')
    assert Journal(tmp_path).seen("c", "d")


def test_legacy_fields_preserved(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "journal.jsonl"
    path.write_text(
        '{"container_key":"uuid","container_full":"name","digest":"d",'
        '"manifest_id":"m","outcome":"ok","at":"then"}\n'
    )
    assert Journal(tmp_path).seen("uuid", "d")
    with sqlite3.connect(tmp_path / "broker.db") as con:
        row = con.execute("SELECT container_full, manifest_id, outcome, at FROM journal").fetchone()
    assert row == ("name", "m", "ok", "then")
