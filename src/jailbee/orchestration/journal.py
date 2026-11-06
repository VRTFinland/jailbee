"""Append-only journal of processed manifests, so a manifest is never applied twice."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3

from jailbee.orchestration.store import StoreError, open_state_store
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

JOURNAL_FILE = "journal.jsonl"


def manifest_digest(file_name: str, text: str, files: Mapping[str, str]) -> str:
    """Return a sha256 hex digest over the manifest name, text and referenced bodies."""
    h = hashlib.sha256()
    parts = [file_name, text]
    for name in sorted(files):
        parts.extend([name, files[name]])
    for part in parts:
        data = part.encode("utf-8")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


_log = logging.getLogger(__name__)


class _RetryImport(Exception):
    pass


class Journal:
    """Durable processed manifests, keyed by container identity and digest."""

    def __init__(self, root: Path) -> None:
        self._path = root / JOURNAL_FILE
        self._store = open_state_store(root)

    def _import(self) -> None:
        path = self._path
        corrupt = False

        def load(con: sqlite3.Connection) -> None:
            nonlocal corrupt
            try:
                text = path.read_bytes()
            except FileNotFoundError:
                return
            except OSError as exc:
                _log.warning("cannot read journal %s: %s", path, exc)
                raise _RetryImport from exc
            valid = 0
            bad = 0
            for number, line in enumerate(text.splitlines(), 1):
                try:
                    entry = json.loads(line.decode("utf-8"))
                    if not isinstance(entry, dict):
                        raise ValueError("entry is not an object")
                    key = entry.get("container_key") or entry["container_full"]
                    digest = entry["digest"]
                    if not isinstance(key, str) or not isinstance(digest, str):
                        raise ValueError("invalid key or digest")
                    def field(name: str, default: str = "") -> str:
                        value = entry.get(name)
                        return default if value is None else str(value)

                    values = (key, digest, field("container_full", key) or key,
                              field("manifest_id"), field("outcome"), field("at"))
                    con.execute("INSERT OR IGNORE INTO journal VALUES (?, ?, ?, ?, ?, ?)", values)
                    valid += 1
                except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                    bad += 1
                    _log.warning("malformed journal %s line %d: %s", path, number, exc)
            corrupt = bool(bad and not valid)
            if corrupt:
                try:
                    path.replace(path.with_name(path.name + ".corrupt"))
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    _log.warning("cannot quarantine journal %s: %s", path, exc)
                    raise _RetryImport from exc

        try:
            self._store.import_once(JOURNAL_FILE, load)
        except _RetryImport as exc:
            raise StoreError(f"cannot import journal {path}") from exc
        # TODO(state-db-cleanup): remove migrated files after the compatibility window.
        if not corrupt:
            try:
                path.replace(path.with_name(path.name + ".migrated"))
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.warning("cannot rename migrated journal %s: %s", path, exc)

    def seen(self, container_key: str, digest: str) -> bool:
        """Return whether this manifest content was already processed."""
        self._import()
        return self._store.journal_seen(container_key, digest)

    def record(
        self, container_key: str, digest: str, manifest_id: str, outcome: str,
        at: datetime, *, container_full: str | None = None,
    ) -> None:
        """Commit the entry before returning to the caller."""
        self._import()
        self._store.journal_record(
            container_key, digest, container_full or container_key, manifest_id,
            outcome, at.isoformat(),
        )
