"""Newline-delimited JSON wire protocol between the host broker and containers.

Standard library only: the in-container client imports this module, so it must
not pull in pydantic, ``jailbee.config`` or any other non-stdlib dependency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import IO, Any, cast

VERSION = 1
MAX_LINE_BYTES = 1024 * 1024  # 1 MiB, newline included

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


def _dump(obj: dict[str, Any]) -> bytes:
    try:
        line = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise WireError(BAD_REQUEST, f"message is not JSON-serialisable: {exc}") from exc
    if len(line) > MAX_LINE_BYTES:
        raise WireError(BAD_REQUEST, f"message exceeds {MAX_LINE_BYTES} bytes")
    return line


def encode_request(req: Request) -> bytes:
    return _dump({"v": VERSION, "op": req.op, "args": req.args})


def encode_response(resp: Response) -> bytes:
    if resp.ok:
        return _dump({"v": VERSION, "ok": True, "result": resp.result})
    return _dump(
        {
            "v": VERSION,
            "ok": False,
            "error": {"code": resp.error_code, "detail": resp.error_detail},
        }
    )


def _parse(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_LINE_BYTES:
        raise WireError(BAD_REQUEST, f"message exceeds {MAX_LINE_BYTES} bytes")
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


def decode_request(line: bytes) -> Request:
    obj = _parse(line)
    op = obj.get("op")
    if not isinstance(op, str):
        raise WireError(BAD_REQUEST, "request op must be a string")
    args = obj.get("args", {})
    if not isinstance(args, dict):
        raise WireError(BAD_REQUEST, "request args must be an object")
    return Request(op=op, args=cast(dict[str, Any], args))


def decode_response(line: bytes) -> Response:
    obj = _parse(line)
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


def read_line(fileobj: IO[bytes]) -> bytes | None:
    """Read one newline-terminated line, capped at MAX_LINE_BYTES.

    Returns None on EOF. Raises WireError(BAD_REQUEST) when the line is over
    the cap, or when it reaches the cap without a trailing newline. After such
    an error the stream is mid-line and unusable: the caller must close the
    connection rather than read on.
    """
    line = fileobj.readline(MAX_LINE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_LINE_BYTES:
        raise WireError(BAD_REQUEST, f"line exceeds {MAX_LINE_BYTES} bytes")
    if len(line) == MAX_LINE_BYTES and not line.endswith(b"\n"):
        raise WireError(BAD_REQUEST, f"line exceeds {MAX_LINE_BYTES} bytes without newline")
    return line
