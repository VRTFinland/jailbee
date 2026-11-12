"""Outbox ops over the host broker socket (container side).

Standard library plus the stdlib-only socket transport. Broker errors become
`OutboxCommandError` with the CLI exit code: 4 when the broker cannot be
reached after the connect retry window (or asks for a retry), 2 for a refused
proposal, 1 for a refused caller (a child container).
"""

from __future__ import annotations

import os
from typing import Any, cast

from jailbee.container_outbox.common import Kind, OutboxCommandError
from jailbee.inbox.transport import (
    BrokerError,
    BrokerUnreachable,
    BrokerVersionMismatch,
    Client,
    retry_seconds,
    socket_path,
)
from jailbee.orchestration import wire

EXIT_UNREACHABLE = 4
CALL_TIMEOUT = 60.0
_UNREACHABLE = (
    "the host broker is not reachable; nothing was staged. "
    "If this persists, tell the human (`jailbee doctor` on the host)"
)
_UNCERTAIN = (
    "the connection to the host broker dropped after the request was sent; "
    "check `jailbee outbox ls` before staging again"
)
# Ops whose retry after a dropped reply could repeat the effect (a second proposal
# or draft action); they get the "check before trying again" message.
_WRITE_OPS = frozenset({wire.OUTBOX_PUT, wire.OUTBOX_DRAFT_APPEND, wire.OUTBOX_DRAFT_SUBMIT})
_VERSION = "container and broker versions differ; ask the human to run `jb apply` on the host"


def call(op: str, args: dict[str, Any]) -> Any:
    """One broker op; raises `OutboxCommandError` with the exit code to use."""
    client = Client(socket_path(os.environ), retry_for=retry_seconds())
    try:
        return client.call(op, args, timeout=CALL_TIMEOUT)
    except BrokerVersionMismatch as exc:
        raise OutboxCommandError(_VERSION, EXIT_UNREACHABLE) from exc
    except BrokerUnreachable as exc:
        message = _UNCERTAIN if op in _WRITE_OPS and client.request_written else _UNREACHABLE
        raise OutboxCommandError(message, EXIT_UNREACHABLE) from exc
    except TimeoutError as exc:
        message = _UNCERTAIN if op in _WRITE_OPS else _UNREACHABLE
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


def sent(*, limit: int | None = None, kind: Kind | None = None) -> list[dict[str, Any]]:
    """This container's sent records (what the host published for it), newest first."""
    args: dict[str, Any] = {}
    if limit is not None:
        args["limit"] = limit
    if kind is not None:
        args["kind"] = kind
    return list(cast(dict[str, Any], call(wire.OUTBOX_SENT, args))["records"])


def show(kind: Kind, name: str) -> dict[str, Any]:
    return cast(dict[str, Any], call(wire.OUTBOX_SHOW, {"kind": kind, "name": name}))


def drop(kind: Kind, name: str) -> None:
    """Remove one staged, unpublished proposal."""
    call(wire.OUTBOX_DROP, {"kind": kind, "name": name})


def draft_append(
    kind: Kind,
    name: str,
    action: dict[str, object],
    bodies: dict[str, str],
    *,
    envelope: dict[str, object] | None = None,
) -> dict[str, Any]:
    """Append one action to the draft batch NAME; the host derives @REF expectations.

    A PR draft carries the PR ``envelope`` (repo, pr, head_sha) read with ``gh``:
    the first append fixes it, later ones must match it."""
    args: dict[str, Any] = {"kind": kind, "name": name, "action": action, "bodies": bodies}
    if envelope is not None:
        args["envelope"] = envelope
    return cast(dict[str, Any], call(wire.OUTBOX_DRAFT_APPEND, args))


def draft_append_many(
    kind: Kind, name: str, actions: list[dict[str, object]], bodies: dict[str, str]
) -> dict[str, Any]:
    """Append one command's issue actions atomically in one broker request."""
    return cast(
        dict[str, Any],
        call(
            wire.OUTBOX_DRAFT_APPEND,
            {"kind": kind, "name": name, "actions": actions, "bodies": bodies},
        ),
    )


def draft_list(kind: Kind) -> list[dict[str, Any]]:
    result = call(wire.OUTBOX_DRAFT_LIST, {"kind": kind})
    return list(cast(dict[str, Any], result)["drafts"])


def draft_show(kind: Kind, name: str) -> dict[str, Any]:
    return cast(dict[str, Any], call(wire.OUTBOX_DRAFT_SHOW, {"kind": kind, "name": name}))


def draft_submit(
    kind: Kind,
    name: str,
    *,
    review: dict[str, object] | None = None,
    bodies: dict[str, str] | None = None,
) -> str:
    """Stage the draft as a proposal and remove it; return the proposal name.

    A PR review draft is submitted with ``review``: the head it was written
    against and its summary (``body``, or ``body_file`` named in ``bodies``)."""
    args: dict[str, Any] = {"kind": kind, "name": name}
    if review is not None:
        args["review"] = review
    if bodies:
        args["bodies"] = bodies
    result = call(wire.OUTBOX_DRAFT_SUBMIT, args)
    return str(cast(dict[str, Any], result)["name"])


def draft_discard(kind: Kind, name: str) -> None:
    call(wire.OUTBOX_DRAFT_DISCARD, {"kind": kind, "name": name})
