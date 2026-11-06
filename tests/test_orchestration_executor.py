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

    def respond(self, conn_id: int, response: wire.Response) -> None:
        self.responses.append((conn_id, response))


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
    env.ask(env.orch, "list", conn=7)
    conn, resp = env.listener.responses[-1]
    assert conn == 7 and resp.ok
    assert [i["id"] for i in resp.result] == ["a"]


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
