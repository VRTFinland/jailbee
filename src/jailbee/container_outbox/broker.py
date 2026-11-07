"""Outbox ops over the host broker socket (container side).

Standard library plus the stdlib-only socket transport. Broker errors become
`OutboxCommandError` with the CLI exit code: 4 when the broker cannot be
reached after the connect retry window (or asks for a retry), 2 for a refused
proposal, 1 for a refused caller (a child container).
"""

from __future__ import annotations

import os
from typing import Any, cast

from jailbee.container_outbox.store import Kind, OutboxCommandError
from jailbee.inbox.transport import (
    CONNECT_RETRY_SECONDS,
    BrokerError,
    BrokerUnreachable,
    BrokerVersionMismatch,
    Client,
    socket_path,
)
from jailbee.orchestration import wire

EXIT_UNREACHABLE = 4
CALL_TIMEOUT = 60.0
RETRY_ENV = "JAILBEE_BROKER_RETRY_SECONDS"  # shared with `jailbee inbox`
_UNREACHABLE = (
    "the host broker is not reachable; nothing was staged. "
    "If this persists, tell the human (`jailbee doctor` on the host)"
)
_UNCERTAIN = (
    "the connection to the host broker dropped after the request was sent; "
    "check `jailbee outbox ls` before staging again"
)
_VERSION = "container and broker versions differ; ask the human to run `jb apply` on the host"


def _retry_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get(RETRY_ENV, CONNECT_RETRY_SECONDS)))
    except ValueError:
        return CONNECT_RETRY_SECONDS


def call(op: str, args: dict[str, Any]) -> Any:
    """One broker op; raises `OutboxCommandError` with the exit code to use."""
    client = Client(socket_path(os.environ), retry_for=_retry_seconds())
    try:
        return client.call(op, args, timeout=CALL_TIMEOUT)
    except BrokerVersionMismatch as exc:
        raise OutboxCommandError(_VERSION, EXIT_UNREACHABLE) from exc
    except BrokerUnreachable as exc:
        message = _UNCERTAIN if op == wire.OUTBOX_PUT and client.request_written else _UNREACHABLE
        raise OutboxCommandError(message, EXIT_UNREACHABLE) from exc
    except TimeoutError as exc:
        message = _UNCERTAIN if op == wire.OUTBOX_PUT else _UNREACHABLE
        raise OutboxCommandError(message, EXIT_UNREACHABLE) from exc
    except BrokerError as exc:
        detail = exc.detail or exc.code
        if exc.code in (wire.STORE, wire.BUSY):
            raise OutboxCommandError(f"host broker: {detail}", EXIT_UNREACHABLE) from exc
        if exc.code in (wire.BAD_REQUEST, wire.NOT_FOUND):
            raise OutboxCommandError(detail, 2) from exc
        raise OutboxCommandError(detail, 1) from exc
    finally:
        client.close()


def put(kind: Kind, slug: str, manifest: dict[str, object], bodies: dict[str, str]) -> str:
    """Stage one proposal; return its ``NNN-slug.json`` name."""
    result = call(
        wire.OUTBOX_PUT, {"kind": kind, "slug": slug, "manifest": manifest, "bodies": bodies}
    )
    return str(cast(dict[str, Any], result)["name"])


def list_proposals(kind: Kind) -> list[dict[str, Any]]:
    result = call(wire.OUTBOX_LIST, {"kind": kind})
    return list(cast(dict[str, Any], result)["proposals"])


def show(kind: Kind, name: str) -> dict[str, Any]:
    return cast(dict[str, Any], call(wire.OUTBOX_SHOW, {"kind": kind, "name": name}))
