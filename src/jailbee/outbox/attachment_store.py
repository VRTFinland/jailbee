"""Immutable, UUID-scoped image captures in the private broker database."""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TypedDict

from jailbee.orchestration.store import BrokerStore

MAX_SIZE = 10_000_000
MAX_CHUNK = 192 * 1024
_EXTENSIONS = {
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
    "image/webp": {".webp"},
}
MAX_AGE = 24 * 60 * 60


class AttachmentError(ValueError):
    """Invalid or inaccessible attachment transfer."""


class Descriptor(TypedDict):
    blob: str
    name: str
    content_type: str
    size: int
    sha256: str


def _signature(content_type: str, prefix: bytes) -> bool:
    if content_type == "image/png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return prefix.startswith(b"\xff\xd8\xff")
    if content_type == "image/gif":
        return prefix.startswith((b"GIF87a", b"GIF89a"))
    return len(prefix) >= 12 and prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"


class AttachmentStore(BrokerStore):
    def __init__(self, store: BrokerStore | Path) -> None:
        super().__init__(store.path if isinstance(store, BrokerStore) else store)

    def begin(
        self, uuid: str, name: str, content_type: str, size: int, sha256: str, now: float
    ) -> str:
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or len(name.encode("utf-8")) > 255
            or any(c in name for c in "/\\")
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
        ):
            raise AttachmentError("invalid display name")
        if content_type not in _EXTENSIONS:
            raise AttachmentError("unsupported image type")
        if Path(name).suffix.lower() not in _EXTENSIONS[content_type]:
            raise AttachmentError("filename does not match image type")
        if type(size) is not int or not 0 < size <= MAX_SIZE:
            raise AttachmentError("invalid image size")
        if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
            raise AttachmentError("invalid sha256")
        blob = secrets.token_urlsafe(24)
        with self.transaction() as con:
            con.execute(
                "INSERT INTO outbox_attachment "
                "(uuid,id,name,content_type,size,sha256,state,last_activity) "
                "VALUES (?,?,?,?,?,?,'pending',?)",
                (uuid, blob, name, content_type, size, sha256, now),
            )
        return blob

    def append(self, uuid: str, blob: str, offset: int, data: bytes, now: float) -> None:
        if type(data) is not bytes or not data or len(data) > MAX_CHUNK or type(offset) is not int:
            raise AttachmentError("invalid chunk")
        with self.transaction() as con:
            row = con.execute(
                "SELECT state,received,size FROM outbox_attachment WHERE uuid=? AND id=?",
                (uuid, blob),
            ).fetchone()
            if (
                row is None
                or row[0] != "pending"
                or row[1] != offset
                or offset + len(data) > row[2]
            ):
                raise AttachmentError("invalid transfer or offset")
            con.execute(
                "INSERT INTO outbox_attachment_chunk VALUES (?,?,?,?)", (uuid, blob, offset, data)
            )
            con.execute(
                "UPDATE outbox_attachment SET received=?, revision=revision+1,last_activity=? "
                "WHERE uuid=? AND id=?",
                (offset + len(data), now, uuid, blob),
            )

    def finish(self, uuid: str, blob: str, now: float) -> Descriptor:
        digest = hashlib.sha256()
        prefix = b""
        length = 0
        with self.transaction(write=False) as con:
            row = con.execute(
                "SELECT name,content_type,size,sha256,state,received,revision "
                "FROM outbox_attachment WHERE uuid=? AND id=?",
                (uuid, blob),
            ).fetchone()
            if row is not None and row[4] == "ready":
                return {
                    "blob": blob,
                    "name": row[0],
                    "content_type": row[1],
                    "size": row[2],
                    "sha256": row[3],
                }
            if row is None or row[4] != "pending" or row[5] != row[2]:
                raise AttachmentError("transfer is not complete")
            for offset, data in con.execute(
                "SELECT offset,data FROM outbox_attachment_chunk "
                "WHERE uuid=? AND id=? ORDER BY offset",
                (uuid, blob),
            ):
                if offset != length:
                    raise AttachmentError("invalid chunk order")
                length += len(data)
                prefix = (prefix + data[:12])[:12]
                digest.update(data)
        if length != row[2] or digest.hexdigest() != row[3] or not _signature(row[1], prefix):
            raise AttachmentError("image content does not match declaration")
        with self.transaction() as con:
            result = con.execute(
                "UPDATE outbox_attachment SET state='ready',revision=revision+1,last_activity=? "
                "WHERE uuid=? AND id=? AND state='pending' AND revision=?",
                (now, uuid, blob, row[6]),
            )
            if result.rowcount != 1:
                current = con.execute(
                    "SELECT state,revision FROM outbox_attachment WHERE uuid=? AND id=?",
                    (uuid, blob),
                ).fetchone()
                if current is None or current[0] != "ready" or current[1] != row[6] + 1:
                    raise AttachmentError("transfer changed during finalization")
        return {
            "blob": blob,
            "name": row[0],
            "content_type": row[1],
            "size": row[2],
            "sha256": row[3],
        }

    def metadata(self, uuid: str, blob: str) -> Descriptor:
        with self.transaction(write=False) as con:
            row = con.execute(
                "SELECT name,content_type,size,sha256 FROM outbox_attachment "
                "WHERE uuid=? AND id=? AND state='ready'",
                (uuid, blob),
            ).fetchone()
        if row is None:
            raise AttachmentError("attachment not found")
        return {
            "blob": blob,
            "name": row[0],
            "content_type": row[1],
            "size": row[2],
            "sha256": row[3],
        }

    def iter_bytes(self, uuid: str, blob: str) -> Iterator[bytes]:
        with self.transaction(write=False) as con:
            if (
                con.execute(
                    "SELECT 1 FROM outbox_attachment WHERE uuid=? AND id=? AND state='ready'",
                    (uuid, blob),
                ).fetchone()
                is None
            ):
                raise AttachmentError("attachment not found")
            for (data,) in con.execute(
                "SELECT data FROM outbox_attachment_chunk WHERE uuid=? AND id=? ORDER BY offset",
                (uuid, blob),
            ):
                yield bytes(data)

    @staticmethod
    def pin(
        con: sqlite3.Connection,
        uuid: str,
        kind: str,
        proposal: str,
        blob: str,
        expected: Mapping[str, object] | None = None,
    ) -> Descriptor:
        row = con.execute(
            "SELECT name,content_type,size,sha256 FROM outbox_attachment "
            "WHERE uuid=? AND id=? AND state='ready'",
            (uuid, blob),
        ).fetchone()
        if row is None:
            raise AttachmentError("attachment not found")
        descriptor: Descriptor = {
            "blob": blob,
            "name": row[0],
            "content_type": row[1],
            "size": row[2],
            "sha256": row[3],
        }
        if expected is not None and expected != descriptor:
            raise AttachmentError("attachment metadata does not match")
        con.execute(
            "INSERT OR IGNORE INTO outbox_attachment_ref VALUES (?,?,?,?)",
            (uuid, kind, proposal, blob),
        )
        return descriptor

    def unpin(self, con: sqlite3.Connection, uuid: str, kind: str, proposal: str) -> None:
        con.execute(
            "DELETE FROM outbox_attachment_ref WHERE uuid=? AND kind=? AND proposal=?",
            (uuid, kind, proposal),
        )

    def collect(self, now: float, older_than: float = MAX_AGE) -> int:
        """Delete expired unreferenced captures and their chunks atomically."""
        if older_than < 0:
            raise AttachmentError("invalid retention period")
        with self.transaction() as con:
            condition = (
                "last_activity < ? AND NOT EXISTS "
                "(SELECT 1 FROM outbox_attachment_ref r WHERE "
                "r.uuid=outbox_attachment.uuid AND r.id=outbox_attachment.id)"
            )
            con.execute(
                "DELETE FROM outbox_attachment_chunk WHERE (uuid,id) IN "
                f"(SELECT uuid,id FROM outbox_attachment WHERE {condition})",
                (now - older_than,),
            )
            result = con.execute(
                f"DELETE FROM outbox_attachment WHERE {condition}", (now - older_than,)
            )
            return result.rowcount
