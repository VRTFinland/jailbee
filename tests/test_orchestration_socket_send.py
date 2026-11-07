from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest

from jailbee.orchestration import broker, finishing, outbox, socket_ops, wire
from jailbee.orchestration.actions import Outcome
from jailbee.orchestration.store import BrokerStore
from tests.test_orchestration_broker import Rig, _child, _orch

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.orchestration.actions import ActionContext


def _message(mid: str = "m1", kind: str = "note") -> dict[str, object]:
    return {"version": 1, "id": mid, "type": "message", "kind": kind, "body": "hello"}


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


class Env:
    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        rig.incus.list_containers.return_value = [_orch(), _child()]
        rig.dispatch.return_value = Outcome("done", "delivered")
        self.memory = broker.BrokerMemory()
        rig.tick(self.memory)
        rig.deliver.reset_mock()
        rig.dispatch.reset_mock()
        self.store = BrokerStore(rig.root / "broker.db")

    @property
    def ctx(self) -> ActionContext:
        return next(c for c in self.memory.contexts.values() if c.sender.full == "proj-kid")

    def send(self, manifest: object, body: object = None, wait: bool = False) -> dict[str, object]:
        return socket_ops.handle_send(
            self.ctx, self.memory, self.store, manifest=manifest, body=body, wait=wait,
            is_connected=lambda: True,
        )


@pytest.fixture
def env(rig: Rig) -> Env:
    return Env(rig)


def test_tick_fills_contexts_and_children(rig: Rig) -> None:
    rig.incus.list_containers.return_value = [_orch(), _child()]
    memory = broker.BrokerMemory()
    rig.tick(memory)
    assert {c.sender.full for c in memory.contexts.values()} == {"proj-orch", "proj-kid"}
    assert set(memory.contexts) == {c.sender.key for c in memory.contexts.values()}
    view = memory.children["proj-orch"]
    assert [c["name"] for c in view.children] == ["kid"]
    assert view.observed_at > 0
    rig.incus.list_containers.return_value = [_orch()]
    rig.tick(memory)
    assert {c.sender.full for c in memory.contexts.values()} == {"proj-orch"}
    rig.incus.list_containers.return_value = []
    rig.tick(memory)
    assert memory.contexts == {} and memory.children == {}


def test_valid_message_dispatches_once_and_returns_done(env: Env) -> None:
    item = env.send(_message())
    env.rig.dispatch.assert_called_once()
    assert item["type"] == "status" and item["ref"] == "m1" and item["state"] == "done"
    env.rig.deliver.assert_not_called()


def test_resend_returns_recorded_status_without_dispatching(env: Env) -> None:
    first = env.send(_message())
    again = env.send(_message())
    assert again == first
    env.rig.dispatch.assert_called_once()
    assert len(env.store.history(env.ctx.sender.key)) == 1


def test_invalid_manifest_is_refused_synchronously(env: Env) -> None:
    item = env.send({"version": 1, "id": "bad1", "type": "message"})
    assert item["state"] == "refused" and item["ref"] == "bad1"
    env.rig.dispatch.assert_not_called()
    assert env.send({"version": 1, "id": "bad1", "type": "message"}) == item


@pytest.mark.parametrize("wait", [True, False])
def test_wait_controls_acked(env: Env, wait: bool) -> None:
    item = env.send(_message(), wait=wait)
    unacked = [i.id for i in env.store.unacked(env.ctx.sender.key)]
    assert (item["id"] in unacked) is (not wait)
    assert env.store.get(env.ctx.sender.key, str(item["id"])) is not None


def test_recorded_item_is_the_returned_item(env: Env) -> None:
    item = env.send(_message())
    recorded = env.store.lookup_request(env.ctx.sender.key, "m1")
    assert recorded is not None and recorded.item == item


@pytest.mark.parametrize(
    ("manifest", "body"),
    [
        ("nope", None),
        ({"version": 1}, None),
        ({"id": 5}, None),
        ({"id": "a/b"}, None),
        ({"id": ""}, None),
        ({"id": "ok"}, 5),
    ],
)
def test_bad_request_raises(env: Env, manifest: object, body: object) -> None:
    with pytest.raises(wire.WireError) as exc:
        env.send(manifest, body)
    assert exc.value.code == wire.BAD_REQUEST


def test_body_is_supplied_to_the_manifest(env: Env) -> None:
    manifest = {"version": 1, "id": "m2", "type": "message", "body_file": "m2.md"}
    item = env.send(manifest, body="from file")
    assert item["state"] == "done"
    assert env.rig.dispatch.call_args.args[1].body == "from file"


def test_done_message_writes_reply_path_then_clears_it(env: Env) -> None:
    item = env.send(_message("d1", kind="done"))
    assert item["state"] == "done"
    # reply delivered, so the durable gate is gone again, like the file path.
    assert not finishing.reply_path(env.ctx, env.ctx.sender).exists()


def test_done_reply_path_is_written_before_the_reply(env: Env, monkeypatch) -> None:
    seen: list[dict[str, object]] = []
    real = socket_ops.deliver

    def deliver(*args, **kwargs):
        seen.append(json.loads(finishing.reply_path(env.ctx, env.ctx.sender).read_text()))
        return real(*args, **kwargs)

    monkeypatch.setattr(socket_ops, "deliver", deliver)
    env.send(_message("d2", kind="done"))
    assert seen == [{"ref": "d2", "state": "done", "detail": "delivered"}]


def test_accepted_publish_without_a_row_is_a_duplicate(env: Env, monkeypatch) -> None:
    monkeypatch.setattr("jailbee.orchestration.publishing.was_accepted", lambda ctx, ref: True)
    with pytest.raises(wire.WireError) as exc:
        env.send(_message("p1"))
    assert exc.value.code == wire.DUPLICATE
    assert "request p1 was already handled" in exc.value.detail
    env.rig.dispatch.assert_not_called()


def test_recorded_row_without_an_item_is_a_duplicate(env: Env) -> None:
    env.store.record_request(
        env.ctx.sender.key, "p2", digest=None, state="ignored", detail="", item=None, now=1.0
    )
    with pytest.raises(wire.WireError) as exc:
        env.send(_message("p2"))
    assert exc.value.code == wire.DUPLICATE
    env.rig.dispatch.assert_not_called()


@pytest.mark.parametrize("wait", [True, False])
def test_recorded_item_is_inserted_idempotently(env: Env, wait: bool) -> None:
    # A crash between the journal and the insert left the row without the item.
    item = {"version": 1, "id": "s-1", "type": "status", "ref": "r1", "state": "done"}
    key = env.ctx.sender.key
    env.store.record_request(key, "r1", digest=None, state="done", detail="", item=item, now=1.0)
    assert env.send(_message("r1"), wait=wait) == item
    row = env.store.get(key, "s-1")
    assert row is not None and (row.acked_at is not None) is wait
    assert env.send(_message("r1"), wait=wait) == item
    assert len(env.store.history(key)) == 1
    env.rig.dispatch.assert_not_called()


def test_a_failing_reply_path_cleanup_does_not_fail_the_send(env: Env, monkeypatch) -> None:
    def boom(ctx: object, ref: str) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(broker, "clear_reply_path", boom)
    item = env.send(_message("d3", kind="done"))
    assert item["state"] == "done"
    assert env.store.get(env.ctx.sender.key, str(item["id"])) is not None


def test_handlers_see_the_time_of_handling(env: Env) -> None:
    env.send(_message("t1"))
    seen = env.rig.dispatch.call_args.args[0].now
    assert seen != env.ctx.now
    assert abs(seen.timestamp() - time.time()) < 60


def test_a_waiting_resend_marks_the_earlier_unread_status_read(env: Env) -> None:
    """`send` without --wait, then the same id with --wait: printed, so not also unread."""
    item = env.send(_message())
    key = env.ctx.sender.key
    assert [i.id for i in env.store.unacked(key)] == [item["id"]]
    assert env.send(_message(), wait=True) == item
    assert env.store.unacked(key) == []

@pytest.mark.parametrize("wait", [False, True])
def test_synchronous_refusal_is_read_history(env: Env, wait: bool) -> None:
    item = env.send({"version": 1, "id": "bad1", "type": "message"}, wait=wait)
    row = env.store.get(env.ctx.sender.key, str(item["id"]))
    assert row is not None and row.acked_at is not None
    assert env.store.unacked(env.ctx.sender.key) == []


@pytest.mark.parametrize("wait", [False, True])
def test_status_stays_behind_older_pending_delivery(env: Env, wait: bool) -> None:
    from jailbee.orchestration.protocol import inbox_file_name_for
    from jailbee.orchestration.state import pending_dir

    ctx = env.ctx
    directory = pending_dir(ctx.root, ctx.sender.full)
    older = {"version": 1, "id": "older", "type": "message", "kind": "note", "body": "first"}
    outbox.save_pending(
        directory / inbox_file_name_for("older", ctx.now),
        older, recipient_uuid=ctx.sender.key,
    )
    item = env.send(_message(), wait=wait)
    assert env.store.history(ctx.sender.key) == []
    assert len(list(directory.iterdir())) == 2
    assert env.send(_message(), wait=True) == item
    assert len(list(directory.iterdir())) == 2
    assert outbox.flush_pending(
        ctx.incus, ctx.root, ctx.sender.full, uid=None, recipient_uuid=ctx.sender.key,
    ) == 2
    history = env.store.history(ctx.sender.key)
    assert [r.id for r in history] == ["older", item["id"]]
    assert history[0].acked_at is None and history[1].acked_at is not None
    env.rig.dispatch.assert_called_once()


def test_waiting_retry_acks_stored_status_despite_later_pending_item(env: Env) -> None:
    from jailbee.orchestration.protocol import inbox_file_name_for
    from jailbee.orchestration.state import pending_dir

    item = env.send(_message())
    ctx = env.ctx
    directory = pending_dir(ctx.root, ctx.sender.full)
    outbox.save_pending(
        directory / inbox_file_name_for("later", ctx.now),
        {"id": "later", "type": "message", "kind": "note"}, recipient_uuid=ctx.sender.key,
    )
    assert env.send(_message(), wait=True) == item
    assert env.store.unacked(ctx.sender.key) == []
    assert [outbox.load_pending(p).item["id"] for p in directory.iterdir()] == ["later"]
    env.rig.dispatch.assert_called_once()
