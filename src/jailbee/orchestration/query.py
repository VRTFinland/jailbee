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
from jailbee.orchestration.store import StoreError

if TYPE_CHECKING:
    from jailbee.orchestration.store import BrokerStore

log = logging.getLogger(__name__)

SNAPSHOT_OPS = frozenset({"children", "status", "grant"})
STORE_OPS = frozenset({"list", "read", "ack"})
QUERY_OPS = SNAPSHOT_OPS | STORE_OPS


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
        if op in STORE_OPS:
            from jailbee.orchestration import socket_ops

            if not self._store.has_recipient_uuid(uuid):
                raise wire.WireError(
                    wire.STORE, "inbox store recipient is not ready; retry shortly"
                )
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
            return socket_ops.handle_ack(self._store, uuid, id=args.get("id"), now=time.time())

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
