"""Off-loop inbox queries and immutable views of completed rounds and actions."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from jailbee.orchestration import wire
from jailbee.orchestration.outbox import grant_json_text
from jailbee.orchestration.store import StoreError

if TYPE_CHECKING:
    from jailbee.orchestration import broker_state
    from jailbee.orchestration.actions import ActionContext
    from jailbee.orchestration.store import BrokerStore

log = logging.getLogger(__name__)

SNAPSHOT_OPS = frozenset({"children", "status", "grant"})
STORE_OPS = frozenset({"list", "read", "ack"})
# Outbox ops are SQLite queries too: query workers, never the round loop.
QUERY_OPS = SNAPSHOT_OPS | STORE_OPS | wire.OUTBOX_OPS


@dataclass(frozen=True)
class ContainerSnapshot:
    """JSON strings sever every nested reference to mutable broker/context data."""

    role: str
    ready: bool
    children_json: str
    grant_json: str | None


class Queries:
    """Only the broker publishes; readers capture one whole immutable generation.

    The store holds a path, not a SQLite connection: its methods open and close
    a connection per call, retaining BEGIN IMMEDIATE for acknowledgement writes.
    """

    def __init__(self, store: BrokerStore) -> None:
        self._store = store
        self._snapshot: Mapping[str, ContainerSnapshot] = MappingProxyType({})

    def publish(self, snapshot: Mapping[str, ContainerSnapshot]) -> None:
        self._snapshot = MappingProxyType(dict(snapshot))

    def snapshot(self) -> Mapping[str, ContainerSnapshot]:
        """Return the immutable generation for executor-side recovery."""
        return self._snapshot

    def publish_container(self, uuid: str, view: ContainerSnapshot) -> None:
        """Replace one executor-built view without rebuilding unrelated containers."""
        self.publish({**self._snapshot, uuid: view})

    def answer(self, uuid: str, request: wire.Request) -> wire.Response:
        try:
            result = self._dispatch(uuid, request)
        except wire.WireError as exc:
            return wire.fail(exc.code, exc.detail)
        except StoreError as exc:
            return wire.fail(wire.STORE, str(exc))
        except Exception:
            log.exception("broker: query %r failed", request.op)
            return wire.fail(wire.REFUSED, "broker error")
        return wire.ok(result)

    def _dispatch(self, uuid: str, request: wire.Request) -> object:
        op, args = request.op, request.args
        if op in wire.OUTBOX_OPS:
            return self._outbox(uuid, op, args)
        if op in STORE_OPS:
            from jailbee.orchestration import socket_ops

            if not self._store.has_recipient_uuid(uuid):
                raise wire.WireError(
                    wire.STORE, "inbox store recipient is not ready; retry shortly"
                )
            known = self._snapshot.get(uuid)
            if known is not None and known.role == "plain":
                raise wire.WireError(wire.NOT_ORCHESTRATED, "this container is not an orchestrator")
            if op == "list":
                return socket_ops.handle_list(self._store, uuid, after=args.get("after"))
            if op == "read":
                return socket_ops.handle_read(
                    self._store,
                    uuid,
                    id=args.get("id"),
                    keep=args.get("keep", False),
                    now=time.time(),
                )
            return socket_ops.handle_ack(
                self._store,
                uuid,
                id=args.get("id"),
                now=time.time(),
                ok_statuses=args.get("ok_statuses", False),
            )

        # Never consult BrokerMemory, ActionContext, Incus or SQLite here.
        snapshot = self._snapshot
        view = snapshot.get(uuid)
        if view is None or not view.ready:
            raise wire.WireError(wire.STORE, "inbox store recipient is not ready; retry shortly")
        if op == "grant":
            if view.grant_json is None:
                raise wire.WireError(
                    wire.NOT_ORCHESTRATED, "this container holds no orchestration grant"
                )
            return json.loads(view.grant_json)
        if op not in ("children", "status"):
            raise wire.WireError(wire.BAD_REQUEST, f"unknown op '{op}'")
        child = args.get("child") if op == "status" else None
        if child is not None and not isinstance(child, str):
            raise wire.WireError(wire.BAD_REQUEST, "child must be a string or null")
        if view.role != "orchestrator":
            raise wire.WireError(wire.NOT_ORCHESTRATED, "this container is not an orchestrator")
        result: dict[str, object] = json.loads(view.children_json)
        if child is not None:
            children = result["children"]
            assert isinstance(children, list)
            picked = [c for c in children if c.get("name") == child]
            if not picked:
                raise wire.WireError(wire.NOT_FOUND, f"unknown child '{child}'")
            result["children"] = picked
        return result

    def _outbox(self, uuid: str, op: str, args: dict[str, object]) -> object:
        """Outbox ops for any non-child container; the role comes from the
        completed-round view, so the broker, not the container CLI, decides."""
        from jailbee.container_outbox.role import CHILD_REFUSAL
        from jailbee.outbox import broker_ops
        from jailbee.outbox.store_db import OutboxStore

        view = self._snapshot.get(uuid)
        if view is None or not view.ready:
            raise wire.WireError(
                wire.STORE, "the broker does not know this container yet; retry shortly"
            )
        if view.role == "child":
            raise wire.WireError(wire.REFUSED, CHILD_REFUSAL)
        return broker_ops.handle(OutboxStore(self._store), uuid, op, args, now=time.time())


def _context_snapshot(memory: broker_state.BrokerMemory, ctx: ActionContext) -> ContainerSnapshot:
    """Serialize on the executor so query readers never reach mutable contexts."""
    from jailbee.orchestration import socket_ops

    assert memory.store is not None
    previous = memory.queries.snapshot().get(ctx.sender.key) if memory.queries else None
    grant = ctx.grant()
    try:
        ready = memory.store.has_recipient_uuid(ctx.sender.key)
    except StoreError:
        log.exception("broker: recipient readiness for %s failed", ctx.sender.key)
        ready = previous.ready if previous is not None else False
    try:
        children = socket_ops.children_result(memory, memory.store, ctx.sender.key, ctx)
    except StoreError:
        log.exception("broker: child history for %s failed", ctx.sender.key)
        view = memory.children.get(ctx.sender.full)
        old = json.loads(previous.children_json) if previous is not None else {}
        children = {
            "observed_at": view.observed_at if view is not None else None,
            "children": list(view.children) if view is not None else [],
            "last_from_child": old.get("last_from_child", {}),
        }
    return ContainerSnapshot(
        role=ctx.role(),
        ready=ready,
        children_json=json.dumps(children),
        grant_json=grant_json_text(grant, *ctx.default_runner()) if grant is not None else None,
    )


def publish_context(memory: broker_state.BrokerMemory, ctx: ActionContext) -> None:
    """Commit one action's view without publishing other unfinished round state."""
    if memory.queries is not None and memory.store is not None:
        try:
            memory.queries.publish_container(ctx.sender.key, _context_snapshot(memory, ctx))
        except Exception:
            log.exception("broker: publishing query view for %s failed", ctx.sender.key)


def publish_queries(memory: broker_state.BrokerMemory) -> None:
    """Build on the broker thread, then atomically replace the completed-round view.

    Each component is isolated so failed store reads do not hide other updates.
    Neither contexts nor their mutable children/grant data cross into the listener.
    """
    if memory.queries is None or memory.store is None:
        return
    empty_children = json.dumps({"children": [], "observed_at": None, "last_from_child": {}})
    try:
        # A child whose context is missing this round (say, its repo config no
        # longer loads) must not read as plain: outbox ops refuse children.
        snapshot = {
            uuid: ContainerSnapshot(
                "child" if memory.env_roles.get(full) == "child" else "plain",
                True,
                empty_children,
                None,
            )
            for full, uuid in memory.store.recipients().items()
        }
    except Exception:
        log.exception("broker: publishing query recipients failed")
        snapshot = {
            uuid: ContainerSnapshot(
                "plain" if view.role == "plain" else "none", view.ready, empty_children, None
            )
            for uuid, view in memory.queries.snapshot().items()
        }
    for uuid, ctx in memory.contexts.items():
        try:
            snapshot[uuid] = _context_snapshot(memory, ctx)
        except Exception:
            log.exception("broker: publishing query view for %s failed", uuid)
            # Fail closed: never retain a stale grant if serialization fails.
            snapshot[uuid] = ContainerSnapshot("none", False, empty_children, None)
    try:
        memory.queries.publish(snapshot)
    except Exception:
        log.exception("broker: publishing query snapshot failed")
