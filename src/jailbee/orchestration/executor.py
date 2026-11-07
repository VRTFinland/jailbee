"""The broker's request executor: serves queued socket requests between rounds.

Runs on the broker loop thread, the only thread that calls the op handlers, so
handlers never overlap a round. Long-polls (`next`) are parked in
`memory.waiters` and re-evaluated after every event and round, and at least
once per second.
"""

from __future__ import annotations

import logging
import queue
import time
from typing import TYPE_CHECKING

from jailbee.orchestration import socket_ops, wire
from jailbee.orchestration.listener import DisconnectEvent, RequestEvent
from jailbee.orchestration.store import StoreError

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.orchestration.broker_state import BrokerMemory, StopFlag
    from jailbee.orchestration.listener import Event
    from jailbee.orchestration.store import BrokerStore

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0


def serve_requests(
    memory: BrokerMemory,
    *,
    until: float,
    stop: StopFlag,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Serve queued requests until `clock()` reaches `until` or a stop is requested."""
    while not stop.requested:
        remaining = until - clock()
        if remaining <= 0:
            return
        try:
            event = memory.requests.get(timeout=min(POLL_SECONDS, remaining))
        except queue.Empty:
            event = None
        if event is not None:
            handle_event(memory, event, clock=clock)
        evaluate_waiters(memory, clock=clock)


def handle_event(
    memory: BrokerMemory, event: Event, *, clock: Callable[[], float] = time.monotonic
) -> None:
    store = memory.store
    assert store is not None and memory.listener is not None
    if isinstance(event, DisconnectEvent):
        memory.waiters.drop(event.uuid, event.conn_id)
        memory.listener.release(event.conn_id)
        return
    if not memory.listener.is_connected(event.conn_id):
        # Sent and gone (open, send, close): nobody waits for the answer, and
        # running it would let such loops cost others; free its slot instead.
        memory.listener.release(event.conn_id)
        return
    try:
        result = _dispatch(memory, store, event, clock)
    except wire.WireError as e:
        response = wire.fail(e.code, e.detail)
    except StoreError as e:
        response = wire.fail(wire.STORE, str(e))
    except Exception:
        log.exception("broker: socket request %r failed", event.request.op)
        response = wire.fail(wire.REFUSED, "broker error")
    else:
        if result is _PARKED:
            return
        response = wire.ok(result)
    memory.listener.respond(event.conn_id, response)


_PARKED = object()


def _dispatch(
    memory: BrokerMemory, store: BrokerStore, event: RequestEvent, clock: Callable[[], float]
) -> object:
    uuid = event.uuid
    op = event.request.op
    args = event.request.args
    if not store.has_recipient_uuid(uuid):
        raise wire.WireError(wire.STORE, "inbox store recipient is not ready; retry shortly")
    if op == "send":
        ctx = memory.contexts.get(uuid)
        if ctx is None:
            raise wire.WireError(wire.NOT_ORCHESTRATED, "this container has no orchestration role")
        return socket_ops.handle_send(
            ctx,
            memory,
            store,
            manifest=args.get("manifest"),
            body=args.get("body"),
            wait=args.get("wait") is True,
            is_connected=lambda: (
                memory.listener is not None and memory.listener.is_connected(event.conn_id)
            ),
        )
    if op == "list":
        return socket_ops.handle_list(store, uuid, after=args.get("after"))
    if op == "read":
        return socket_ops.handle_read(
            store, uuid, id=args.get("id"), keep=args.get("keep", False), now=time.time()
        )
    if op == "ack":
        return socket_ops.handle_ack(
            store,
            uuid,
            id=args.get("id"),
            now=time.time(),
            ok_statuses=args.get("ok_statuses", False),
        )
    if op == "children":
        return socket_ops.handle_children(memory, store, uuid)
    if op == "status":
        return socket_ops.handle_status(memory, store, uuid, child=args.get("child"))
    if op == "grant":
        return socket_ops.handle_grant(memory, uuid)
    if op == "next":
        parsed = socket_ops.handle_next_args(args)
        socket_ops.validate_next_context(memory, uuid, parsed, now=time.time())
        mono = clock()
        previous = memory.waiters.add(
            socket_ops.Waiter(
                uuid=uuid,
                conn_id=event.conn_id,
                ref=parsed.ref,
                wake=parsed.wake,
                quiet=parsed.quiet,
                digest=parsed.digest,
                deadline=mono + parsed.timeout if parsed.timeout is not None else None,
                info=parsed.info,
            ),
            replace=parsed.replace,
        )
        if previous is not None:
            assert memory.listener is not None
            memory.listener.respond(previous.conn_id, wire.ok({"kind": "replaced"}))
        return _PARKED
    raise wire.WireError(wire.BAD_REQUEST, f"unknown op '{op}'")


def evaluate_waiters(memory: BrokerMemory, *, clock: Callable[[], float] = time.monotonic) -> None:
    """Answer every parked `next` that is ready."""
    store = memory.store
    listener = memory.listener
    if store is None or listener is None or not len(memory.waiters):
        return
    try:
        answered = memory.waiters.evaluate(memory, store, now=time.time(), mono=clock())
    except Exception:
        log.exception("broker: evaluating waiters failed")
        return
    for waiter, outcome in answered:
        if isinstance(outcome, wire.WireError):
            listener.respond(waiter.conn_id, wire.fail(outcome.code, outcome.detail))
        else:
            listener.respond(waiter.conn_id, wire.ok(outcome))
