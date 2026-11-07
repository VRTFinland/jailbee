from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

from jailbee.orchestration import broker, executor, socket_ops, wire
from jailbee.orchestration.outbox import deliver
from jailbee.orchestration.actions import Outcome
from jailbee.orchestration.listener import Listener, RequestEvent
from jailbee.orchestration.query import ContainerSnapshot, Queries
from jailbee.orchestration.store import BrokerStore, StoreError
from tests.test_orchestration_broker import Rig, _child, _orch
from tests.test_orchestration_listener import _connect_long, recv_line

if TYPE_CHECKING:
    from pathlib import Path


class Env:
    def __init__(self, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rig = rig
        self.memory = broker.BrokerMemory()
        rig.incus.list_containers.return_value = [_orch(), _child()]
        rig.tick(self.memory)
        self.store = BrokerStore(rig.root / "broker.db")
        self.queries = Queries(self.store)
        self.memory.store = self.store
        self.memory.queries = self.queries
        monkeypatch.setattr(broker, "_ensure_socket", lambda *a: None)
        for full in ("proj-orch", "proj-kid", "proj-former"):
            self.store.set_recipient(full, full, now=1.0)
        rig.tick(self.memory)
        self.listener = Listener(self.memory.requests, queries=self.queries)
        self.memory.listener = self.listener
        self.listener.start()
        for full in ("proj-orch", "proj-kid", "proj-former", "unknown"):
            self.listener.open_container(full, rig.root / full)

    def call(self, op: str, *, uuid: str = "proj-orch", **args: object) -> wire.Response:
        with _connect_long(self.rig.root / uuid) as client:
            client.sendall(wire.encode_request(wire.Request(op, args)))
            return wire.decode_response(recv_line(client))

    def put(self, mid: str, *, at: float = 100.0) -> None:
        self.store.insert_item(
            "proj-orch",
            "proj-orch",
            {"id": mid, "type": "message", "from": "kid", "body": mid},
            now=at,
        )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    instance = Env(Rig(tmp_path, monkeypatch), monkeypatch)
    yield instance
    instance.listener.stop()


def test_all_queries_answer_last_round_while_executor_handler_is_blocked(env: Env) -> None:
    entered, unblock = threading.Event(), threading.Event()
    env.put("read-me")
    env.put("ack-me")
    # Publish history along with the round's children/grant, not on query threads.
    env.rig.tick(env.memory)
    expected_children = env.call("children").result
    expected_grant = env.call("grant").result  # runners uses this same op

    def blocked(*args: Any) -> Outcome:
        entered.set()
        assert unblock.wait(5)
        return Outcome("done", "delivered")

    env.rig.dispatch.side_effect = blocked
    sender = _connect_long(env.rig.root / "proj-kid")
    sender.sendall(
        wire.encode_request(
            wire.Request(
                "send",
                {
                    "manifest": {"version": 1, "id": "blocking", "type": "message", "body": "hi"},
                    "wait": True,
                },
            )
        )
    )
    event = env.memory.requests.get(timeout=2)
    assert isinstance(event, RequestEvent)
    worker = threading.Thread(target=executor.handle_event, args=(env.memory, event))
    worker.start()
    try:
        assert entered.wait(2)
        env.rig.incus.reset_mock()
        env.memory.children["proj-orch"].children[0]["running"] = False
        env.memory.contexts["proj-orch"].sender.config.clear()
        assert env.call("children").result == expected_children
        assert env.call("status", child="kid").result == expected_children
        assert env.call("grant").result == expected_grant
        assert env.call("list", after=0).result["items"][0]["id"] == "read-me"
        assert env.call("read", id="read-me").result["body"] == "read-me"
        assert env.call("ack", id="ack-me").result == {"acked": True}
        assert env.store.unacked("proj-orch") == []
        env.rig.incus.assert_not_called()
        assert env.rig.incus.mock_calls == []
        assert worker.is_alive()
        assert env.memory.requests.empty()
    finally:
        unblock.set()
        worker.join(timeout=3)
        sender.close()
    assert not worker.is_alive()


@pytest.mark.parametrize("kind", ["stop", "destroy", "restart", "spawn"])
def test_lifecycle_queries_are_current_before_success_reply(
    env: Env, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    before_kids = [] if kind == "spawn" else [_child(running=kind != "restart")]
    after_kids = [] if kind == "destroy" else [_child(running=kind != "stop")]
    env.rig.incus.list_containers.return_value = [_orch(), *before_kids]
    env.rig.tick(env.memory)
    previous = env.call("children").result

    def act(*args: Any) -> Outcome:
        env.rig.incus.list_containers.return_value = [_orch(), *after_kids]
        return Outcome("started" if kind in ("spawn", "restart") else "done", "handled")

    env.rig.dispatch.side_effect = act
    seen = []

    def reply(*args: Any, **kwargs: Any) -> str:
        children = env.queries.answer("proj-orch", wire.Request("children"))
        assert children.ok
        assert [(c["name"], c["running"]) for c in children.result["children"]] == (
            [] if kind == "destroy" else [("kid", kind != "stop")]
        )
        status = env.queries.answer("proj-orch", wire.Request("status", {"child": "kid"}))
        if kind == "destroy":
            assert status.error_code == wire.NOT_FOUND
        else:
            assert status.ok and status.result == children.result
        seen.append(children.result)
        return deliver(*args, **kwargs)

    monkeypatch.setattr(socket_ops, "deliver", reply)
    manifest: dict[str, object] = {"version": 1, "id": "action", "type": kind, "child": "kid"}
    if kind == "restart":
        manifest["prompt"] = "continue"
    elif kind == "spawn":
        manifest = {
            "version": 1, "id": "action", "type": "spawn", "kind": "new",
            "branch": "kid", "prompt": "start",
            "rights": {"version": 1, "mounts": [], "loose": False, "egress": []},
        }
    result = socket_ops.handle_send(
        env.memory.contexts["proj-orch"], env.memory, env.store,
        manifest=manifest, body=None, wait=True, is_connected=lambda: True,
    )
    assert result["state"] in ("done", "started")
    assert len(seen) == 1
    assert previous != seen[0]


@pytest.mark.parametrize("kind", ["note", "question", "blocked", "done"])
def test_child_message_queries_match_live_next_history_before_another_tick(
    env: Env, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from jailbee.orchestration import actions, messages

    inventory = [_orch(), _child()]
    for row in inventory:
        row["config"]["volatile.uuid"] = row["name"]
    env.rig.incus.list_containers.return_value = inventory
    env.rig.tick(env.memory)
    env.rig.dispatch.side_effect = actions.dispatch
    if kind == "done":
        # The import/cleanup work is outside this query regression's scope.
        monkeypatch.setattr(messages, "handle_done", lambda ctx, m: messages._relay(
            ctx, ctx.nodes["proj-orch"], m
        ))
    before = env.call("status", child="kid").result
    assert before["last_from_child"] == {}
    result = socket_ops.handle_send(
        env.memory.contexts["proj-kid"], env.memory, env.store,
        manifest={"version": 1, "id": "child-msg", "type": "message", "kind": kind,
                  "body": "latest child update"},
        body=None, wait=True, is_connected=lambda: True,
    )
    assert result["state"] == "done"
    for row in env.store.unacked("proj-orch"):
        env.store.ack("proj-orch", row.id, now=1.0)
    env.store.set_digest_at("proj-orch", env.memory.children["proj-orch"].observed_at - 2)
    waiter = socket_ops.Waiter("proj-orch", 1, frozenset(), 0, 1, None)
    digest = socket_ops._evaluate_one(
        waiter, env.memory, env.store,
        now=env.memory.children["proj-orch"].observed_at, mono=1.0
    )
    assert isinstance(digest, dict) and digest["kind"] == "digest"
    assert set(digest["last_from_child"]) == {"kid"}
    for op in ("children", "status"):
        response = env.queries.answer("proj-orch", wire.Request(op, {"child": "kid"}))
        assert response.ok
        assert response.result["last_from_child"] == digest["last_from_child"]
    assert before["last_from_child"] == {}


def test_snapshot_changes_only_at_completed_round(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, unblock = threading.Event(), threading.Event()
    before = env.call("children").result
    original = broker._settle_all_watches

    def blocked(*args: Any) -> None:
        env.memory.children["proj-orch"].children[0]["name"] = "new-kid"
        entered.set()
        assert unblock.wait(5)
        original(*args)

    monkeypatch.setattr(broker, "_settle_all_watches", blocked)
    worker = threading.Thread(target=env.rig.tick, args=(env.memory,))
    worker.start()
    try:
        assert entered.wait(2)
        assert env.call("children").result == before
    finally:
        unblock.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert env.call("children").result["children"][0]["name"] == "new-kid"


def test_failed_round_keeps_last_snapshot(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    before = env.call("children").result

    def broken(*args: Any) -> None:
        env.memory.children.clear()
        raise RuntimeError("round failed")

    monkeypatch.setattr(broker, "_settle_all_watches", broken)
    with pytest.raises(RuntimeError, match="round failed"):
        env.rig.tick(env.memory)
    assert env.call("children").result == before


@pytest.mark.parametrize("uuid", ["proj-kid", "proj-former"])
@pytest.mark.parametrize("op", ["children", "status", "grant"])
def test_role_refusals(env: Env, uuid: str, op: str) -> None:
    assert env.call(op, uuid=uuid).error_code == wire.NOT_ORCHESTRATED


@pytest.mark.parametrize("op", ["children", "status", "grant", "list", "read", "ack"])
def test_not_ready_recipient(env: Env, op: str) -> None:
    response = env.call(op, uuid="unknown", after=0, id="x")
    assert response.error_code == wire.STORE
    assert "recipient is not ready" in response.error_detail


def test_status_validation_and_missing_child(env: Env) -> None:
    assert env.call("status", child=1).error_code == wire.BAD_REQUEST
    assert env.call("status", child="gone").error_code == wire.NOT_FOUND
    assert env.call("status", uuid="proj-kid", child=1).error_code == wire.BAD_REQUEST


def test_snapshot_queries_never_touch_store_or_live_contexts(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("live state was read")

    for name in ("has_recipient_uuid", "history_meta", "recipients"):
        monkeypatch.setattr(env.store, name, broken)
    env.memory.contexts.clear()
    env.memory.children.clear()
    assert env.call("children").ok
    assert env.call("status").ok
    assert env.call("grant").ok


def test_sqlite_queries_use_worker_connections_and_immediate_writes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.put("read-me")
    env.put("ack-me")
    connections: list[int] = []
    writes: list[tuple[int, str]] = []
    connect = sqlite3.connect

    def traced(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connections.append(threading.get_ident())
        con = connect(*args, **kwargs)
        con.set_trace_callback(lambda sql: writes.append((threading.get_ident(), sql)))
        return con

    monkeypatch.setattr(sqlite3, "connect", traced)
    assert env.call("list", after=0).ok
    assert env.call("read", id="read-me").ok
    assert env.call("ack", id="ack-me").ok
    ids = {t.ident for t in env.listener._query_threads}
    assert connections and set(connections) <= ids
    assert len([sql for _, sql in writes if sql == "BEGIN IMMEDIATE"]) == 2


def test_query_errors_are_contained(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise StoreError("store unavailable")

    monkeypatch.setattr(socket_ops, "handle_list", fail)
    assert env.call("list", after=0).error_code == wire.STORE
    monkeypatch.setattr(socket_ops, "handle_list", lambda *a, **k: 1 / 0)
    assert env.call("list", after=0).error_code == wire.REFUSED
    assert env.call("grant").ok


def test_blocked_sqlite_worker_does_not_block_snapshot_queries(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, unblock = threading.Event(), threading.Event()
    original = env.store.has_recipient_uuid

    def blocked(uuid: str) -> bool:
        entered.set()
        assert unblock.wait(5)
        return original(uuid)

    monkeypatch.setattr(env.store, "has_recipient_uuid", blocked)
    with _connect_long(env.rig.root / "proj-orch") as client:
        client.sendall(wire.encode_request(wire.Request("list", {"after": 0})))
        try:
            assert entered.wait(2)
            assert env.call("children").ok
            assert env.call("status").ok
            assert env.call("grant").ok
        finally:
            unblock.set()
        assert wire.decode_response(recv_line(client)).ok


@pytest.mark.parametrize("targeted", [False, True])
def test_concurrent_snapshot_swaps_keep_each_response_in_one_generation(
    env: Env, targeted: bool
) -> None:
    done = threading.Event()

    def publish() -> None:
        n = 0
        while not done.is_set():
            payload = json.dumps({"children": [{"name": str(n)}], "observed_at": n})
            view = ContainerSnapshot("orchestrator", True, payload, "{}")
            if targeted:
                env.queries.publish_container("proj-orch", view)
            else:
                env.queries.publish({"proj-orch": view})
            n += 1

    worker = threading.Thread(target=publish)
    worker.start()
    try:
        for _ in range(100):
            result = env.call("children").result
            assert result["children"][0]["name"] == str(result["observed_at"])
    finally:
        done.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_targeted_publication_preserves_other_container_views(env: Env) -> None:
    env.queries.publish_container(
        "proj-orch", ContainerSnapshot("orchestrator", True, '{"children": []}', "{}")
    )
    assert env.queries.answer("proj-kid", wire.Request("children")).error_code == (
        wire.NOT_ORCHESTRATED
    )
    assert env.queries.answer("proj-former", wire.Request("children")).error_code == (
        wire.NOT_ORCHESTRATED
    )


def test_published_records_and_returned_results_have_no_mutable_aliases(env: Env) -> None:
    value = ContainerSnapshot(
        "orchestrator", True, json.dumps({"children": [{"name": "one"}]}), "{}"
    )
    source = {"proj-orch": value}
    env.queries.publish(source)
    source.clear()
    result = env.queries.answer("proj-orch", wire.Request("children")).result
    result["children"][0]["name"] = "mutated"
    assert env.queries.answer("proj-orch", wire.Request("children")).result["children"] == [
        {"name": "one"}
    ]
