from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from jailbee.orchestration import broker_state, socket_ops, wire
from jailbee.orchestration.actions import Outcome
from jailbee.orchestration.socket_ops import NextArgs, Waiter, Waiters
from jailbee.orchestration.store import BrokerStore, StoreError
from tests.test_orchestration_broker import Rig, _child, _orch

if TYPE_CHECKING:
    from pathlib import Path


class Env:
    def __init__(self, rig: Rig) -> None:
        rig.incus.list_containers.return_value = [_orch(), _child()]
        rig.dispatch.return_value = Outcome("done", "delivered")
        self.memory = broker_state.BrokerMemory()
        rig.tick(self.memory)
        self.store = BrokerStore(rig.root / "broker.db")
        self.memory.children["proj-orch"].children[0]["started_at"] = 500.0
        self.orch = self._key("proj-orch")
        self.kid = self._key("proj-kid")

    def _key(self, full: str) -> str:
        return next(k for k, c in self.memory.contexts.items() if c.sender.full == full)

    def put(
        self, mid: str, *, kind: str = "note", frm: str | None = None, at: float = 100.0
    ) -> None:
        item: dict[str, object] = {"type": "message", "id": mid, "kind": kind}
        if frm:
            item["from"] = frm
        self.store.insert_item(self.orch, "proj-orch", item, now=at)

    def waiter(self, **kw: object) -> Waiter:
        base: dict[str, object] = {
            "uuid": self.orch,
            "conn_id": 1,
            "wake": None,
            "quiet": 0,
            "digest": 0,
            "deadline": None,
        }
        return Waiter(**{**base, **kw})  # type: ignore[arg-type]

    def evaluate(self, ws: Waiters, now: float = 1000.0, mono: float = 0.0):  # type: ignore[no-untyped-def]
        view = self.memory.children.get("proj-orch")
        if view is not None:  # the broker republishes the list every round
            self.memory.children["proj-orch"] = broker_state.ChildrenView(now, view.children)
        return ws.evaluate(self.memory, self.store, now=now, mono=mono)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(Rig(tmp_path, monkeypatch))


def test_list_in_seq_order_and_unacked_only(env: Env) -> None:
    env.put("b")
    env.put("a")
    env.put("c")
    env.store.ack(env.orch, "a", now=1.0)
    assert [i["id"] for i in socket_ops.handle_list(env.store, env.orch, after=0)["items"]] == [
        "b",
        "c",
    ]


def test_read_acks_keep_does_not_and_unknown(env: Env) -> None:
    env.put("m1")
    env.put("m2")
    item = socket_ops.handle_read(env.store, env.orch, id="m1", keep=True, now=5.0)
    assert item["id"] == "m1"
    assert len(socket_ops.handle_list(env.store, env.orch, after=0)["items"]) == 2
    socket_ops.handle_read(env.store, env.orch, id="m1", keep=False, now=5.0)
    assert [i["id"] for i in socket_ops.handle_list(env.store, env.orch, after=0)["items"]] == [
        "m2"
    ]
    for bad in ("m1", "nope"):
        with pytest.raises(wire.WireError) as e:
            socket_ops.handle_read(env.store, env.orch, id=bad, keep=False, now=6.0)
        assert e.value.code == wire.NOT_FOUND
    with pytest.raises(wire.WireError) as e2:
        socket_ops.handle_read(env.store, env.orch, id=3, keep=False, now=6.0)
    assert e2.value.code == wire.BAD_REQUEST
    with pytest.raises(wire.WireError) as e3:
        socket_ops.handle_read(env.store, env.orch, id="m2", keep="no", now=6.0)
    assert e3.value.code == wire.BAD_REQUEST


def test_children_status_grant_for_orchestrator(env: Env) -> None:
    env.put("m1", frm="kid", at=123.0)
    res = socket_ops.handle_children(env.memory, env.store, env.orch)
    assert [c["name"] for c in res["children"]] == ["kid"]  # type: ignore[attr-defined]
    assert res["last_from_child"] == {"kid": 123.0}
    assert isinstance(res["observed_at"], float)
    one = socket_ops.handle_status(env.memory, env.store, env.orch, child="kid")
    assert len(one["children"]) == 1  # type: ignore[arg-type]
    with pytest.raises(wire.WireError) as e:
        socket_ops.handle_status(env.memory, env.store, env.orch, child="ghost")
    assert (e.value.code, e.value.detail) == (wire.NOT_FOUND, "unknown child 'ghost'")
    assert isinstance(socket_ops.handle_grant(env.memory, env.orch), dict)


def test_missing_view_gives_empty_children(env: Env) -> None:
    env.memory.children.clear()
    res = socket_ops.handle_children(env.memory, env.store, env.orch)
    assert res["children"] == [] and res["observed_at"] is None


def test_child_and_unknown_uuid_are_not_orchestrated(env: Env) -> None:
    calls = [
        lambda u: socket_ops.handle_children(env.memory, env.store, u),
        lambda u: socket_ops.handle_status(env.memory, env.store, u, child=None),
        lambda u: socket_ops.handle_grant(env.memory, u),
    ]
    for call in calls:
        for u in (env.kid, "missing"):
            with pytest.raises(wire.WireError) as e:
                call(u)
            assert e.value.code == wire.NOT_ORCHESTRATED


def test_next_args_validation() -> None:
    ok = socket_ops.handle_next_args(
        {"wake_on": ["done"], "quiet_after": 5, "digest": 7, "timeout": 2.5}
    )
    assert ok == NextArgs(frozenset({"done"}), 5, 7, 2.5)
    assert socket_ops.handle_next_args({}) == NextArgs(None, 0, 0, None)
    for bad in (
        {"wake_on": "done"},
        {"wake_on": ["bogus-token"]},
        {"wake_on": [1]},
        {"quiet_after": 0},
        {"quiet_after": True},
        {"digest": -1},
        {"digest": 1.5},
        {"timeout": -1},
        {"timeout": "x"},
        {"timeout": True},
        {"replace": 1},
        {"replace": None},
        {"waiter": "123"},
        {"waiter": {}},
        {"waiter": {"pid": True, "started_at": "now", "command": "wait"}},
        {"waiter": {"pid": -1, "started_at": "now", "command": "wait"}},
        {"waiter": {"pid": 123, "started_at": 1, "command": "wait"}},
        {"waiter": {"pid": 123, "started_at": "now", "command": ""}},
        {"waiter": {"pid": 123, "started_at": "now", "command": "x" * 1025}},
    ):
        with pytest.raises(wire.WireError) as e:
            socket_ops.handle_next_args(bad)
        assert e.value.code == wire.BAD_REQUEST, bad


def test_waiter_identity_strips_terminal_controls() -> None:
    parsed = socket_ops.handle_next_args(
        {"replace": True, "waiter": {"pid": 123, "started_at": "now\n", "command": "wait\x1b\x9b"}}
    )
    assert parsed.replace is True
    assert parsed.info is not None
    assert parsed.info.describe() == "PID 123, wait started now, command: wait"


def test_busy_and_drop(env: Env) -> None:
    ws = Waiters()
    ws.add(env.waiter())
    with pytest.raises(wire.WireError) as e:
        ws.add(env.waiter(conn_id=2))
    assert e.value.code == wire.BUSY
    ws.drop(env.orch, 99)
    assert len(ws) == 1
    ws.drop(env.orch, 1)
    ws.add(env.waiter(conn_id=2))


def test_immediate_message_answer(env: Env) -> None:
    env.put("m1")
    ws = Waiters()
    ws.add(env.waiter())
    [(w, res)] = env.evaluate(ws)
    assert res == {"kind": "message", "id": "m1"} and w.conn_id == 1
    assert len(ws) == 0


def test_filter_skips_non_matching(env: Env) -> None:
    env.put("m1", kind="note")
    ws = Waiters()
    ws.add(env.waiter(wake=frozenset({"question"})))
    assert env.evaluate(ws) == []
    env.put("m2", kind="question")
    assert env.evaluate(ws, mono=1.0) == []  # match found after waiting: settles
    [(_, res)] = env.evaluate(ws, mono=2.0)
    assert res == {"kind": "message", "id": "m2"}


def test_settle_delays_and_reselects(env: Env) -> None:
    ws = Waiters()
    ws.add(env.waiter())
    assert env.evaluate(ws, mono=0.0) == []
    env.put("m2")
    assert env.evaluate(ws, mono=1.0) == []  # m2 found: settle until 2.0
    env.store.ack(env.orch, "m2", now=1.2)  # the first pick is taken meanwhile
    env.store.insert_item(
        env.orch, "proj-orch", {"type": "message", "id": "m1", "kind": "n"}, now=50.0
    )
    assert env.evaluate(ws, mono=1.5) == []
    [(_, res)] = env.evaluate(ws, mono=2.0)
    assert res == {"kind": "message", "id": "m1"}  # re-selected, not cached


def test_timeout(env: Env) -> None:
    ws = Waiters()
    ws.add(env.waiter(deadline=5.0))
    assert env.evaluate(ws, mono=4.0) == []
    [(_, res)] = env.evaluate(ws, mono=5.0)
    assert res == {"kind": "timeout"}
    ws.add(env.waiter(deadline=0.0))
    [(_, res)] = env.evaluate(ws, mono=0.0)
    assert res == {"kind": "timeout"}


def test_quiet_child(env: Env) -> None:
    started = 500.0
    ws = Waiters()
    ws.add(env.waiter(quiet=10))
    assert env.evaluate(ws, now=started + 1) == []
    assert env.evaluate(ws, now=started + 11, mono=1.0) == []  # settling
    [(_, res)] = env.evaluate(ws, now=started + 12, mono=2.5)
    assert res == {"kind": "quiet-child", "child": "kid"}


def test_digest_first_stamps_then_fires(env: Env) -> None:
    ws = Waiters()
    ws.add(env.waiter(digest=10))
    assert env.evaluate(ws, now=1000.0) == []
    assert env.store.get_digest_at(env.orch) == 1000.0
    assert env.evaluate(ws, now=1005.0, mono=1.0) == []
    assert env.evaluate(ws, now=1011.0, mono=2.0) == []  # settling
    assert env.store.get_digest_at(env.orch) == 1000.0
    [(_, res)] = env.evaluate(ws, now=1013.0, mono=3.5)
    assert res["kind"] == "digest"
    assert [c["name"] for c in res["children"]] == ["kid"]  # type: ignore[attr-defined]
    assert res["last_from_child"] == {}
    assert env.store.get_digest_at(env.orch) == 1013.0


def test_settle_reselect_replacing_digest_keeps_the_stamp(env: Env) -> None:
    ws = Waiters()
    ws.add(env.waiter(digest=10))
    assert env.evaluate(ws, now=1000.0) == []
    assert env.store.get_digest_at(env.orch) == 1000.0
    assert env.evaluate(ws, now=1011.0, mono=1.0) == []  # digest found: settling
    env.put("m1", at=1011.5)
    [(_, res)] = env.evaluate(ws, now=1012.5, mono=2.5)
    assert res == {"kind": "message", "id": "m1"}
    assert env.store.get_digest_at(env.orch) == 1000.0


def test_list_stays_under_wire_cap_and_keeps_ls_line(env: Env) -> None:
    from jailbee.inbox.render import ls_line

    big = "x" * (100 * 1024)
    for n in range(20):
        item: dict[str, object] = {
            "type": "message",
            "id": f"m{n:02d}",
            "kind": "note",
            "from": "kid",
            "body": big,
            "extra": big,
            "imported_sha": "a" * 40,
            "on_done_reason": big,
            "import_error": big,
        }
        env.store.insert_item(env.orch, "proj-orch", item, now=100.0 + n)
    full = [r.item for r in env.store.unacked(env.orch)]
    got = socket_ops.handle_list(env.store, env.orch, after=0)["items"]
    resp = wire.encode_response(wire.Response(ok=True, result=got))
    assert len(resp) < wire.MAX_LINE_BYTES
    assert [ls_line(i) for i in got] == [ls_line(i) for i in full]


def test_quiet_without_children_view_is_bad_request(env: Env) -> None:
    env.memory.children.clear()
    ws = Waiters()
    ws.add(env.waiter(quiet=5))
    [(_, res)] = env.evaluate(ws)
    assert isinstance(res, wire.WireError) and res.code == wire.BAD_REQUEST


def test_a_store_error_answers_only_that_waiter(env: Env, monkeypatch) -> None:
    env.store.insert_item(
        env.kid, "proj-kid", {"type": "message", "id": "k1", "kind": "n"}, now=1.0
    )
    ws = Waiters()
    ws.add(env.waiter(uuid=env.orch, conn_id=1))
    ws.add(env.waiter(uuid=env.kid, conn_id=2))
    real = env.store.unacked

    def unacked(uuid: str):  # type: ignore[no-untyped-def]
        if uuid == env.orch:
            raise StoreError("locked")
        return real(uuid)

    monkeypatch.setattr(env.store, "unacked", unacked)
    answered = {w.conn_id: res for w, res in env.evaluate(ws)}
    assert answered[2] == {"kind": "message", "id": "k1"}
    err = answered[1]
    assert isinstance(err, wire.WireError) and err.code == wire.STORE
    assert len(ws) == 0


@pytest.mark.parametrize("kw", [{"quiet": 5}, {"digest": 5}])
def test_a_waiter_that_lost_its_context_is_not_orchestrated(env: Env, kw: dict[str, int]) -> None:
    ws = Waiters()
    ws.add(env.waiter(**kw))
    del env.memory.contexts[env.orch]
    [(_, res)] = env.evaluate(ws)
    assert isinstance(res, wire.WireError) and res.code == wire.NOT_ORCHESTRATED


def test_a_plain_waiter_without_context_keeps_waiting(env: Env) -> None:
    ws = Waiters()
    ws.add(env.waiter())
    del env.memory.contexts[env.orch]
    assert env.evaluate(ws) == []
    assert len(ws) == 1


def test_ack_marks_read_and_unknown_is_not_found(env: Env) -> None:
    env.put("m1")
    assert socket_ops.handle_ack(env.store, env.orch, id="m1", now=5.0) == {"acked": True}
    assert socket_ops.handle_list(env.store, env.orch, after=0)["items"] == []
    # acking twice is harmless
    assert socket_ops.handle_ack(env.store, env.orch, id="m1", now=6.0) == {"acked": True}
    with pytest.raises(wire.WireError) as e:
        socket_ops.handle_ack(env.store, env.orch, id="nope", now=6.0)
    assert e.value.code == wire.NOT_FOUND
    with pytest.raises(wire.WireError) as e2:
        socket_ops.handle_ack(env.store, env.orch, id=None, now=6.0)
    assert e2.value.code == wire.BAD_REQUEST


def test_waiter_evaluation_and_children_do_not_load_item_json(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(uuid: str):  # type: ignore[no-untyped-def]
        raise AssertionError("history() loads every item_json")

    monkeypatch.setattr(env.store, "history", boom)
    env.put("a", frm="kid", at=900.0)
    ws = Waiters()
    ws.add(env.waiter(quiet=60))
    assert env.evaluate(ws, now=1000.0)  # answered without history()
    assert socket_ops.handle_children(env.memory, env.store, env.orch)["last_from_child"] == {
        "kid": 900.0
    }


def _fill_over_a_mebibyte(env: Env) -> list[str]:
    ids = [f"msg-{n:05d}-{'f' * 40}" for n in range(5000)]
    with env.store._tx() as con:  # one transaction: 5 000 inserts stay fast
        con.executemany(
            "INSERT INTO items(recipient_uuid, recipient_full, id, type, kind_or_state, "
            "created_at, item_json) VALUES(?,?,?,?,?,?,?)",
            [
                (
                    env.orch,
                    "proj-orch",
                    i,
                    "message",
                    "note",
                    100.0,
                    json.dumps({"type": "message", "id": i, "kind": "note", "body": "b" * 300}),
                )
                for i in ids
            ],
        )
    return ids


def test_paged_list_walks_more_than_a_line_of_unread(env: Env) -> None:
    ids = _fill_over_a_mebibyte(env)
    seen: list[str] = []
    after: object = 0
    pages = 0
    while after is not None:
        page = socket_ops.handle_list(env.store, env.orch, after=after)
        assert isinstance(page, dict)
        assert len(wire.encode_response(wire.ok(page))) < wire.MAX_LINE_BYTES
        seen += [str(i["id"]) for i in page["items"]]
        after = page["next"]
        pages += 1
    assert seen == ids
    assert pages > 1


def test_paged_list_rejects_a_bad_cursor(env: Env) -> None:
    for bad in (None, -1, "x", 1.5, True):
        with pytest.raises(wire.WireError) as exc:
            socket_ops.handle_list(env.store, env.orch, after=bad)
        assert exc.value.code == wire.BAD_REQUEST
