"""Executor-side handlers for the broker socket ops."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from jailbee.orchestration import broker, publishing, wire
from jailbee.orchestration.actions import Outcome, refused
from jailbee.orchestration.protocol import ManifestError, body_file_of, parse_manifest
from jailbee.outbox_io import _validate_outbox_name

if TYPE_CHECKING:
    from jailbee.orchestration.actions import ActionContext
    from jailbee.orchestration.protocol import Manifest
    from jailbee.orchestration.store import BrokerStore


def handle_send(
    ctx: ActionContext,
    memory: broker.BrokerMemory,
    store: BrokerStore,
    *,
    manifest: object,
    body: object,
    wait: bool,
) -> dict[str, object]:
    """Run one `send` request and return its status item (an `InboxStatus` object).

    The status is journalled in `requests` before it is delivered, so a re-sent
    id gets the recorded status back without acting again. It is inserted into
    the sender's store inbox acked when `wait` (the client prints it now) and
    unacked otherwise (it shows up in the inbox like today's status file).
    """
    if not isinstance(manifest, dict):
        raise wire.WireError(wire.BAD_REQUEST, "manifest must be an object")
    request_id = manifest.get("id")
    if not isinstance(request_id, str) or not request_id:
        raise wire.WireError(wire.BAD_REQUEST, "manifest.id must be a non-empty string")
    if body is not None and not isinstance(body, str):
        raise wire.WireError(wire.BAD_REQUEST, "body must be a string or null")
    file_name = f"{request_id}.json"
    try:
        _validate_outbox_name(file_name)
    except ValueError as e:
        raise wire.WireError(wire.BAD_REQUEST, str(e)) from e
    if ctx.role() == "none":
        raise wire.WireError(wire.NOT_ORCHESTRATED, "this container has no orchestration role")

    recorded = store.lookup_request(ctx.sender.key, request_id)
    if recorded is not None and recorded.item is not None:
        return recorded.item
    if recorded is not None or publishing.was_accepted(ctx, request_id):
        # The file transport drops these silently; a socket client needs an
        # answer. The real status is (or will be) in its inbox. Not recorded.
        return _item(request_id, refused("request already handled; see the inbox for its status"))

    text = json.dumps(manifest)
    files = {file_name: text}
    if body is not None:
        files[body_file_of(file_name, text) or f"{request_id}.md"] = body

    parsed: Manifest | None = None
    preset: Outcome | None = None
    try:
        parsed = parse_manifest(file_name, text, files)
    except ManifestError as e:
        preset = refused(str(e))

    item: dict[str, object] | None = None

    def record(ref: str, outcome: Outcome | None) -> None:
        nonlocal item
        if outcome is not None:
            item = _item(ref, outcome)
        store.record_request(
            ctx.sender.key,
            request_id,
            digest=None,
            state=outcome.state if outcome else "ignored",
            detail=outcome.detail if outcome else "",
            item=item,
            now=time.time(),
        )

    def reply(ref: str, outcome: Outcome) -> None:
        assert item is not None
        store.insert_item(ctx.sender.key, ctx.sender.full, item, now=time.time(), acked=wait)
        broker.clear_reply_path(ctx, ref)

    outcome = broker._handle_manifest(
        ctx,
        memory,
        ref=request_id,
        manifest=parsed,
        outcome=preset,
        record=record,
        reply=reply,
        before_publish=lambda: None,
        label=file_name,
    )
    if item is not None:
        return item
    # dispatch ignored the request (no effective role); nothing was stored.
    return _item(request_id, outcome or refused("request ignored"))


def _item(ref: str, outcome: Outcome) -> dict[str, object]:
    status = broker._status(ref, outcome)
    loaded = json.loads(status.model_dump_json(by_alias=True))
    assert isinstance(loaded, dict)
    return loaded
