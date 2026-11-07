"""Executor-side handlers for the broker socket ops."""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jailbee.inbox.render import jq_tostring
from jailbee.orchestration import broker_replies, broker_state, manifests, publishing, wake, wire
from jailbee.orchestration.actions import Outcome, refused
from jailbee.orchestration.outbox import acknowledge_delivery, deliver, grant_json_text
from jailbee.orchestration.protocol import ManifestError, body_file_of, parse_manifest
from jailbee.orchestration.store import StoreError
from jailbee.outbox_io import _validate_outbox_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.orchestration.actions import ActionContext
    from jailbee.orchestration.protocol import Manifest
    from jailbee.orchestration.store import BrokerStore, ItemMeta

log = logging.getLogger(__name__)


def handle_send(
    ctx: ActionContext,
    memory: broker_state.BrokerMemory,
    store: BrokerStore,
    *,
    manifest: object,
    body: object,
    wait: bool,
    is_connected: Callable[[], bool],
) -> dict[str, object]:
    """Run one `send` request and return its status item (an `InboxStatus` object).

    The status is journalled in `requests` before it is delivered, so a re-sent
    id gets the recorded status back without acting again. It is inserted into
    the sender's inbox through the ordinary pending gate. A live client consumes
    a refusal or a `wait` response; a client gone during handling leaves it unread.
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

    ctx = dataclasses.replace(ctx, now=datetime.now(UTC))

    def deliver_status(status: dict[str, object]) -> None:
        deliver(
            ctx.incus,
            ctx.root,
            ctx.sender.full,
            status,
            running=ctx.sender.running,
            uid=ctx.cfg.container_user.uid,
            now=ctx.now,
            seq=next(ctx.seq),
            recipient_uuid=ctx.sender.key,
            acked=False,
        )

    def consume_status(status: dict[str, object]) -> None:
        if wait or status.get("state") == "refused":
            acknowledge_delivery(
                ctx.root,
                ctx.sender.full,
                str(status["id"]),
                recipient_uuid=ctx.sender.key,
                is_connected=is_connected,
            )

    recorded = store.lookup_request(ctx.sender.key, request_id)
    if recorded is not None and recorded.item is not None:
        # A crash between the journal and the insert may have lost the item;
        # the insert ignores a duplicate.
        if store.get(ctx.sender.key, str(recorded.item["id"])) is None:
            deliver_status(recorded.item)
        consume_status(recorded.item)
        return recorded.item
    if recorded is not None or publishing.was_accepted(ctx, request_id):
        # Requests without a recorded status need a stable duplicate answer,
        # not a refusal. The real status is (or will be) in the inbox, or was
        # already reported.
        raise wire.WireError(
            wire.DUPLICATE,
            f"request {request_id} was already handled; "
            "its status is in the inbox or was already reported",
        )

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
        deliver_status(item)
        try:
            broker_replies.clear_reply_path(ctx, ref)
        except OSError:
            # Only a stale gate remains; the status itself is delivered.
            log.warning("broker: %s: could not clear the reply gate of %s", ctx.sender.full, ref)

    outcome = manifests.handle_manifest(
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
        consume_status(item)
        return item
    # dispatch ignored the request (no effective role); nothing was stored.
    return _item(request_id, outcome or refused("request ignored"))


def _item(ref: str, outcome: Outcome) -> dict[str, object]:
    status = broker_replies.status(ref, outcome)
    loaded = json.loads(status.model_dump_json(by_alias=True))
    assert isinstance(loaded, dict)
    return loaded


# Longest text any `ls_line` cell shows is 60 characters; keep headroom.
_LIST_TEXT_CAP = 200
_LIST_FIELDS = (
    "id",
    "type",
    "kind",
    "from",
    "ref",
    "state",
    "body",
    "detail",
    "imported_sha",
    "import_error",
    "on_done_reason",
)
_LIST_TEXT_FIELDS = ("body", "detail", "on_done_reason")


def _list_view(item: dict[str, object]) -> dict[str, object]:
    """Only what `inbox.render.ls_line` reads, long texts cut, so a long inbox
    of big bodies still fits one response line. `ls_line` output is unchanged."""
    view: dict[str, object] = {}
    for key in _LIST_FIELDS:
        if key not in item:
            continue
        value = item[key]
        if key in _LIST_TEXT_FIELDS and value is not None and value is not False:
            # ls_line stringifies (jq_tostring) then cuts; do the same up front.
            value = jq_tostring(value)
            value = value[:_LIST_TEXT_CAP]
        elif key == "imported_sha" and isinstance(value, str):
            value = value[:12]
        elif key == "import_error" and value is not None and value is not False:
            value = True
        view[key] = value
    return view


# A list page's encoded items stay under this, well inside one wire line.
LIST_PAGE_BYTES = wire.MAX_LINE_BYTES // 2
_LIST_FETCH = 256  # rows read per store query while filling a page


def _list_page(
    store: BrokerStore, uuid: str, after: int
) -> tuple[list[dict[str, object]], int | None]:
    """The oldest unacked views after seq `after` that fit `LIST_PAGE_BYTES`,
    and the cursor of the next page (None when this page reached the end)."""
    views: list[dict[str, object]] = []
    used = 0
    while True:
        rows = store.unacked(uuid, after=after, limit=_LIST_FETCH)
        for row in rows:
            view = _list_view(row.item)
            size = len(json.dumps(view, ensure_ascii=False).encode()) + 2
            if views and used + size > LIST_PAGE_BYTES:
                return views, after
            views.append(view)
            used += size
            after = row.seq
        if len(rows) < _LIST_FETCH:
            return views, None


def handle_list(store: BrokerStore, uuid: str, *, after: object) -> object:
    """The unacked items of a container's inbox, oldest first, in `ls` shape.

    `after` is 0 for the first page, then the previous page's `next` cursor.
    The answer is ``{"items": [...], "next": cursor | None}``.
    """
    if type(after) is not int or after < 0:
        raise wire.WireError(wire.BAD_REQUEST, "after must be a non-negative integer")
    items, cursor = _list_page(store, uuid, after)
    return {"items": items, "next": cursor}


def handle_read(
    store: BrokerStore, uuid: str, *, id: object, keep: object, now: float
) -> dict[str, object]:
    """Return one unacked item, acking it unless `keep`."""
    if not isinstance(id, str) or not id:
        raise wire.WireError(wire.BAD_REQUEST, "id must be a non-empty string")
    if not isinstance(keep, bool):
        raise wire.WireError(wire.BAD_REQUEST, "keep must be a boolean")
    row = store.get(uuid, id)
    if row is None or row.acked_at is not None:
        raise wire.WireError(wire.NOT_FOUND, f"no unread item '{id}'")
    if not keep:
        store.ack(uuid, id, now=now)
    return row.item


def handle_ack(store: BrokerStore, uuid: str, *, id: object, now: float) -> dict[str, object]:
    """Mark one item read (idempotent); an unknown id is `not_found`."""
    if not isinstance(id, str) or not id:
        raise wire.WireError(wire.BAD_REQUEST, "id must be a non-empty string")
    if not store.ack(uuid, id, now=now):
        raise wire.WireError(wire.NOT_FOUND, f"no item '{id}'")
    return {"acked": True}


def _orchestrator(memory: broker_state.BrokerMemory, uuid: str) -> ActionContext:
    ctx = memory.contexts.get(uuid)
    if ctx is None or ctx.role() != "orchestrator":
        raise wire.WireError(wire.NOT_ORCHESTRATED, "this container is not an orchestrator")
    return ctx


def children_result(
    memory: broker_state.BrokerMemory, store: BrokerStore, uuid: str, ctx: ActionContext
) -> dict[str, object]:
    view = memory.children.get(ctx.sender.full)
    return {
        "observed_at": view.observed_at if view is not None else None,
        "children": list(view.children) if view is not None else [],
        "last_from_child": wake.newest_from_child(store.history_meta(uuid)),
    }


def handle_children(
    memory: broker_state.BrokerMemory, store: BrokerStore, uuid: str
) -> dict[str, object]:
    """The orchestrator's published children list and when each child last wrote."""
    return children_result(memory, store, uuid, _orchestrator(memory, uuid))


def handle_status(
    memory: broker_state.BrokerMemory, store: BrokerStore, uuid: str, *, child: object
) -> dict[str, object]:
    """Like `handle_children`, optionally narrowed to one child by name."""
    if child is not None and not isinstance(child, str):
        raise wire.WireError(wire.BAD_REQUEST, "child must be a string or null")
    result = children_result(memory, store, uuid, _orchestrator(memory, uuid))
    if child is not None:
        children = result["children"]
        assert isinstance(children, list)
        picked = [c for c in children if c.get("name") == child]
        if not picked:
            raise wire.WireError(wire.NOT_FOUND, f"unknown child '{child}'")
        result["children"] = picked
    return result


def handle_grant(memory: broker_state.BrokerMemory, uuid: str) -> dict[str, object]:
    ctx = memory.contexts.get(uuid)
    grant = ctx.grant() if ctx is not None else None
    if ctx is None or grant is None:
        raise wire.WireError(wire.NOT_ORCHESTRATED, "this container holds no orchestration grant")
    loaded = json.loads(grant_json_text(grant, *ctx.default_runner()))
    assert isinstance(loaded, dict)
    return loaded


# -- long-poll ---------------------------------------------------------------


@dataclass(frozen=True)
class NextArgs:
    wake: frozenset[str] | None
    quiet: int
    digest: int
    timeout: float | None


def _positive_int(args: dict[str, object], key: str) -> int:
    value = args.get(key)
    if value is None:
        return 0
    if type(value) is not int or value <= 0:
        raise wire.WireError(wire.BAD_REQUEST, f"{key} must be a positive integer")
    return value


def handle_next_args(args: dict[str, object]) -> NextArgs:
    """Validate the `next` arguments (BAD_REQUEST on anything malformed)."""
    tokens = args.get("wake_on")
    wake_set: frozenset[str] | None = None
    if tokens is not None:
        if not isinstance(tokens, list) or not all(
            isinstance(t, str) and wake.valid_wake_token(t) for t in tokens
        ):
            raise wire.WireError(wire.BAD_REQUEST, "wake_on must be a list of valid wake tokens")
        wake_set = frozenset(tokens)
    raw_timeout = args.get("timeout")
    timeout: float | None = None
    if raw_timeout is not None:
        if (
            type(raw_timeout) not in (int, float)
            or not math.isfinite(raw_timeout)  # type: ignore[arg-type]  # type() test above
            or raw_timeout < 0  # type: ignore[operator]  # type() test above
        ):
            raise wire.WireError(wire.BAD_REQUEST, "timeout must be a non-negative number")
        timeout = float(raw_timeout)  # type: ignore[arg-type]  # validated above
    return NextArgs(
        wake=wake_set,
        quiet=_positive_int(args, "quiet_after"),
        digest=_positive_int(args, "digest"),
        timeout=timeout,
    )


SETTLE_SECONDS = 1.0


@dataclass
class Waiter:
    uuid: str
    conn_id: int
    wake: frozenset[str] | None
    quiet: int
    digest: int
    deadline: float | None  # time.monotonic()
    waited: bool = False
    settle_until: float | None = None


class Waiters:
    """Parked `next` long-polls: at most one per container."""

    def __init__(self) -> None:
        self._by_uuid: dict[str, Waiter] = {}

    def __len__(self) -> int:
        return len(self._by_uuid)

    def add(self, waiter: Waiter) -> None:
        if waiter.uuid in self._by_uuid:
            raise wire.WireError(wire.BUSY, "another process is already waiting in this container")
        self._by_uuid[waiter.uuid] = waiter

    def drop(self, uuid: str, conn_id: int) -> None:
        current = self._by_uuid.get(uuid)
        if current is not None and current.conn_id == conn_id:
            del self._by_uuid[uuid]

    def evaluate(
        self, memory: broker_state.BrokerMemory, store: BrokerStore, *, now: float, mono: float
    ) -> list[tuple[Waiter, dict[str, object] | wire.WireError]]:
        """Answer every waiter that is ready; the answered ones are removed."""
        answered: list[tuple[Waiter, dict[str, object] | wire.WireError]] = []
        for uuid, waiter in list(self._by_uuid.items()):
            try:
                outcome = _evaluate_one(waiter, memory, store, now=now, mono=mono)
            except StoreError as e:
                outcome = wire.WireError(wire.STORE, str(e))
            if outcome is not None:
                del self._by_uuid[uuid]
                answered.append((waiter, outcome))
        return answered


def _evaluate_one(
    waiter: Waiter,
    memory: broker_state.BrokerMemory,
    store: BrokerStore,
    *,
    now: float,
    mono: float,
) -> dict[str, object] | wire.WireError | None:
    if waiter.settle_until is not None and mono < waiter.settle_until:
        return None
    uuid = waiter.uuid
    ctx = memory.contexts.get(uuid)
    if (waiter.quiet or waiter.digest) and (ctx is None or ctx.role() != "orchestrator"):
        return wire.WireError(wire.NOT_ORCHESTRATED, "this container is not an orchestrator")
    view = (
        memory.children.get(ctx.sender.full)
        if ctx is not None and ctx.role() == "orchestrator"
        else None
    )
    history = store.history_meta(uuid)
    try:
        found, stamp = wake.select_wakeup(
            store.unacked(uuid),
            history,
            view.children if view is not None else None,
            children_observed_at=view.observed_at if view is not None else None,
            wake=waiter.wake,
            quiet=waiter.quiet,
            digest=waiter.digest,
            digest_at=store.get_digest_at(uuid),
            now=now,
        )
    except wake.WakeUsage as e:
        return wire.WireError(wire.BAD_REQUEST, str(e))
    settled = waiter.settle_until is not None
    if found is None:
        if stamp is not None:
            store.set_digest_at(uuid, stamp)
        waiter.settle_until = None
        waiter.waited = True
        if waiter.deadline is not None and mono >= waiter.deadline:
            return {"kind": "timeout"}
        return None
    if waiter.waited and not settled:
        # Let a burst of related messages land before answering (run_wait's settle).
        waiter.settle_until = mono + SETTLE_SECONDS
        return None
    if stamp is not None:
        store.set_digest_at(uuid, stamp)
    return _wake_result(found, view, history, now)


def _wake_result(
    found: wake.Wake,
    view: broker_state.ChildrenView | None,
    history: list[ItemMeta],
    now: float,
) -> dict[str, object]:
    if isinstance(found, wake.MessageWake):
        return {"kind": "message", "id": found.id}
    if isinstance(found, wake.QuietChildWake):
        return {"kind": "quiet-child", "child": found.child}
    return {
        "kind": "digest",
        "children": list(view.children) if view is not None else [],
        "last_from_child": wake.newest_from_child(history),
        "observed_at": view.observed_at if view is not None else now,
    }
