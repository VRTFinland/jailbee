# ruff: noqa: F811
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from jailbee.incus import IncusError
from jailbee.orchestration import broker, outbox
from jailbee.orchestration.outbox import INBOX_DIR, INBOX_READ_DIR
from jailbee.orchestration.protocol import inbox_file_name_for
from tests.test_orchestration_socket_device import UUID, Env, env, orch  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

    from tests.test_orchestration_broker import Rig  # noqa: F401

FULL = "proj-orch"


def item(id_: str, text: str = "hi") -> str:
    return json.dumps({"version": 1, "id": id_, "type": "message", "kind": "info", "text": text})


class Dirs:
    """A fake container filesystem behind the broker's inbox read/delete seams."""

    def __init__(self, env: Env) -> None:
        self.files: dict[str, dict[str, str]] = {INBOX_DIR: {}, INBOX_READ_DIR: {}}
        self.fail_read = False
        self.fail_delete = False
        self.deleted: list[tuple[str, list[str]]] = []
        env.rig.read_inbox_dir.side_effect = self._read
        env.rig.delete_inbox_files.side_effect = self._delete

    def _read(
        self, incus: object, full: str, directory: str, *, uid: int | None, limit: int
    ) -> dict[str, str]:
        if self.fail_read:
            raise IncusError("container is stopped")
        files = self.files[directory]
        names = sorted(n for n in files if not n.startswith("."))  # like the real helper
        return {n: files[n] for n in names[:limit]}

    def _delete(
        self, incus: object, full: str, directory: str, names: list[str], *, uid: int | None
    ) -> None:
        if self.fail_delete:
            raise IncusError("rm failed")
        self.deleted.append((directory, list(names)))
        for n in names:
            self.files[directory].pop(n, None)


@pytest.fixture
def dirs(env: Env) -> Dirs:
    return Dirs(env)


def name(stamp: str, seq: int, id_: str) -> str:
    return f"{stamp}-{seq:08d}-{id_}.json"


def test_unread_unacked_read_acked_in_name_order_ids_kept(env: Env, dirs: Dirs) -> None:
    dirs.files[INBOX_DIR] = {
        name("20261101T100000Z", 2, "b"): item("b"),
        name("20261101T100000Z", 1, "a"): item("a"),
        ".tmp-x.json": item("t"),
        name("20261101T100000Z", 3, "bad"): "{not json",
    }
    dirs.files[INBOX_READ_DIR] = {
        name("20261031T090000Z", 1, "r1"): item("r1"),
        name("20261031T090000Z", 2, "r2"): item("r2"),
    }
    env.set(orch())
    env.tick()
    st = env.store
    assert [r.id for r in st.unacked(UUID)] == ["a", "b"]
    hist = st.history(UUID)
    assert [r.id for r in hist] == ["r1", "r2", "a", "b"]
    assert [r.acked_at is not None for r in hist] == [True, True, False, False]
    assert hist[0].created_at == 1793437200.0  # parsed from the file name
    assert st.recipients()[FULL] == UUID
    # migrated files are gone; unparsable and dot files are left alone
    assert dirs.files[INBOX_READ_DIR] == {}
    assert set(dirs.files[INBOX_DIR]) == {".tmp-x.json", name("20261101T100000Z", 3, "bad")}


def test_second_serve_does_not_migrate_again(env: Env, dirs: Dirs) -> None:
    dirs.files[INBOX_DIR] = {name("20261101T100000Z", 1, "a"): item("a")}
    env.set(orch())
    env.tick()
    env.rig.read_inbox_dir.reset_mock()
    env.memory.attached.clear()  # force _ensure_socket to run the setup again
    dirs.files[INBOX_DIR][name("20261101T110000Z", 1, "late")] = item("late")
    env.tick()
    env.rig.read_inbox_dir.assert_not_called()
    assert [r.id for r in env.store.unacked(UUID)] == ["a"]


def test_insert_failure_removes_nothing_and_retries(
    env: Env, dirs: Dirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration.store import BrokerStore, StoreError

    dirs.files[INBOX_DIR] = {name("20261101T100000Z", 1, "a"): item("a")}
    real = BrokerStore.insert_item

    def boom(self: BrokerStore, *a: Any, **k: Any) -> bool:
        raise StoreError("disk full")

    monkeypatch.setattr(BrokerStore, "insert_item", boom)
    env.set(orch())
    env.tick()
    assert dirs.deleted == []
    assert env.store.recipients() == {}
    monkeypatch.setattr(BrokerStore, "insert_item", real)
    env.tick()
    assert [r.id for r in env.store.unacked(UUID)] == ["a"]
    assert env.store.recipients()[FULL] == UUID
    assert dirs.files[INBOX_DIR] == {}


def test_delete_failure_keeps_the_row_unwritten_and_does_not_duplicate(
    env: Env, dirs: Dirs
) -> None:
    dirs.files[INBOX_DIR] = {name("20261101T100000Z", 1, "a"): item("a")}
    dirs.fail_delete = True
    env.set(orch())
    env.tick()
    assert env.store.recipients() == {}
    dirs.fail_delete = False
    env.tick()
    assert [r.id for r in env.store.history(UUID)] == ["a"]
    assert env.store.recipients()[FULL] == UUID


def test_unreadable_container_writes_no_row_and_retries(env: Env, dirs: Dirs) -> None:
    dirs.files[INBOX_DIR] = {name("20261101T100000Z", 1, "a"): item("a")}
    dirs.fail_read = True
    env.set(orch())
    env.tick()
    assert env.store.recipients() == {}
    assert env.store.history(UUID) == []
    dirs.fail_read = False
    env.tick()
    assert [r.id for r in env.store.unacked(UUID)] == ["a"]


def test_stopped_container_is_not_migrated(env: Env, dirs: Dirs) -> None:
    ctx_node = orch()
    ctx_node["status"] = "Stopped"
    env.set(ctx_node)
    env.tick()
    env.rig.read_inbox_dir.assert_not_called()
    assert env.store.recipients() == {}


def test_paging_reads_every_file(env: Env, dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(broker, "MIGRATE_PAGE", 2)
    dirs.files[INBOX_DIR] = {name("20261101T100000Z", i, f"i{i}"): item(f"i{i}") for i in range(5)}
    env.set(orch())
    env.tick()
    assert [r.id for r in env.store.unacked(UUID)] == [f"i{i}" for i in range(5)]
    assert dirs.files[INBOX_DIR] == {}


def test_pending_items_land_after_migrated_ones(
    env: Env, dirs: Dirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jailbee.orchestration.state import pending_dir

    monkeypatch.setattr(broker, "flush_pending", outbox.flush_pending)
    dirs.files[INBOX_DIR] = {name("20261101T100000Z", 1, "a"): item("a")}
    pdir = pending_dir(env.rig.root, FULL)
    pdir.mkdir(parents=True)
    (pdir / name("20261102T100000Z", 1, "p")).write_text(item("p"))
    env.set(orch())
    env.tick()
    assert [r.id for r in env.store.unacked(UUID)] == ["a", "p"]
    assert not list(pdir.iterdir())


def test_stamp_time_falls_back() -> None:
    assert broker._stamp_time("garbage.json", 7.0) == 7.0
    n = inbox_file_name_for(
        "x", __import__("datetime").datetime(2026, 1, 2, 3, 4, 5, tzinfo=__import__("datetime").UTC)
    )
    assert broker._stamp_time(n, 0.0) == 1767323045.0


def test_inbox_helpers(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    incus = MagicMock()
    outbox.delete_inbox_files(incus, FULL, INBOX_READ_DIR, ["a.json", "b.json"], uid=1000)
    incus.exec.assert_called_once_with(
        FULL, ["rm", "-f", "--", "a.json", "b.json"], uid=1000, cwd=INBOX_READ_DIR
    )
    with pytest.raises(ValueError):
        outbox.delete_inbox_files(incus, FULL, INBOX_DIR, ["../x"], uid=None)
    incus = MagicMock()
    incus.exec_capped.return_value = ""
    assert outbox.read_inbox_dir(incus, FULL, INBOX_DIR, uid=1, limit=5) == {}
    assert incus.exec_capped.call_args.args[1][4] == INBOX_DIR
