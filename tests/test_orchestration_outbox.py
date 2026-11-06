from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from jailbee.incus import IncusError
from jailbee.orchestration import outbox
from jailbee.orchestration.protocol import InboxStatus, inbox_file_name
from jailbee.orchestration.state import pending_dir

if TYPE_CHECKING:
    from pathlib import Path

AT = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)
FULL = "proj-child"


def test_grant_json_has_publish():
    from jailbee.orchestration.grant import Grant, PublishRights

    data = json.loads(
        outbox.grant_json_text(Grant(publish=PublishRights(issues=("comment",))), None, None)
    )
    assert data["publish"]["issues"] == ["comment"]


def _item(n: str) -> InboxStatus:
    return InboxStatus(version=1, id=f"h-{n}", type="status", ref="r", state="queued", detail="d")


def _incus() -> MagicMock:
    return MagicMock()


def _finish_item():
    from jailbee.orchestration.protocol import PendingFinish

    return PendingFinish(
        version=1,
        type="finish",
        id="f-1",
        child_full="proj-c",
        child_short="c",
        branch="b",
        done_sha="a" * 40,
        body="done",
        in_reply_to=None,
        uid=1000,
    )


def _finished_message():
    from jailbee.orchestration.protocol import InboxMessage

    return InboxMessage(version=1, id="h-f", type="message", from_="c", body="done")


def test_flush_stops_at_finish_item_without_callback(tmp_path: Path) -> None:
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    path = directory / "a.finish.json"
    path.write_text(_finish_item().model_dump_json())
    incus = _incus()
    assert flush(incus, tmp_path, FULL, uid=1000) == 0
    assert path.exists()
    incus.exec_with_input.assert_not_called()


def test_ensure_env_missing_launcher_raises() -> None:
    incus = _incus()
    incus.exec.side_effect = IncusError("missing")
    with pytest.raises(outbox.ContainerJailbeeMissing) as exc:
        outbox.ensure_orchestration_env(incus, FULL, uid=1000)
    assert type(exc.value) is outbox.ContainerJailbeeMissing
    assert str(exc.value) == (
        "container image has no in-container jailbee (needed for `jailbee inbox`) — "
        "run `jb base build` on the host, then recreate the container"
    )
    assert len(incus.exec.call_args_list) == 1


def test_flush_without_pending_dir(tmp_path: Path) -> None:
    assert flush(_incus(), tmp_path, FULL, uid=None) == 0


def test_has_pending_ignores_quarantined_bad_files(tmp_path: Path) -> None:
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / "a.finish.json.bad").write_text("invalid")
    assert outbox.has_pending(tmp_path, FULL) is False
    (directory / "b.json").write_text("{}")
    assert outbox.has_pending(tmp_path, FULL) is True


def _store_recipient(root: Path):
    from jailbee.orchestration.store import BrokerStore, store_path

    store = BrokerStore(store_path(root))
    store.set_recipient(FULL, "uuid-1", now=1.0)
    return store


@pytest.mark.parametrize("running", [True, False])
def test_deliver_inserts_into_store_for_store_recipient(tmp_path: Path, running: bool) -> None:
    store = _store_recipient(tmp_path)
    incus = _incus()
    result = deliver(incus, tmp_path, FULL, _item("1"), running=running, uid=1000, now=AT)
    assert result == "delivered"
    assert [s.id for s in store.unacked("uuid-1")] == ["h-1"]
    assert store.unacked("uuid-1")[0].item == json.loads(_item("1").model_dump_json(by_alias=True))
    incus.exec.assert_not_called()
    incus.exec_with_input.assert_not_called()
    assert not outbox.has_pending(tmp_path, FULL)


def test_deliver_store_parks_behind_existing_pending(tmp_path: Path) -> None:
    store = _store_recipient(tmp_path)
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / inbox_file_name(_item("0"), AT)).write_text(
        _item("0").model_dump_json(by_alias=True)
    )
    later = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
    result = deliver(_incus(), tmp_path, FULL, _item("1"), running=True, uid=1000, now=later)
    assert result == "parked"
    assert store.unacked("uuid-1") == []
    assert (directory / inbox_file_name(_item("1"), later)).is_file()
    # The next flush moves both, in order.
    assert flush(_incus(), tmp_path, FULL, uid=1000) == 2
    assert [s.id for s in store.unacked("uuid-1")] == ["h-0", "h-1"]
    assert not outbox.has_pending(tmp_path, FULL)


def test_flush_store_recipient_moves_plain_and_finish_in_name_order(tmp_path: Path) -> None:
    store = _store_recipient(tmp_path)
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / "a.json").write_text(_item("a").model_dump_json(by_alias=True))
    (directory / "b.finish.json").write_text(_finish_item().model_dump_json())
    (directory / "c.json").write_text(_item("c").model_dump_json(by_alias=True))
    finish = MagicMock(return_value=_finished_message())
    incus = _incus()
    assert flush(incus, tmp_path, FULL, uid=1000, finish=finish) == 3
    assert [s.id for s in store.unacked("uuid-1")] == ["h-a", "h-f", "h-c"]
    incus.exec_with_input.assert_not_called()
    assert list(directory.iterdir()) == []


def test_flush_store_recipient_without_finish_stops_at_finish(tmp_path: Path) -> None:
    store = _store_recipient(tmp_path)
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / "a.json").write_text(_item("a").model_dump_json(by_alias=True))
    (directory / "b.finish.json").write_text(_finish_item().model_dump_json())
    (directory / "c.json").write_text(_item("c").model_dump_json(by_alias=True))
    assert flush(_incus(), tmp_path, FULL, uid=1000) == 1
    assert [s.id for s in store.unacked("uuid-1")] == ["h-a"]
    assert (directory / "b.finish.json").is_file()
    assert (directory / "c.json").is_file()


def test_flush_store_recipient_stops_at_store_error_and_keeps_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration import store as store_mod

    _store_recipient(tmp_path)
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / "a.json").write_text(_item("a").model_dump_json(by_alias=True))
    (directory / "b.json").write_text(_item("b").model_dump_json(by_alias=True))

    def boom(self, *a, **k):
        raise store_mod.StoreError("locked")

    monkeypatch.setattr(store_mod.BrokerStore, "insert_item", boom)
    assert flush(_incus(), tmp_path, FULL, uid=1000) == 0
    assert sorted(p.name for p in directory.iterdir()) == ["a.json", "b.json"]


def test_flush_store_recipient_quarantines_invalid_and_notes_in_store(tmp_path: Path) -> None:
    store = _store_recipient(tmp_path)
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / "a.finish.json").write_text("invalid")
    (directory / "b.json").write_text("not json")
    (directory / "c.json").write_text(_item("c").model_dump_json(by_alias=True))
    incus = _incus()
    assert flush(incus, tmp_path, FULL, uid=1000, finish=MagicMock()) == 1
    incus.exec_with_input.assert_not_called()
    items = store.unacked("uuid-1")
    kinds = [(i.type, i.kind_or_state) for i in items]
    assert kinds.count(("message", "note")) == 2
    assert [i.id for i in items if i.type == "status"] == ["h-c"]
    assert (directory / "a.finish.json.bad").is_file()
    assert (directory / "b.json.bad").is_file()
    assert not outbox.has_pending(tmp_path, FULL)


def test_quarantine_note_falls_back_to_pending_when_store_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration import store as store_mod

    _store_recipient(tmp_path)
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    (directory / "a.finish.json").write_text("invalid")

    def boom(self, *a, **k):
        raise store_mod.StoreError("locked")

    monkeypatch.setattr(store_mod.BrokerStore, "insert_item", boom)
    flush(_incus(), tmp_path, FULL, uid=1000, finish=MagicMock())
    parked = [p for p in directory.iterdir() if p.name.endswith(".json")]
    assert len(parked) == 1
    assert json.loads(parked[0].read_text())["kind"] == "note"


def test_deliver_parks_when_the_store_lookup_is_unreadable(tmp_path: Path) -> None:
    from jailbee.orchestration.store import store_path

    store_path(tmp_path).write_bytes(b"garbage" * 100)
    incus = _incus()
    result = deliver(incus, tmp_path, FULL, _item("1"), running=True, uid=1000, now=AT)
    assert result == "parked"
    incus.exec_with_input.assert_not_called()


def test_deliver_store_error_parks_item(tmp_path: Path, mocker) -> None:
    from jailbee.orchestration.store import BrokerStore, StoreError

    _store_recipient(tmp_path)
    mocker.patch.object(BrokerStore, "insert_item", side_effect=StoreError("boom"))
    result = deliver(_incus(), tmp_path, FULL, _item("1"), running=True, uid=1000, now=AT)
    assert result == "parked"
    assert (pending_dir(tmp_path, FULL) / inbox_file_name(_item("1"), AT)).exists()


def deliver(incus, root, full, item, **kw):
    return outbox.deliver(incus, root, full, item, recipient_uuid="uuid-1", **kw)


def flush(incus, root, full, **kw):
    return outbox.flush_pending(incus, root, full, recipient_uuid="uuid-1", **kw)


def test_launcher_check_never_creates_transport_directories():
    incus = _incus()
    outbox.ensure_orchestration_env(incus, FULL, uid=1000)
    incus.exec.assert_called_once_with(FULL, ["test", "-x", outbox.CONTAINER_JAILBEE])
    incus.exec_with_input.assert_not_called()


@pytest.mark.parametrize("running", [True, False])
def test_no_socket_row_needed_for_verified_delivery(tmp_path, running):
    from jailbee.orchestration.store import BrokerStore, store_path

    store = BrokerStore(store_path(tmp_path))
    incus = _incus()
    assert (
        deliver(incus, tmp_path, FULL, _item("1"), running=running, uid=None, now=AT) == "delivered"
    )
    assert [row.id for row in store.unacked("uuid-1")] == ["h-1"]
    assert store.recipients() == {}  # Delivery never grants socket access.
    incus.exec.assert_not_called()
    incus.exec_with_input.assert_not_called()


def test_missing_identity_parks_never_uses_namesakes_socket_row(tmp_path):
    store = _store_recipient(tmp_path)
    incus = _incus()
    assert (
        outbox.deliver(incus, tmp_path, FULL, _item("1"), running=True, uid=None, now=AT)
        == "parked"
    )
    assert store.unacked("uuid-1") == []
    assert outbox.flush_pending(incus, tmp_path, FULL, uid=None) == 0
    assert outbox.has_pending(tmp_path, FULL)
    incus.exec.assert_not_called()
    incus.exec_with_input.assert_not_called()


def test_verified_identity_ignores_namesakes_stale_socket_row(tmp_path):
    store = _store_recipient(tmp_path)
    assert (
        outbox.deliver(
            _incus(),
            tmp_path,
            FULL,
            _item("1"),
            running=False,
            uid=None,
            now=AT,
            recipient_uuid="uuid-new",
        )
        == "delivered"
    )
    assert store.unacked("uuid-1") == []
    assert [row.id for row in store.unacked("uuid-new")] == ["h-1"]
    assert store.recipients() == {FULL: "uuid-1"}


def test_flush_retry_after_insert_before_unlink_does_not_duplicate(tmp_path, monkeypatch):
    store = _store_recipient(tmp_path)
    directory = pending_dir(tmp_path, FULL)
    directory.mkdir(parents=True)
    path = directory / "a.json"
    path.write_text(_item("a").model_dump_json())
    with monkeypatch.context() as patch:
        patch.setattr(type(path), "unlink", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(OSError):
            flush(_incus(), tmp_path, FULL, uid=None)
    assert path.exists()
    assert flush(_incus(), tmp_path, FULL, uid=None) == 1
    assert [row.id for row in store.unacked("uuid-1")] == ["h-a"]


def test_sequence_orders_spooled_items_not_random_ids(tmp_path):
    store = _store_recipient(tmp_path)
    first, second = _item("ffff"), _item("0000")
    for seq, item in enumerate((first, second)):
        outbox.deliver(_incus(), tmp_path, FULL, item, running=False, uid=None, now=AT, seq=seq)
    assert flush(_incus(), tmp_path, FULL, uid=None) == 2
    assert [row.id for row in store.unacked("uuid-1")] == [first.id, second.id]
