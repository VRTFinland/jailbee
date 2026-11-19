"""Durable, claim-bound publication of captured images."""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from jailbee import attachment_github, attachments
from jailbee.attachment_manifest import Attachment
from jailbee.orchestration.store import BrokerStore
from jailbee.outbox.attachment_store import AttachmentError as StoreAttachmentError
from jailbee.outbox.attachment_store import AttachmentStore
from jailbee.outbox.publication_db import Claim, PublicationStore, _stored_digest
from jailbee.outbox_io import JournalError


class AttachmentPublicationError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


@dataclass(frozen=True)
class UploadOutcome:
    attachment: Attachment
    host: str
    repository_id: int
    state: str
    url: str | None


class AttachmentPublisher:
    def __init__(self, store: BrokerStore, publications: PublicationStore) -> None:
        self.store = store
        self.publications = publications
        self.attachments = AttachmentStore(store)

    def _check(self, con: sqlite3.Connection, claim: Claim, digest: str) -> None:
        self.publications._check(con, claim)
        self.publications._required(con, claim, digest)
        if _stored_digest(con, claim.uuid, claim.kind, claim.name) != digest:
            raise JournalError(f"{claim.name}: stored proposal changed")

    def outcomes(self, claim: Claim, digest: str) -> tuple[UploadOutcome, ...]:
        with self.store.transaction(write=False) as con:
            self._check(con, claim, digest)
            rows = con.execute(
                "SELECT blob,name,content_type,size,sha256,host,repository_id,state,url "
                "FROM outbox_attachment_upload WHERE uuid=? AND kind=? AND proposal_name=? "
                "AND digest=? ORDER BY blob,host,repository_id",
                (claim.uuid, claim.kind, claim.name, digest),
            ).fetchall()
        return tuple(UploadOutcome(Attachment(*row[:5]), *row[5:]) for row in rows)

    def resolve_body(
        self,
        claim: Claim,
        digest: str,
        body: str,
        declared: Mapping[str, Attachment],
        repo_root: Path,
        repo: str,
    ) -> str:
        refs = attachments.references(body)
        with self.store.transaction(write=False) as con:
            self._check(con, claim, digest)
            unresolved = con.execute(
                "SELECT 1 FROM outbox_attachment_upload WHERE uuid=? AND kind=? "
                "AND proposal_name=? AND digest=? AND state IN ('prepared','uncertain') LIMIT 1",
                (claim.uuid, claim.kind, claim.name, digest),
            ).fetchone()
            if unresolved:
                raise AttachmentPublicationError(
                    "Previous image upload may have succeeded; reconcile it before retrying",
                    uncertain=True,
                )
        if not refs:
            return body
        identifiers = list(dict.fromkeys(ref.id for ref in refs))
        if any(identifier not in declared for identifier in identifiers):
            raise AttachmentPublicationError("Missing attachment declaration")
        try:
            target = attachment_github.resolve_target(repo_root, repo)
        except (attachment_github.UploadError, ValueError) as exc:
            raise AttachmentPublicationError(str(exc)) from exc
        urls = {}
        for identifier in identifiers:
            item = declared[identifier]
            urls[identifier] = self._upload(claim, digest, item, repo_root, target)
        return attachments.rewrite(body, urls)

    def _upload(
        self,
        claim: Claim,
        digest: str,
        item: Attachment,
        root: Path,
        target: attachment_github.UploadTarget,
    ) -> str:
        key = (
            claim.uuid,
            claim.kind,
            claim.name,
            digest,
            item.blob,
            target.host,
            target.repository_id,
        )
        with self.store.transaction(write=False) as con:
            self._check(con, claim, digest)
            row = con.execute(
                "SELECT name,content_type,size,sha256,state,url FROM outbox_attachment_upload "
                "WHERE uuid=? AND kind=? AND proposal_name=? AND digest=? AND blob=? "
                "AND host=? AND repository_id=?",
                key,
            ).fetchone()
        if row is not None:
            if tuple(row[:4]) != (item.name, item.content_type, item.size, item.sha256):
                raise AttachmentPublicationError("Attachment metadata differs from recorded upload")
            if row[4] == "applied" and row[5]:
                return str(row[5])
            if row[4] != "rejected":
                raise AttachmentPublicationError(
                    "Previous image upload may have succeeded; "
                    "inspect remote state before retrying",
                    uncertain=True,
                )
        with self._materialize(claim, item) as path:
            with self.store.transaction() as con:
                self._check(con, claim, digest)
                con.execute(
                    "INSERT INTO outbox_attachment_upload VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,'prepared',NULL) "
                    "ON CONFLICT(uuid,kind,proposal_name,digest,blob,host,repository_id) "
                    "DO UPDATE SET state='prepared',url=NULL WHERE state='rejected'",
                    (*key, item.name, item.content_type, item.size, item.sha256),
                )
            try:
                url = attachment_github.upload_to_target(
                    root, target, path, item.name, item.content_type
                )
                attachments.rewrite("[x](attachment:x)", {"x": url})
            except attachment_github.UploadRejectedError as exc:
                self._record(claim, digest, key, "rejected", None)
                raise AttachmentPublicationError(str(exc)) from exc
            except Exception as exc:
                self._record(claim, digest, key, "uncertain", None)
                raise AttachmentPublicationError(
                    "Image upload outcome is uncertain; inspect remote state before retrying",
                    uncertain=True,
                ) from exc
            try:
                self._record(claim, digest, key, "applied", url)
            except Exception as exc:
                raise AttachmentPublicationError(
                    "Image uploaded but its result could not be recorded; "
                    "reconcile before retrying",
                    uncertain=True,
                ) from exc
            return url

    @contextlib.contextmanager
    def _materialize(self, claim: Claim, item: Attachment) -> Iterator[Path]:
        directory: tempfile.TemporaryDirectory[str] | None = None
        try:
            if self.attachments.metadata(claim.uuid, item.blob) != item.to_dict():
                raise AttachmentPublicationError("Attachment metadata differs from stored image")
            directory = tempfile.TemporaryDirectory(prefix="jailbee-upload-")
            path = Path(directory.name) / "image"
            checksum = hashlib.sha256()
            size = 0
            with path.open("xb") as stream:
                for chunk in self.attachments.iter_bytes(claim.uuid, item.blob):
                    size += len(chunk)
                    if size > item.size:
                        raise AttachmentPublicationError(
                            "Attachment size does not match descriptor"
                        )
                    checksum.update(chunk)
                    stream.write(chunk)
                if size != item.size or checksum.hexdigest() != item.sha256:
                    raise AttachmentPublicationError("Attachment content does not match descriptor")
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, StoreAttachmentError) as exc:
            if directory is not None:
                with contextlib.suppress(OSError):
                    directory.cleanup()
            raise AttachmentPublicationError(f"Cannot prepare image upload: {exc}") from exc
        except BaseException:
            if directory is not None:
                with contextlib.suppress(OSError):
                    directory.cleanup()
            raise
        try:
            yield path
        finally:
            with contextlib.suppress(OSError):
                directory.cleanup()

    def reconcile(
        self,
        claim: Claim,
        digest: str,
        blob: str,
        host: str,
        repository_id: int,
        *,
        url: str | None = None,
        authorize_duplicate: bool = False,
    ) -> None:
        if (url is None) == (not authorize_duplicate):
            raise ValueError("Provide a canonical URL or explicitly authorize a duplicate")
        if url is not None:
            attachments.rewrite("[x](attachment:x)", {"x": url})
        with self.store.transaction() as con:
            self._check(con, claim, digest)
            result = con.execute(
                "UPDATE outbox_attachment_upload SET state=?,url=? WHERE uuid=? AND kind=? "
                "AND proposal_name=? AND digest=? AND blob=? AND host=? AND repository_id=? "
                "AND state IN ('prepared','uncertain')",
                (
                    "applied" if url else "rejected",
                    url,
                    claim.uuid,
                    claim.kind,
                    claim.name,
                    digest,
                    blob,
                    host,
                    repository_id,
                ),
            )
            if result.rowcount != 1:
                raise AttachmentPublicationError("No unresolved image upload matches this identity")

    def _record(
        self, claim: Claim, digest: str, key: tuple[str | int, ...], state: str, url: str | None
    ) -> None:
        with self.store.transaction() as con:
            self._check(con, claim, digest)
            con.execute(
                "UPDATE outbox_attachment_upload SET state=?,url=? WHERE uuid=? AND kind=? "
                "AND proposal_name=? AND digest=? AND blob=? AND host=? AND repository_id=? "
                "AND state='prepared'",
                (state, url, *key),
            )
