from __future__ import annotations

import queue
import socket
import sqlite3
import threading
from typing import TYPE_CHECKING, Any

import pytest

from jailbee.orchestration import broker_state, executor, outbox, socket_ops, wire
from jailbee.orchestration.actions import Outcome
from jailbee.orchestration.listener import DisconnectEvent, Listener, RequestEvent
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

    def keepalive(self, conn_id: int) -> None:
        self.respond(conn_id, wire.ok({"kind": "keepalive"}))

    def release(self, conn_id: int) -> None:
        self.released.append(conn_id)


class Env:
    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        rig.incus.list_containers.return_value = [_orch(), _child()]
        rig.dispatch.return_value = Outcome("done", "delivered")
        self.memory = broker_state.BrokerMemory()
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


def test_next_sends_initial_and_periodic_keepalives_without_answering_waiter(env: Env) -> None:
    ticks = [0.0]
    executor.handle_event(
        env.memory,
        RequestEvent(env.orch, 3, wire.Request("next", {"keepalive": True})),
        clock=lambda: ticks[0],
    )
    assert env.listener.responses == [(3, wire.ok({"kind": "keepalive"}))]
    assert len(env.memory.waiters) == 1
    ticks[0] = wire.KEEPALIVE_INTERVAL - 0.1
    executor.evaluate_waiters(env.memory, clock=lambda: ticks[0])
    assert len(env.listener.responses) == 1
    ticks[0] = wire.KEEPALIVE_INTERVAL
    executor.evaluate_waiters(env.memory, clock=lambda: ticks[0])
    assert env.listener.responses[-1] == (3, wire.ok({"kind": "keepalive"}))
    assert len(env.memory.waiters) == 1
    env.put("real-message")
    ticks[0] += 1
    executor.evaluate_waiters(env.memory, clock=lambda: ticks[0])
    ticks[0] += 2
    executor.evaluate_waiters(env.memory, clock=lambda: ticks[0])
    assert env.listener.responses[-1] == (3, wire.ok({"kind": "message", "id": "real-message"}))
    assert len(env.memory.waiters) == 0
    env.ask(env.orch, "list", conn=8, after=0)
    assert [item["id"] for item in env.last().result["items"]] == ["real-message"]


def test_old_next_client_has_no_keepalive(env: Env) -> None:
    env.ask(env.orch, "next", conn=4)
    assert env.listener.responses == []
    assert len(env.memory.waiters) == 1


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


def test_busy_names_container_waiter(env: Env) -> None:
    env.ask(
        env.orch,
        "next",
        conn=1,
        waiter={
            "pid": 123,
            "started_at": "2026-11-07T12:00:00+00:00",
            "command": "jailbee inbox wait",
        },
    )
    env.ask(env.orch, "next", conn=2)
    response = env.last()
    assert response.error_code == wire.BUSY
    assert "PID 123" in response.error_detail
    assert "2026-11-07T12:00:00+00:00" in response.error_detail
    assert "jailbee inbox wait" in response.error_detail
    assert "--replace" in response.error_detail


def test_replace_answers_old_waiter_and_preserves_new_slot(env: Env) -> None:
    env.ask(env.orch, "next", conn=1)
    env.ask(env.kid, "next", conn=3)
    env.ask(env.orch, "next", conn=2, replace=True)
    assert env.listener.responses == [(1, wire.ok({"kind": "replaced"}))]
    executor.handle_event(env.memory, DisconnectEvent(env.orch, 1))
    assert len(env.memory.waiters) == 2
    env.put("a")
    executor.evaluate_waiters(env.memory)
    assert env.listener.responses[-1] == (2, wire.ok({"kind": "message", "id": "a"}))
    assert len(env.memory.waiters) == 1  # the other container is untouched
    assert env.store.get(env.orch, "a").acked_at is None


def test_malformed_replace_does_not_displace_waiter(env: Env) -> None:
    env.ask(env.orch, "next", conn=1)
    env.ask(env.orch, "next", conn=2, replace="yes")
    assert env.listener.responses == [(2, wire.fail(wire.BAD_REQUEST, "replace must be a boolean"))]
    env.put("a")
    executor.evaluate_waiters(env.memory)
    assert env.listener.responses[-1][0] == 1


@pytest.mark.parametrize("option", ["quiet_after", "digest"])
def test_invalid_child_wait_does_not_replace_existing(env: Env, option: str) -> None:
    env.ask(env.kid, "next", conn=1)
    env.ask(env.kid, "next", conn=2, replace=True, **{option: 60})
    assert env.listener.responses[-1][0] == 2
    assert env.last().error_code == wire.NOT_ORCHESTRATED
    assert len(env.memory.waiters) == 1
    executor.handle_event(env.memory, DisconnectEvent(env.kid, 1))
    assert len(env.memory.waiters) == 0


@pytest.mark.parametrize("option", ["quiet_after", "digest"])
@pytest.mark.parametrize("view_state", ["missing", "stale"])
def test_unusable_watchdog_does_not_replace_existing(
    env: Env, option: str, view_state: str
) -> None:
    env.ask(env.orch, "next", conn=1)
    if view_state == "missing":
        env.memory.children.pop("proj-orch", None)
    else:
        env.memory.children["proj-orch"] = broker_state.ChildrenView(0.0, ())
    env.ask(env.orch, "next", conn=2, replace=True, **{option: 60})
    assert env.listener.responses[-1][0] == 2
    assert env.last().error_code == wire.BAD_REQUEST
    assert "fresh broker children list" in env.last().error_detail
    assert len(env.memory.waiters) == 1
    env.put("a")
    executor.evaluate_waiters(env.memory)
    assert env.listener.responses[-1][0] == 1


def test_replace_with_no_existing_waiter(env: Env) -> None:
    env.ask(env.orch, "next", conn=2, replace=True)
    assert env.listener.responses == []
    assert len(env.memory.waiters) == 1


def test_real_socket_replace_replies_to_both_connections(env: Env, tmp_path: Path) -> None:
    env.memory.requests = queue.Queue()
    listener = Listener(env.memory.requests)
    env.memory.listener = listener
    listener.start()
    listener.open_container(env.orch, tmp_path / "wait-sock")
    clients = [socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) for _ in range(2)]
    try:
        for client, replace in zip(clients, (False, True), strict=True):
            client.settimeout(2)
            client.connect(str(tmp_path / "wait-sock" / "broker.sock"))
            client.sendall(wire.encode_request(wire.Request("next", {"replace": replace})))
            event = env.memory.requests.get(timeout=2)
            assert isinstance(event, RequestEvent)
            executor.handle_event(env.memory, event)
        with clients[0].makefile("rb") as reader:
            response = wire.decode_response(wire.read_line(reader))
        assert response.ok and response.result == {"kind": "replaced"}
        env.put("a")
        executor.evaluate_waiters(env.memory)
        with clients[1].makefile("rb") as reader:
            response = wire.decode_response(wire.read_line(reader))
        assert response.ok and response.result == {"kind": "message", "id": "a"}
        assert env.store.get(env.orch, "a").acked_at is None
    finally:
        for client in clients:
            client.close()
        listener.stop()


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
        env.memory, until=5.0, stop=broker_state.StopFlag(), clock=lambda: next(ticks, 10.0)
    )
    assert env.listener.responses[0][1].error_code == wire.REFUSED
    assert env.listener.responses[0][1].error_detail == "broker error"
    assert len(env.listener.responses) == 2


def test_serve_returns_when_until_passes(env: Env) -> None:
    env.memory.requests = queue.Queue()
    executor.serve_requests(env.memory, until=0.0, stop=broker_state.StopFlag(), clock=lambda: 1.0)


def test_serve_returns_on_stop(env: Env) -> None:
    stop = broker_state.StopFlag()
    stop.requested = True
    executor.serve_requests(env.memory, until=1e12, stop=stop)


def test_bulk_ack_op(env: Env) -> None:
    env.put("child-message")
    env.store.insert_item(
        env.orch, "proj-orch", {"id": "ok", "type": "status", "state": "done"}, now=1
    )
    env.ask(env.orch, "ack", ok_statuses=True)
    assert env.last().ok and env.last().result == {"acked": 1}
    assert [r.id for r in env.store.unacked(env.orch)] == ["child-message"]


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


@pytest.mark.parametrize("boundary", ["insert", "spool", "spool-ack", "sqlite-lock"])
def test_real_send_disconnect_during_delivery_stays_unread(
    env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from jailbee.orchestration.protocol import inbox_file_name_for
    from jailbee.orchestration.state import pending_dir

    env.memory.requests = queue.Queue()
    listener = Listener(env.memory.requests)
    env.memory.listener = listener
    listener.start()
    listener.open_container(env.kid, tmp_path / "sock")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(tmp_path / "sock" / "broker.sock"))
    client.sendall(
        wire.encode_request(
            wire.Request(
                "send",
                {"manifest": _manifest(), "body": None, "wait": True},
            )
        )
    )
    event = env.memory.requests.get(timeout=2)
    assert isinstance(event, RequestEvent)

    def disconnect() -> None:
        client.close()
        dropped = env.memory.requests.get(timeout=2)
        assert isinstance(dropped, DisconnectEvent)
        assert not listener.is_connected(event.conn_id)

    ctx = env.memory.contexts[env.kid]
    if boundary.startswith("spool"):
        outbox.save_pending(
            pending_dir(ctx.root, ctx.sender.full) / inbox_file_name_for("older", ctx.now),
            {"id": "older", "type": "message", "kind": "note"},
            recipient_uuid=env.kid,
        )
        real_save = outbox.save_pending

        def save(*args: Any, **kwargs: Any) -> None:
            if (boundary == "spool") or kwargs.get("acked") is True:
                disconnect()
            real_save(*args, **kwargs)

        monkeypatch.setattr(outbox, "save_pending", save)
    elif boundary == "insert":
        real_insert = outbox.insert_into_store

        def insert(*args: Any, **kwargs: Any) -> bool:
            disconnect()
            return real_insert(*args, **kwargs)

        monkeypatch.setattr(outbox, "insert_into_store", insert)
    else:
        real_ack = BrokerStore.ack_if_connected
        waiting = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def blocked_ack(self: BrokerStore, *args: Any, **kwargs: Any) -> bool:
            # An actual writer blocks BEGIN IMMEDIATE until the socket has closed.
            con = sqlite3.connect(self.path, isolation_level=None)
            con.execute("BEGIN IMMEDIATE")

            def close_while_locked() -> None:
                try:
                    assert waiting.wait(2)
                    disconnect()
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    release.set()

            worker = threading.Thread(target=close_while_locked)
            worker.start()
            try:
                real_connect = sqlite3.connect

                def traced_connect(*a: Any, **kw: Any) -> sqlite3.Connection:
                    connection = real_connect(*a, **kw)
                    connection.set_trace_callback(
                        lambda sql: waiting.set() if sql == "BEGIN IMMEDIATE" else None
                    )
                    return connection

                monkeypatch.setattr(sqlite3, "connect", traced_connect)
                # The trace fires at BEGIN, while our writer still owns the lock.
                result: list[bool] = []
                ack_worker = threading.Thread(
                    target=lambda: result.append(real_ack(self, *args, **kwargs))
                )
                ack_worker.start()
                assert release.wait(2)
                con.execute("COMMIT")
                ack_worker.join(3)
                assert not ack_worker.is_alive()
                assert not errors
                return result[0]
            finally:
                worker.join(3)
                con.close()

        monkeypatch.setattr(BrokerStore, "ack_if_connected", blocked_ack)
    try:
        executor.handle_event(env.memory, event)
        rows = env.store.history(env.kid)
        if boundary.startswith("spool"):
            pending = [
                outbox.load_pending(p) for p in pending_dir(ctx.root, ctx.sender.full).iterdir()
            ]
            status = [p for p in pending if p.item.get("type") == "status"]
            assert len(status) == 1 and not status[0].acked
            outbox.flush_pending(
                ctx.incus, ctx.root, ctx.sender.full, uid=None, recipient_uuid=env.kid
            )
            rows = env.store.history(env.kid)
        status_rows = [r for r in rows if r.item.get("type") == "status"]
        assert len(status_rows) == 1 and status_rows[0].acked_at is None
        assert env.store.unacked(env.kid)
    finally:
        client.close()
        listener.stop()
