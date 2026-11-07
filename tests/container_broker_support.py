"""An in-process broker for container outbox command tests.

Replaces the socket client of `jailbee.container_outbox.broker` with one that
answers through the real `Queries` and outbox store in a temporary
``broker.db``, so commands exercise broker-side validation without a socket
(and never reach a real broker socket the test host may have).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jailbee.inbox.transport import BrokerError
from jailbee.orchestration import wire
from jailbee.orchestration.query import ContainerSnapshot, Queries
from jailbee.orchestration.store import BrokerStore, store_path
from jailbee.outbox import store_db

UUID = "container-uuid"
_EMPTY = json.dumps({"children": [], "observed_at": None, "last_from_child": {}})


class FakeBroker:
    def __init__(self, root: Path, role: str) -> None:
        self.root = root
        self.queries = Queries(BrokerStore(store_path(root)))
        self.queries.publish({UUID: ContainerSnapshot(role, True, _EMPTY, None)})
        self.calls: list[str] = []

    def proposals(self, kind: store_db.Kind | None = "issue") -> list[store_db.Proposal]:
        return store_db.read_proposals(self.root, UUID, kind)

    def only(self) -> tuple[str, dict[str, Any], dict[str, str]]:
        """The single staged issue proposal: name, decoded manifest, bodies."""
        (row,) = self.proposals()
        return row.name, json.loads(row.manifest), dict(row.bodies)

    def draft(self, name: str, kind: store_db.Kind = "issue") -> store_db.Draft | None:
        return store_db.OutboxStore(self.queries._store).get_draft(UUID, kind, name)

    def draft_state(self, name: str) -> tuple[dict[str, Any], dict[str, str]] | None:
        """The issue draft's decoded manifest and bodies, or None."""
        found = self.draft(name)
        return None if found is None else (json.loads(found.manifest), dict(found.bodies))

    def client(self, *args: object, **kwargs: object) -> _Client:
        return _Client(self)


class _Client:
    def __init__(self, broker: FakeBroker) -> None:
        self._broker = broker
        self.request_written = False
        self.connected = True

    def call(self, op: str, args: dict[str, Any], *, timeout: float | None) -> Any:
        # Round-trip through the wire encoding, as the real socket does.
        request = wire.decode_request(
            wire.encode_request(wire.Request(op, args)), max_bytes=wire.OUTBOX_MAX_LINE_BYTES
        )
        self.request_written = True
        self._broker.calls.append(op)
        response = self._broker.queries.answer(UUID, request)
        if not response.ok:
            raise BrokerError(response.error_code or wire.BAD_REQUEST, response.error_detail)
        return response.result

    def close(self) -> None:
        pass


def install(monkeypatch: pytest.MonkeyPatch, root: Path, role: str = "plain") -> FakeBroker:
    from jailbee.container_outbox import broker as client_module

    root.mkdir(parents=True, exist_ok=True)
    fake = FakeBroker(root, role)
    monkeypatch.setattr(client_module, "Client", fake.client)
    return fake
