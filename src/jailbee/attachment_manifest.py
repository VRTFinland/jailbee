"""Validation of immutable attachment metadata in outbox manifests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from types import MappingProxyType

from jailbee.attachments import AttachmentError

_EXTENSIONS = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
    "image/webp": {".webp"},
}
_FIELDS = {"blob", "name", "content_type", "size", "sha256"}


@dataclass(frozen=True)
class Attachment:
    blob: str
    name: str
    content_type: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


def parse_attachments(value: object) -> Mapping[str, Attachment]:
    if not isinstance(value, dict):
        raise AttachmentError("attachments must be an object")
    result: dict[str, Attachment] = {}
    for identifier, item in value.items():
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", identifier
        ):
            raise AttachmentError("invalid attachment ID")
        if not isinstance(item, dict) or set(item) != _FIELDS:
            raise AttachmentError(f"attachment {identifier!r} must contain exactly {_FIELDS}")
        blob, name, content_type, size, sha256 = (
            item[k] for k in ("blob", "name", "content_type", "size", "sha256")
        )
        if not isinstance(blob, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", blob):
            raise AttachmentError(f"attachment {identifier!r}: invalid blob")
        try:
            valid_name = (
                isinstance(name, str)
                and bool(name)
                and name not in {".", ".."}
                and len(name.encode("utf-8")) <= 255
                and not any(c in name for c in "/\\")
                and not any(ord(c) < 32 or ord(c) == 127 for c in name)
            )
        except UnicodeEncodeError:
            valid_name = False
        if not valid_name or not isinstance(name, str):
            raise AttachmentError(f"attachment {identifier!r}: invalid name")
        if (
            not isinstance(content_type, str)
            or content_type not in _EXTENSIONS
            or PurePosixPath(name).suffix.lower() not in _EXTENSIONS[content_type]
        ):
            raise AttachmentError(f"attachment {identifier!r}: invalid content type or suffix")
        if type(size) is not int or not 0 < size <= 10_000_000:
            raise AttachmentError(f"attachment {identifier!r}: invalid size")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise AttachmentError(f"attachment {identifier!r}: invalid sha256")
        result[identifier] = Attachment(blob, name, content_type, size, sha256)
    return MappingProxyType(result)
