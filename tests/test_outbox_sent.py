"""Tests for the outbox Sent log module (state DB under the isolated XDG_STATE_HOME)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jailbee.outbox import sent
from jailbee.outbox.sent import SentAction

T0 = datetime(2026, 11, 4, 10, 0, tzinfo=UTC)
CREATED = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)


def _action(manifest: str = "m1", index: int = 0, **kw: object) -> SentAction:
    base: dict[str, object] = {
        "outbox": "issue",
        "manifest": manifest,
        "index": index,
        "kind": "comment",
        "repo": "o/r",
        "target": 5,
        "url": None,
        "title": None,
        "content": "hello",
        "digest": "d1",
    }
    base.update(kw)
    return SentAction(**base)  # type: ignore[arg-type]


def _rec(
    action: SentAction,
    state: str = "applied",
    *,
    instance: str = "app-feat",
    prefix: str = "app",
    created: datetime | None = CREATED,
    now: datetime = T0,
    publisher: str = "user",
) -> None:
    sent.record(
        instance=instance,
        container_prefix=prefix,
        container_created=created,
        publisher=publisher,  # type: ignore[arg-type]
        action=action,
        state=state,  # type: ignore[arg-type]
        now=now,
    )


def test_record_twice_same_key_is_one_row() -> None:
    _rec(_action())
    _rec(_action(), now=T0 + timedelta(minutes=1))
    rows = sent.list_records()
    assert len(rows) == 1
    assert rows[0].sent_at == T0 and rows[0].container == "feat"


def test_uncertain_then_mark_applied_sets_state_and_url() -> None:
    _rec(_action(target=None), "uncertain", publisher="orchestrator")
    sent.mark_applied(
        instance="app-feat",
        container_created=CREATED,
        outbox="issue",
        manifest="m1",
        digest="d1",
        index=0,
        url="https://github.com/o/r/issues/7",
        target=7,
    )
    (row,) = sent.list_records()
    assert (row.state, row.url, row.target) == ("applied", "https://github.com/o/r/issues/7", 7)
    assert row.publisher == "orchestrator" and row.sent_at == T0


def test_successful_retry_replaces_uncertain_attempt_provenance() -> None:
    _rec(_action(), "uncertain", publisher="user")
    success_time = T0 + timedelta(minutes=5)
    _rec(_action(url="https://github.com/o/r/issues/5"), publisher="orchestrator", now=success_time)
    (row,) = sent.list_records()
    assert (row.state, row.publisher, row.sent_at) == ("applied", "orchestrator", success_time)


def test_applied_is_never_downgraded() -> None:
    _rec(_action(url="u"), "applied")
    _rec(_action(), "uncertain")
    (row,) = sent.list_records()
    assert row.state == "applied" and row.url == "u"


def test_forget_removes_only_that_row() -> None:
    _rec(_action("m1"), "uncertain")
    _rec(_action("m2"), "uncertain")
    sent.forget(
        instance="app-feat",
        container_created=CREATED,
        outbox="issue",
        manifest="m1",
        digest="d1",
        index=0,
    )
    assert [r.manifest for r in sent.list_records()] == ["m2"]


def test_none_created_still_dedups_per_instance() -> None:
    _rec(_action(), created=None)
    _rec(_action(), created=None)
    _rec(_action(), created=None, instance="app-other")
    assert len(sent.list_records()) == 2


def test_list_filters_and_orders_newest_first() -> None:
    _rec(_action("a"), now=T0)
    _rec(_action("b"), now=T0 + timedelta(hours=2))
    _rec(
        _action("c"),
        instance="app-feat",
        created=CREATED + timedelta(days=1),
        now=T0 + timedelta(hours=1),
    )
    _rec(_action("d"), instance="zz-x", prefix="zz", now=T0 + timedelta(hours=3))
    assert [r.manifest for r in sent.list_records()] == ["d", "b", "c", "a"]
    assert [r.manifest for r in sent.list_records(container_prefix="app")] == ["b", "c", "a"]
    got = sent.list_records(instance="app-feat", container_created=CREATED)
    assert [r.manifest for r in got] == ["b", "a"]
    assert [r.manifest for r in sent.list_records(limit=1)] == ["d"]


def test_get_record() -> None:
    _rec(_action())
    (row,) = sent.list_records()
    assert sent.get_record(row.id) == row
    assert sent.get_record(row.id + 99) is None


def test_clear_by_prefix_and_before() -> None:
    _rec(_action("a"), now=T0)
    _rec(_action("b"), now=T0 + timedelta(days=2))
    _rec(_action("c"), instance="zz-x", prefix="zz", now=T0)
    assert sent.clear(container_prefix="app", before=T0 + timedelta(days=1)) == 1
    assert sorted(r.manifest for r in sent.list_records()) == ["b", "c"]
    assert sent.clear(container_prefix="app", before=None) == 1
    assert [r.manifest for r in sent.list_records()] == ["c"]


def test_counts() -> None:
    _rec(_action("a"))
    _rec(_action("b"))
    _rec(_action("c"), created=None)
    _rec(_action("d"), instance="zz-x", prefix="zz")
    c = sent.counts()
    assert c.by_instance == {("app-feat", CREATED): 2, ("app-feat", None): 1, ("zz-x", CREATED): 1}
    assert c.by_prefix == {"app": 3, "zz": 1}


def test_record_with_broken_db_warns_and_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))  # state dir cannot be created
    with caplog.at_level(logging.WARNING, logger="jailbee.outbox.sent"):
        _rec(_action())
        sent.mark_applied(
            instance="i",
            container_created=None,
            outbox="issue",
            manifest="m",
            digest="d",
            index=0,
            url=None,
            target=None,
        )
        sent.forget(
            instance="i",
            container_created=None,
            outbox="issue",
            manifest="m",
            digest="d",
            index=0,
        )
    assert len(caplog.records) == 3


def test_reused_manifest_name_with_new_digest_is_a_new_row() -> None:
    _rec(_action(digest="old", content="first", url="u1"))
    _rec(_action(digest="new", content="second", url="u2"), now=T0 + timedelta(minutes=5))
    rows = sorted(sent.list_records(), key=lambda r: r.id)
    assert [(r.content, r.url, r.sent_at) for r in rows] == [
        ("first", "u1", T0),
        ("second", "u2", T0 + timedelta(minutes=5)),
    ]


def test_forget_never_deletes_an_applied_row() -> None:
    _rec(_action(), "applied")
    sent.forget(
        instance="app-feat",
        container_created=CREATED,
        outbox="issue",
        manifest="m1",
        digest="d1",
        index=0,
    )
    assert len(sent.list_records()) == 1


def test_forget_with_other_digest_keeps_the_uncertain_row() -> None:
    _rec(_action(), "uncertain")
    sent.forget(
        instance="app-feat",
        container_created=CREATED,
        outbox="issue",
        manifest="m1",
        digest="other",
        index=0,
    )
    assert len(sent.list_records()) == 1
