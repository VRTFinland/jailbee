from __future__ import annotations

import queue
from typing import TYPE_CHECKING, Any

import pytest

from jailbee.orchestration import broker, executor, socket_ops, wire
from jailbee.orchestration.actions import Outcome
from jailbee.orchestration.listener import DisconnectEvent, RequestEvent
from jailbee.orchestration.store import BrokerStore
from tests.test_orchestration_broker import Rig, _child, _orch

if TYPE_CHECKING:
    from pathlib import Path


class FakeListener:
    def __init__(self) -> None:
        self.responses: list[tuple[int, wire.Response]] = []
        self.dropped: set[int] = set()
        self.released: list[int] = []

    def respond(self, conn_id: int, response: wire.Response) -> None:
        self.responses.append((conn_id, response))

    def is_connected(self, conn_id: int) -> bool:
        return conn_id not in self.dropped

    def release(self, conn_id: int) -> None:
        self.released.append(conn_id)


class Env:
    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        rig.incus.list_containers.return_value = [_orch(), _child()]
        rig.dispatch.return_value = Outcome("done", "delivered")
        self.memory = broker.BrokerMemory()
        rig.tick(self.memory)
        self.store = BrokerStore(rig.root / "broker.db")
        self.memory.store = self.store
        self.listener = FakeListener()
        self.memory.listener = self.listener  # type: ignore[assignment]
        self.orch = self._key("proj-orch")
        self.kid = self._key("proj-kid")
        for full, uuid in (("proj-orch", self.orch), ("proj-kid", self.kid)):
            self.store.set_recipient(full, uuid, now=1.0)
        self.store.set_recipient("proj-unknown", "unknown-uuid", now=1.0)

    def _key(self, full: str) -> str:
        return next(k for k, c in self.memory.contexts.items() if c.sender.full == full)

    def ask(self, uuid: str, op: str, conn: int = 1, **args: Any) -> None:
        req = wire.Request(op, args)
        executor.handle_event(self.memory, RequestEvent(uuid, conn, req))

    def put(self, mid: str, at: float = 100.0) -> None:
        item: dict[str, object] = {"type": "message", "id": mid, "kind": "note"}
        self.store.insert_item(self.orch, "proj-orch", item, now=at)

    def last(self) -> wire.Response:
        return self.listener.responses[-1][1]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(Rig(tmp_path, monkeypatch))


def _manifest() -> dict[str, object]:
    return {"version": 1, "id": "m1", "type": "message", "kind": "note", "body": "hi"}


def test_list_is_answered(env: Env) -> None:
    env.put("a")
    env.ask(env.orch, "list", conn=7, after=0)
    conn, resp = env.listener.responses[-1]
    assert conn == 7 and resp.ok
    assert [i["id"] for i in resp.result["items"]] == ["a"]


def test_send_without_context(env: Env) -> None:
    env.ask("unknown-uuid", "send", manifest=_manifest())
    assert env.last().error_code == wire.NOT_ORCHESTRATED


def test_send_with_context_answers_status(env: Env) -> None:
    env.ask(env.kid, "send", manifest=_manifest(), body=None, wait=True)
    resp = env.last()
    assert resp.ok and resp.result["type"] == "status" and resp.result["ref"] == "m1"
    env.rig.dispatch.assert_called_once()


def test_next_with_unread_item_answers_at_once(env: Env) -> None:
    env.put("a")
    env.ask(env.orch, "next")
    assert env.listener.responses == []  # parked until evaluated
    executor.evaluate_waiters(env.memory)
    assert env.last().result == {"kind": "message", "id": "a"}


def test_next_parks_then_wakes_on_insert(env: Env) -> None:
    env.ask(env.orch, "next", conn=3)
    executor.evaluate_waiters(env.memory)
    assert env.listener.responses == []
    assert len(env.memory.waiters) == 1
    env.put("late")
    t = [0.0]
    executor.evaluate_waiters(env.memory, clock=lambda: t[0])  # starts the settle
    t[0] = 5.0
    executor.evaluate_waiters(env.memory, clock=lambda: t[0])
    conn, resp = env.listener.responses[-1]
    assert conn == 3 and resp.result == {"kind": "message", "id": "late"}
    assert len(env.memory.waiters) == 0


def test_second_next_is_busy(env: Env) -> None:
    env.ask(env.orch, "next", conn=1)
    env.ask(env.orch, "next", conn=2)
    conn, resp = env.listener.responses[-1]
    assert conn == 2 and resp.error_code == wire.BUSY


def test_disconnect_drops_waiter(env: Env) -> None:
    env.ask(env.orch, "next", conn=1)
    executor.handle_event(env.memory, DisconnectEvent(env.orch, 1))
    assert len(env.memory.waiters) == 0


def test_disconnect_releases_the_parked_request_s_budget(env: Env) -> None:
    env.ask(env.orch, "next", conn=1)
    env.listener.dropped.add(1)
    executor.handle_event(env.memory, DisconnectEvent(env.orch, 1))
    assert env.listener.released == [1]


def test_a_dropped_connection_s_request_is_skipped_and_released(env: Env) -> None:
    """Open, send, close: the request must not run, and its budget must come back."""
    env.listener.dropped.add(5)
    env.ask(env.kid, "send", conn=5, manifest=_manifest(), body=None, wait=False)
    env.ask(env.orch, "next", conn=5)
    env.rig.dispatch.assert_not_called()
    assert len(env.memory.waiters) == 0
    assert env.listener.responses == []
    assert env.listener.released == [5, 5]


def test_next_timeout(env: Env) -> None:
    t = [100.0]
    executor.handle_event(
        env.memory,
        RequestEvent(env.orch, 4, wire.Request("next", {"timeout": 10})),
        clock=lambda: t[0],
    )
    executor.evaluate_waiters(env.memory, clock=lambda: t[0])
    assert env.listener.responses == []
    t[0] = 111.0
    executor.evaluate_waiters(env.memory, clock=lambda: t[0])
    conn, resp = env.listener.responses[-1]
    assert conn == 4 and resp.result == {"kind": "timeout"}


def test_unknown_op(env: Env) -> None:
    env.ask(env.orch, "frobnicate")
    assert env.last().error_code == wire.BAD_REQUEST


def test_handler_exception_answers_error_and_loop_survives(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("kaput")

    monkeypatch.setattr(socket_ops, "handle_list", boom)
    env.memory.requests.put(RequestEvent(env.orch, 1, wire.Request("list")))
    env.memory.requests.put(RequestEvent(env.orch, 2, wire.Request("grant")))
    ticks = iter([0.0, 0.0, 0.0, 10.0])
    executor.serve_requests(
        env.memory, until=5.0, stop=broker.StopFlag(), clock=lambda: next(ticks, 10.0)
    )
    assert env.listener.responses[0][1].error_code == wire.REFUSED
    assert env.listener.responses[0][1].error_detail == "broker error"
    assert len(env.listener.responses) == 2


def test_serve_returns_when_until_passes(env: Env) -> None:
    env.memory.requests = queue.Queue()
    executor.serve_requests(env.memory, until=0.0, stop=broker.StopFlag(), clock=lambda: 1.0)


def test_serve_returns_on_stop(env: Env) -> None:
    stop = broker.StopFlag()
    stop.requested = True
    executor.serve_requests(env.memory, until=1e12, stop=stop)


def test_ack_op_acks_and_unknown_is_not_found(env: Env) -> None:
    env.put("a")
    env.ask(env.orch, "ack", id="a")
    assert env.last().ok and env.last().result == {"acked": True}
    assert [r.id for r in env.store.unacked(env.orch)] == []
    env.ask(env.orch, "ack", id="zz")
    assert env.last().error_code == wire.NOT_FOUND


@pytest.mark.parametrize(
    "op", ["list", "read", "ack", "next", "children", "status", "grant", "send"]
)
def test_every_op_requires_a_ready_store_recipient(env: Env, op: str) -> None:
    env.store.remove_recipient("proj-kid")
    env.ask(env.kid, op, id="a", manifest=_manifest(), body=None, wait=True)
    resp = env.last()
    assert resp.error_code == wire.STORE
    assert "recipient is not ready" in resp.error_detail
    assert len(env.memory.waiters) == 0
    env.rig.dispatch.assert_not_called()

@pytest.mark.parametrize("state", ["done", "refused"])
def test_send_disconnected_during_handler_leaves_unread_status(env: Env, state: str) -> None:
    def dispatch(*args: object, **kwargs: object) -> Outcome:
        env.listener.dropped.add(7)
        return Outcome(state, "late outcome")

    env.rig.dispatch.side_effect = dispatch
    env.ask(env.kid, "send", conn=7, manifest=_manifest(), body=None, wait=True)
    rows = env.store.unacked(env.kid)
    assert len(rows) == 1 and rows[0].item["state"] == state
    assert env.store.lookup_request(env.kid, "m1").item == rows[0].item
