"""Inbox commands over the host broker socket (standard library only)."""

import os
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

from jailbee.inbox import cli
from jailbee.inbox.cli import PROG
from jailbee.inbox.errors import InboxUsage
from jailbee.inbox.render import clean_c0c1, ls_line, pretty, render_status
from jailbee.inbox.send import SendRequest, prepare_send, validate
from jailbee.inbox.transport import (
    NO_REPLY_MESSAGE,
    BrokerError,
    BrokerUnreachable,
    Client,
    call_next,
    call_send,
    socket_path,
)
from jailbee.orchestration import wire

STALE_AFTER = 900
# The broker serves requests only between its rounds, which can take a while.
CALL_TIMEOUT = 120.0


def _factory() -> Client:
    path = socket_path(os.environ)
    if not path.exists():
        raise BrokerUnreachable(
            "jailbee inbox: the host broker socket is missing; "
            "ask the host operator to check orchestration"
        )
    return Client(path)


def _call(op: str, args: dict[str, Any] | None = None) -> Any:
    with _factory() as client:
        return client.call(op, args or {}, timeout=CALL_TIMEOUT)


def _fail(exc: BrokerError) -> int:
    """Map an unexpected broker error code to a message and exit status."""
    if exc.code == wire.STORE:
        raise exc  # container_cmd._run maps it to exit 4 with the detail
    print(f"{PROG}: {exc.detail or exc.code}", file=sys.stderr)
    return 2 if exc.code == wire.BAD_REQUEST else 1


def _unread() -> Iterator[dict[str, Any]]:
    """Every unread item, oldest first, one `list` page at a time."""
    after: Any = 0
    while after is not None:
        result = _call("list", {"after": after})
        yield from result["items"]
        after = result.get("next")


def cmd_ls() -> int:
    for item in _unread():
        print(ls_line(item))
    return 0


def cmd_read(ident: str, *, keep: bool = False) -> int:
    # Read without acking, print, then ack: a lost reply or a failed print can
    # leave the message unread but never lose it.
    try:
        item = _call("read", {"id": ident, "keep": True})
    except BrokerError as exc:
        if exc.code == wire.NOT_FOUND:
            print(f"{PROG}: no unread message with id {ident}", file=sys.stderr)
            return 1
        return _fail(exc)
    print(pretty(cli._clean_json(item)), flush=True)
    if not keep:
        try:
            _call("ack", {"id": ident})
        except (BrokerError, BrokerUnreachable, OSError) as exc:
            print(
                f"{PROG}: printed {ident} but could not mark it read ({exc}); it stays unread",
                file=sys.stderr,
            )
    return 0


def cmd_wait(
    *,
    timeout: str = "30m",
    wake_on: str | None = None,
    quiet_after: str | None = None,
    digest: str | None = None,
    path: bool = False,
) -> int:
    seconds, quiet, digest_seconds, wake = cli.parse_wait_args(
        timeout, wake_on, quiet_after, digest
    )
    return wait_once(seconds, quiet, digest_seconds, wake, path=path)


def wait_once(
    seconds: float | None,
    quiet: int,
    digest_seconds: int,
    wake: frozenset[str] | None,
    *,
    path: bool = False,
) -> int:
    """One ordinary socket wait: prints exactly one result line (none on timeout)."""
    args: dict[str, Any] = {
        "wake_on": sorted(wake) if wake is not None else None,
        "quiet_after": quiet or None,
        "digest": digest_seconds or None,
    }
    try:
        if seconds == 0:
            with _factory() as client:
                result = client.call("next", {**args, "timeout": 0}, timeout=CALL_TIMEOUT)
        else:
            result = call_next(_factory, args, timeout=seconds)
    except BrokerError as exc:
        if exc.code in (wire.BUSY, wire.BAD_REQUEST):
            raise InboxUsage(exc.detail or "invalid wait request") from None
        return _fail(exc)
    kind = result.get("kind")
    if kind == "message":
        print(clean_c0c1(str(result["id"])))
        if path:
            print(
                f"{PROG}: --path is obsolete with the broker socket; printed the message id",
                file=sys.stderr,
            )
        return 0
    if kind == "quiet-child":
        print(f"quiet-child {clean_c0c1(str(result['child']))}")
        return 0
    if kind == "digest":
        print("digest")
        print(
            render_status(
                list(result.get("children") or []),
                now=time.time(),
                from_child=result.get("last_from_child") or {},
            )
        )
        return 0
    return 1


def cmd_send(request: SendRequest) -> int:
    request = validate(request)
    ident, manifest, body = prepare_send(request)
    # Without --wait the broker still answers only between rounds: same deadline.
    timeout = cli.parse_timeout(request.timeout or "30m")
    deadline = None if timeout is None else time.monotonic() + timeout
    # Always expose the recovery id before sending, while keeping JSON stdout pure.
    print(ident, file=sys.stderr if request.json_output else sys.stdout, flush=True)
    try:
        item = call_send(
            _factory, {"manifest": manifest, "body": body, "wait": request.wait}, timeout=timeout
        )
    except BrokerError as exc:
        if exc.code == wire.DUPLICATE:
            print(f"{PROG}: {exc.detail}", file=sys.stderr)
            return 0
        return _fail(exc)
    except TimeoutError:
        print(NO_REPLY_MESSAGE, file=sys.stderr)
        return 3
    final_id = None
    state = item.get("state") if isinstance(item, dict) else None
    if request.until == "done" and state in ("started", "queued"):
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            result = call_next(_factory, {"ref": ident}, timeout=remaining)
            if result.get("kind") == "timeout":
                print(f"{PROG}: timed out waiting for publish {ident}; inspect inbox ls/read before retrying", file=sys.stderr)
                return 3
            if result.get("kind") != "status":
                raise BrokerUnreachable(
                    f"{PROG}: host broker does not support completion waits; "
                    f"upgrade/restart it and inspect publish {ident} in the inbox"
                )
            item = result["item"]
            state = item.get("state")
            final_id = item["id"]
        except BrokerError as exc:
            return _fail(exc)
    if request.json_output:
        print(pretty(cli._clean_json(item if request.wait else {"id": ident, "state": state})), flush=True)
    if state == "refused":
        print(
            f"{PROG}: {clean_c0c1(str(item.get('detail') or 'request refused'))}", file=sys.stderr
        )
        return 1
    if not request.wait:
        return 0
    if not request.json_output:
        print(pretty(cli._clean_json(item)), flush=True)
    if final_id is not None:
        try:
            _call("ack", {"id": final_id})
        except (BrokerError, BrokerUnreachable, OSError) as exc:
            print(f"{PROG}: printed {final_id} but could not mark it read ({exc}); it stays unread", file=sys.stderr)
    return 0 if state in ("done", "queued", "started") else 1


def _children_view(op: str, args: dict[str, Any], what: str) -> dict[str, Any] | None:
    try:
        result: dict[str, Any] = _call(op, args)
    except BrokerError as exc:
        if exc.code == wire.NOT_ORCHESTRATED:
            print(f"{PROG}: no children view here (not an orchestrator)", file=sys.stderr)
        elif exc.code == wire.NOT_FOUND:
            print(f"{PROG}: unknown child '{clean_c0c1(what)}'", file=sys.stderr)
        else:
            _fail(exc)
        return None
    observed = result.get("observed_at")
    if not isinstance(observed, (int, float)) or time.time() - observed > STALE_AFTER:
        print(f"{PROG}: children view is stale (over 15 minutes old)", file=sys.stderr)
    return result


def cmd_children(*, as_json: bool = False) -> int:
    result = _children_view("children", {}, "")
    if result is None:
        return 1
    return cli.render_children(result["children"], where="broker", line=1, as_json=as_json)


def cmd_status(*, child: str | None = None, as_json: bool = False) -> int:
    result = _children_view("status", {"child": child}, child or "")
    if result is None:
        return 1
    return cli.render_status_view(
        list(result["children"]), result.get("last_from_child") or {}, child=child, as_json=as_json
    )


def _grant() -> dict[str, Any] | None:
    try:
        grant: dict[str, Any] = _call("grant")
    except BrokerError as exc:
        if exc.code == wire.NOT_ORCHESTRATED:
            return None
        raise
    return grant


def cmd_grant(*, as_json: bool = False) -> int:
    try:
        grant = _grant()
    except BrokerError as exc:
        return _fail(exc)
    if grant is None:
        print(f"{PROG}: no orchestration grant here yet", file=sys.stderr)
        return 1
    return cli.render_grant(grant, as_json=as_json)


def cmd_runners(*, as_json: bool = False) -> int:
    if not socket_path(os.environ).exists():
        return cli.cmd_runners(None, as_json=as_json)
    try:
        grant = _grant()
    except BrokerError as exc:
        return _fail(exc)
    return cli.cmd_runners(grant, as_json=as_json)


Command = Callable[[], int]
