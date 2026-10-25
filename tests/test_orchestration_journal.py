from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

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
    Journal(tmp_path).record("c1", "d1", "m1", "ok", AT)
    with (tmp_path / "journal.jsonl").open("a") as fh:
        fh.write('{"cont')
    j = Journal(tmp_path)
    assert j.seen("c1", "d1")
    assert not j.seen("c1", "d2")


def test_old_entries_without_a_key_fall_back_to_the_name(tmp_path: Path) -> None:
    (tmp_path / "journal.jsonl").write_text(
        '{"container_full": "c1", "digest": "d1", "manifest_id": "m", "outcome": "ok", "at": ""}\n'
    )
    assert Journal(tmp_path).seen("c1", "d1")


def test_record_keeps_the_full_name_for_reading(tmp_path: Path) -> None:
    import json

    Journal(tmp_path).record("uuid-1", "d1", "m1", "ok", AT, container_full="proj-x")
    entry = json.loads((tmp_path / "journal.jsonl").read_text())
    assert entry["container_key"] == "uuid-1"
    assert entry["container_full"] == "proj-x"
