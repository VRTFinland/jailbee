"""Bounded image capture on the existing authenticated outbox broker channel."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import Any

from jailbee.orchestration import wire
from jailbee.outbox.attachment_store import MAX_CHUNK, AttachmentError
from jailbee.outbox.store_db import OutboxStore


def _string(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise AttachmentError(f"{key} must be a string")
    return value


def _integer(args: Mapping[str, Any], key: str) -> int:
    value = args.get(key)
    if type(value) is not int:
        raise AttachmentError(f"{key} must be an integer")
    return value


def handle(
    store: OutboxStore, uuid: str, op: str, args: Mapping[str, Any], *, now: float
) -> object:
    """Caller identity is supplied by the socket, never an argument."""
    captures = store.attachments
    try:
        if op == wire.OUTBOX_ATTACHMENT_BEGIN:
            captures.collect(now)
            blob = captures.begin(
                uuid,
                _string(args, "name"),
                _string(args, "content_type"),
                _integer(args, "size"),
                _string(args, "sha256"),
                now=now,
            )
            return {"blob": blob}
        blob = _string(args, "blob")
        if op == wire.OUTBOX_ATTACHMENT_CHUNK:
            encoded = _string(args, "data")
            if len(encoded) > ((MAX_CHUNK + 2) // 3) * 4:
                raise AttachmentError("attachment chunk exceeds protocol limit")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise AttachmentError("attachment chunk must be valid base64") from exc
            captures.append(uuid, blob, _integer(args, "offset"), data, now=now)
            return {"offset": _integer(args, "offset") + len(data)}
        if op == wire.OUTBOX_ATTACHMENT_FINISH:
            return captures.finish(uuid, blob, now=now)
        raise AttachmentError("unknown attachment operation")
    except AttachmentError as exc:
        raise wire.WireError(wire.BAD_REQUEST, str(exc)) from exc
