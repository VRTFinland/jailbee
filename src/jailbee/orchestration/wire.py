"""Newline-delimited JSON wire protocol between the host broker and containers.

Standard library only: the in-container client imports this module, so it must
not pull in pydantic, ``jailbee.config`` or any other non-stdlib dependency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import IO, Any, Literal, cast

VERSION = 1
MAX_LINE_BYTES = 1024 * 1024  # 1 MiB, newline included
KEEPALIVE_INTERVAL = 30.0
# One outbox proposal (manifest plus bodies) can exceed MAX_LINE_BYTES; its put
# request and the show reply get a larger cap of their own. No chunking.
OUTBOX_MAX_LINE_BYTES = 8 * 1024 * 1024

# The two outboxes every outbox op's ``kind`` names; one definition for the
# container client, the broker ops and the host store.
OutboxKind = Literal["issue", "pr"]
OUTBOX_KINDS: tuple[OutboxKind, ...] = ("issue", "pr")


def outbox_kind(value: object) -> OutboxKind | None:
    """``value`` as an `OutboxKind`, or None when it names neither outbox."""
    if value == "issue":
        return "issue"
    if value == "pr":
        return "pr"
    return None


OUTBOX_PUT = "outbox.put"
OUTBOX_LIST = "outbox.list"
OUTBOX_SHOW = "outbox.show"
OUTBOX_DROP = "outbox.drop"
# The calling container's own sent records, from the host outbox store.
OUTBOX_SENT = "outbox.sent"
OUTBOX_OPS: frozenset[str] = frozenset(
    {OUTBOX_PUT, OUTBOX_LIST, OUTBOX_SHOW, OUTBOX_DROP, OUTBOX_SENT}
)
# Draft batches (`--batch`): appends are small requests; showing one can return up
# to its whole manifest plus bodies, so that reply gets the outbox cap.
OUTBOX_DRAFT_APPEND = "outbox.draft.append"
OUTBOX_DRAFT_SHOW = "outbox.draft.show"
OUTBOX_DRAFT_LIST = "outbox.draft.list"
OUTBOX_DRAFT_SUBMIT = "outbox.draft.submit"
OUTBOX_DRAFT_DISCARD = "outbox.draft.discard"
OUTBOX_DRAFT_READ_OPS: frozenset[str] = frozenset({OUTBOX_DRAFT_SHOW, OUTBOX_DRAFT_LIST})
OUTBOX_DRAFT_OPS: frozenset[str] = OUTBOX_DRAFT_READ_OPS | {
    OUTBOX_DRAFT_APPEND,
    OUTBOX_DRAFT_SUBMIT,
    OUTBOX_DRAFT_DISCARD,
}
OUTBOX_OPS = OUTBOX_OPS | OUTBOX_DRAFT_OPS
# The exact prefix `encode_request` writes for an outbox put: the listener reads
# it before the whole line is in, to know which cap the line gets.
_OUTBOX_PUT_PREFIX = b'{"v":%d,"op":"%s",' % (VERSION, OUTBOX_PUT.encode())

VERSION_ERROR = "version"
BAD_REQUEST = "bad_request"
NOT_ORCHESTRATED = "not_orchestrated"
STORE = "store"
REFUSED = "refused"
BUSY = "busy"
NOT_FOUND = "not_found"
DUPLICATE = "duplicate"

ERROR_CODES: frozenset[str] = frozenset(
    {
        VERSION_ERROR,
        BAD_REQUEST,
        NOT_ORCHESTRATED,
        STORE,
        REFUSED,
        BUSY,
        NOT_FOUND,
        DUPLICATE,
    }
)


@dataclass(frozen=True)
class Request:
    op: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Response:
    ok: bool
    result: Any = None
    error_code: str | None = None
    error_detail: str = ""


class WireError(Exception):
    """A malformed or unacceptable wire message; ``code`` is one of ERROR_CODES."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def ok(result: Any = None) -> Response:
    return Response(ok=True, result=result)


def fail(code: str, detail: str = "") -> Response:
    return Response(ok=False, error_code=code, error_detail=detail)


def request_line_limit(prefix: bytes) -> int:
    """The line cap for a request starting with ``prefix`` (as buffered so far).

    Only the canonical encoding of an ``outbox.put`` gets the outbox cap; any
    other line, or one too short to tell, gets the ordinary cap.
    """
    if prefix.startswith(_OUTBOX_PUT_PREFIX):
        return OUTBOX_MAX_LINE_BYTES
    return MAX_LINE_BYTES


def response_line_limit(op: str) -> int:
    """The line cap for the reply to ``op``."""
    return OUTBOX_MAX_LINE_BYTES if op in (OUTBOX_SHOW, OUTBOX_DRAFT_SHOW) else MAX_LINE_BYTES


def _dump(obj: dict[str, Any], max_bytes: int = MAX_LINE_BYTES) -> bytes:
    try:
        line = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise WireError(BAD_REQUEST, f"message is not JSON-serialisable: {exc}") from exc
    if len(line) > max_bytes:
        raise WireError(BAD_REQUEST, f"message exceeds {max_bytes} bytes")
    return line


def encode_request(req: Request) -> bytes:
    limit = OUTBOX_MAX_LINE_BYTES if req.op == OUTBOX_PUT else MAX_LINE_BYTES
    return _dump({"v": VERSION, "op": req.op, "args": req.args}, limit)


def encode_response(resp: Response, *, max_bytes: int = MAX_LINE_BYTES) -> bytes:
    if resp.ok:
        return _dump({"v": VERSION, "ok": True, "result": resp.result}, max_bytes)
    return _dump(
        {
            "v": VERSION,
            "ok": False,
            "error": {"code": resp.error_code, "detail": resp.error_detail},
        },
        max_bytes,
    )


def _parse(line: bytes, max_bytes: int) -> dict[str, Any]:
    if len(line) > max_bytes:
        raise WireError(BAD_REQUEST, f"message exceeds {max_bytes} bytes")
    try:
        obj: object = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WireError(BAD_REQUEST, f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise WireError(BAD_REQUEST, "message must be a JSON object")
    obj = cast(dict[str, Any], obj)
    version = obj.get("v")
    if type(version) is not int or version != VERSION:
        raise WireError(VERSION_ERROR, f"expected protocol version {VERSION}, got {version!r}")
    return obj


def decode_request(line: bytes, *, max_bytes: int = MAX_LINE_BYTES) -> Request:
    obj = _parse(line, max_bytes)
    op = obj.get("op")
    if not isinstance(op, str):
        raise WireError(BAD_REQUEST, "request op must be a string")
    args = obj.get("args", {})
    if not isinstance(args, dict):
        raise WireError(BAD_REQUEST, "request args must be an object")
    if len(line) > MAX_LINE_BYTES and op != OUTBOX_PUT:
        raise WireError(BAD_REQUEST, f"message exceeds {MAX_LINE_BYTES} bytes")
    return Request(op=op, args=cast(dict[str, Any], args))


def decode_response(line: bytes, *, max_bytes: int = MAX_LINE_BYTES) -> Response:
    obj = _parse(line, max_bytes)
    ok_flag = obj.get("ok")
    if not isinstance(ok_flag, bool):
        raise WireError(BAD_REQUEST, "response ok must be a boolean")
    if ok_flag:
        return Response(ok=True, result=obj.get("result"))
    error = obj.get("error")
    if not isinstance(error, dict):
        raise WireError(BAD_REQUEST, "error response must carry an error object")
    code = error.get("code")
    detail = error.get("detail")
    if not isinstance(code, str) or not isinstance(detail, str):
        raise WireError(BAD_REQUEST, "error code and detail must be strings")
    return Response(ok=False, error_code=code, error_detail=detail)


def read_line(fileobj: IO[bytes], *, max_bytes: int = MAX_LINE_BYTES) -> bytes | None:
    """Read one newline-terminated line, capped at ``max_bytes``.

    Returns None on EOF. Raises WireError(BAD_REQUEST) when the line is over
    the cap, or when it reaches the cap without a trailing newline. After such
    an error the stream is mid-line and unusable: the caller must close the
    connection rather than read on.
    """
    line = fileobj.readline(max_bytes + 1)
    if not line:
        return None
    if len(line) > max_bytes:
        raise WireError(BAD_REQUEST, f"line exceeds {max_bytes} bytes")
    if len(line) == max_bytes and not line.endswith(b"\n"):
        raise WireError(BAD_REQUEST, f"line exceeds {max_bytes} bytes without newline")
    return line
