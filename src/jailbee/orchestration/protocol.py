"""Manifest (outbox) and inbox message models for container orchestration."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from jailbee.orchestration.grant import ChildRights

MAX_PROMPT_BYTES = 64 * 1024

ORCHESTRATOR_TYPES = frozenset({"spawn", "message", "import", "restart", "stop", "destroy"})
CHILD_TYPES = frozenset({"message"})

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_BODY_FILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}[.]md$")

# manifest type -> (inline key, file key) of the text field that may live in a file
_TEXT_FIELDS: dict[str, tuple[str, str]] = {
    "spawn": ("prompt", "prompt_file"),
    "restart": ("prompt", "prompt_file"),
    "message": ("body", "body_file"),
}


class ManifestError(ValueError):
    """A manifest cannot be accepted; the message is a one-line reason."""


def _checked_id(v: str) -> str:
    if not _ID_RE.match(v):
        raise ValueError(f"id {v!r} does not match {_ID_RE.pattern}")
    return v


_REF_FORBIDDEN = frozenset("~^:?*[\\")
MAX_REF_BYTES = 255


def _checked_ref(v: str) -> str:
    """A branch name safe to hand to git and the host: `git check-ref-format`-like.

    Refuses what git refuses in a branch name, plus a leading ``-`` (it
    would read as an option on a command line).
    """
    if not v or len(v.encode("utf-8")) > MAX_REF_BYTES:
        raise ValueError(f"must be 1..{MAX_REF_BYTES} bytes")
    if v.startswith("-"):
        raise ValueError(f"{v!r} must not start with '-'")
    if any(ord(c) < 0x20 or c == "\x7f" or c.isspace() for c in v):
        raise ValueError(f"{v!r} must not contain whitespace or control characters")
    if bad := sorted(set(v) & _REF_FORBIDDEN):
        raise ValueError(f"{v!r} must not contain {''.join(bad)!r}")
    if ".." in v or "@{" in v or v == "@":
        raise ValueError(f"{v!r} must not contain '..' or '@{{' or be '@'")
    if v.endswith(("/", ".")):
        raise ValueError(f"{v!r} must not end with '/' or '.'")
    for part in v.split("/"):
        if not part:
            raise ValueError(f"{v!r} must not have an empty path component")
        if part.startswith(".") or part.endswith(".lock"):
            raise ValueError(f"{v!r}: a component must not start with '.' or end with '.lock'")
    return v


def _checked_text(v: str) -> str:
    if len(v.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise ValueError(f"text exceeds {MAX_PROMPT_BYTES} bytes")
    return v


class _Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    id: str

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _checked_id(v)


class SpawnManifest(_Manifest):
    type: Literal["spawn"]
    kind: Literal["fork", "new"]
    branch: str
    base: str | None = None
    prompt: str
    rights: ChildRights
    note: str | None = None

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str) -> str:
        return _checked_text(v)

    @field_validator("branch", "base")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return None if v is None else _checked_ref(v)

    @model_validator(mode="after")
    def _base_only_for_new(self) -> Self:
        if self.base is not None and self.kind != "new":
            raise ValueError("base is only valid for kind 'new'")
        return self


class MessageManifest(_Manifest):
    type: Literal["message"]
    to: str | None = None
    body: str
    kind: Literal["note", "question", "done", "blocked"] | None = None
    in_reply_to: str | None = None

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        return _checked_text(v)

    @field_validator("in_reply_to")
    @classmethod
    def _reply_to(cls, v: str | None) -> str | None:
        # Relayed verbatim into the other container's inbox: an id, nothing else.
        return None if v is None else _checked_id(v)


class ImportManifest(_Manifest):
    type: Literal["import"]
    child: str


class RestartManifest(_Manifest):
    type: Literal["restart"]
    child: str
    prompt: str

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, v: str) -> str:
        return _checked_text(v)


class StopManifest(_Manifest):
    type: Literal["stop"]
    child: str


class DestroyManifest(_Manifest):
    type: Literal["destroy"]
    child: str
    force: bool = False


Manifest = (
    SpawnManifest
    | MessageManifest
    | ImportManifest
    | RestartManifest
    | StopManifest
    | DestroyManifest
)

_MANIFEST_ADAPTER: TypeAdapter[Manifest] = TypeAdapter(
    Annotated[Manifest, Field(discriminator="type")]
)


class InboxMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    version: Literal[1]
    id: str
    type: Literal["message"]
    from_: str = Field(alias="from")
    body: str
    kind: str | None = None
    in_reply_to: str | None = None


class InboxStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    id: str
    type: Literal["status"]
    ref: str
    state: Literal["queued", "started", "done", "refused", "failed"]
    detail: str


InboxItem = InboxMessage | InboxStatus


def new_host_id() -> str:
    return "h-" + secrets.token_hex(4)


def inbox_file_name(item: InboxItem, now: datetime, *, seq: int = 0) -> str:
    """``<stamp>-<seq:08d>-<id>.json``: names sort in emission order.

    The stamp has one-second resolution and ids are random, so `seq` — a
    per-process counter the broker owns (`BrokerMemory.seq`) — orders the
    items written within one second. The stamp leads, so a counter reset
    by a broker restart cannot reorder anything across seconds.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{seq:08d}-{item.id}.json"


def _load_object(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ManifestError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    return data


def _resolve_text(data: dict[str, Any], files: Mapping[str, str]) -> None:
    """Replace ``*_file`` with the inline text field, in place."""
    fields = _TEXT_FIELDS.get(str(data.get("type")))
    if fields is None:
        return
    inline, file_key = fields
    if (inline in data) == (file_key in data):
        raise ManifestError(f"exactly one of {inline!r} / {file_key!r} is required")
    if file_key not in data:
        return
    name = data.pop(file_key)
    if not isinstance(name, str) or not _BODY_FILE_RE.match(name):
        raise ManifestError(f"{file_key} {name!r} does not match {_BODY_FILE_RE.pattern}")
    if name not in files:
        raise ManifestError(f"{file_key} {name!r} not found next to the manifest")
    data[inline] = files[name]


def parse_manifest(file_name: str, text: str, files: Mapping[str, str]) -> Manifest:
    data = _load_object(text)
    stem = PurePosixPath(file_name).stem
    ident = data.get("id")
    if not isinstance(ident, str) or ident != stem:
        raise ManifestError(f"id {ident!r} must equal the file stem {stem!r}")
    _resolve_text(data, files)
    try:
        return _MANIFEST_ADAPTER.validate_python(data)
    except ValidationError as e:
        err = e.errors()[0]
        loc = ".".join(str(p) for p in err["loc"])
        raise ManifestError(f"{loc}: {err['msg']}" if loc else str(err["msg"])) from e


def body_file_of(file_name: str, text: str) -> str | None:
    """The body/prompt file a manifest references, or None (also if unparseable)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("prompt_file", "body_file"):
        name = data.get(key)
        if isinstance(name, str) and _BODY_FILE_RE.match(name):
            return name
    return None
