"""Capture local image inputs through the authenticated outbox broker."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO

from jailbee.container_outbox import broker

_MAX_SIZE = 10_000_000
_CHUNK = 192 * 1024
_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _open_source(path: Path) -> BinaryIO:
    parts = path.parts
    fd = os.open("/" if path.is_absolute() else ".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        components = parts[1:] if path.is_absolute() else parts
        for index, part in enumerate(components):
            if part in ("", "."):
                continue
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index != len(components) - 1:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("attachment must be a regular file")
        stream = os.fdopen(fd, "rb")
        fd = -1
        return stream
    finally:
        if fd != -1:
            os.close(fd)


def _signature(kind: str, prefix: bytes) -> bool:
    if kind == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if kind == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    if kind == "image/gif":
        return prefix.startswith((b"GIF87a", b"GIF89a"))
    return len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"


def _identity(st: os.stat_result) -> tuple[int, ...]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def capture_inputs(
    manifest: Mapping[str, object],
    base_dir: Path,
    *,
    send: Callable[[str, dict[str, Any]], Any] = broker.call,
) -> dict[str, object]:
    """Replace file references with verified, broker-owned image descriptors."""
    result = dict(manifest)
    attachments = manifest.get("attachments")
    if attachments is None:
        return result
    if not isinstance(attachments, dict):
        raise ValueError("attachments must be a mapping")
    sources: list[tuple[str, Path]] = []
    for key, value in attachments.items():
        if (
            not isinstance(key, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", key) is None
            or not isinstance(value, dict)
            or set(value) != {"file"}
        ):
            raise ValueError("invalid attachment input")
        filename = value["file"]
        if not isinstance(filename, str) or not filename:
            raise ValueError("attachment file must be a path")
        path = Path(filename)
        sources.append((key, path if path.is_absolute() else base_dir / path))

    captured: dict[str, object] = {}
    with ExitStack() as stack:
        prepared = []
        for key, path in sources:
            name = path.name
            kind = _TYPES.get(path.suffix.lower())
            if (
                kind is None
                or not name
                or len(name.encode("utf-8")) > 255
                or any(c in name for c in "/\\")
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
            ):
                raise ValueError("invalid attachment filename")
            stream = stack.enter_context(_open_source(path))
            before = _identity(os.fstat(stream.fileno()))
            if not 0 < before[2] <= _MAX_SIZE:
                raise ValueError("invalid attachment size")
            digest = hashlib.sha256()
            prefix = b""
            length = 0
            while chunk := stream.read(_CHUNK):
                length += len(chunk)
                if length > _MAX_SIZE:
                    raise ValueError("attachment too large")
                prefix = (prefix + chunk[:12])[:12]
                digest.update(chunk)
            if length != before[2] or _identity(os.fstat(stream.fileno())) != before:
                raise ValueError("attachment changed during capture")
            if not _signature(kind, prefix):
                raise ValueError("attachment signature does not match filename")
            metadata = {
                "name": name,
                "content_type": kind,
                "size": length,
                "sha256": digest.hexdigest(),
            }
            prepared.append((key, stream, before, metadata))

        for key, stream, before, metadata in prepared:
            stream.seek(0)
            reply = send("outbox.attachment.begin", metadata.copy())
            if (
                not isinstance(reply, dict)
                or not isinstance(reply.get("blob"), str)
                or not reply["blob"]
            ):
                raise ValueError("invalid attachment begin response")
            blob = reply["blob"]
            digest = hashlib.sha256()
            offset = 0
            while chunk := stream.read(_CHUNK):
                digest.update(chunk)
                send(
                    "outbox.attachment.chunk",
                    {
                        "blob": blob,
                        "offset": offset,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                offset += len(chunk)
                if offset > before[2]:
                    raise ValueError("attachment changed during capture")
            if (
                offset != metadata["size"]
                or digest.hexdigest() != metadata["sha256"]
                or _identity(os.fstat(stream.fileno())) != before
            ):
                raise ValueError("attachment changed during capture")
            descriptor = send("outbox.attachment.finish", {"blob": blob})
            if descriptor != {"blob": blob, **metadata}:
                raise ValueError("attachment descriptor does not match capture")
            captured[key] = descriptor
    result["attachments"] = captured
    return result
