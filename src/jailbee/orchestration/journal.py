"""Append-only journal of processed manifests, so a manifest is never applied twice."""

from __future__ import annotations

import hashlib
import json
import os
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


class Journal:
    """JSON-lines journal of `(container key, digest)` pairs already processed.

    The container key is the Incus instance's `volatile.uuid` (the name when
    absent, see `actions.Node.key`), so a container recreated under a reused
    name starts with a clean slate. Entries written before the key existed
    fall back to `container_full`.
    """

    def __init__(self, root: Path) -> None:
        self._path = root / JOURNAL_FILE
        self._seen: set[tuple[str, str]] | None = None

    def _load(self) -> set[tuple[str, str]]:
        if self._seen is None:
            seen: set[tuple[str, str]] = set()
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
            for line in lines:
                try:
                    entry = json.loads(line)
                    key = entry.get("container_key") or entry["container_full"]
                    seen.add((key, entry["digest"]))
                except (ValueError, KeyError, TypeError):
                    continue
            self._seen = seen
        return self._seen

    def seen(self, container_key: str, digest: str) -> bool:
        """Return whether this manifest content was already processed for the container."""
        return (container_key, digest) in self._load()

    def record(
        self,
        container_key: str,
        digest: str,
        manifest_id: str,
        outcome: str,
        at: datetime,
        *,
        container_full: str | None = None,
    ) -> None:
        """Append one entry and fsync it; `container_full` is for people reading it."""
        seen = self._load()
        line = json.dumps(
            {
                "container_key": container_key,
                "container_full": container_full or container_key,
                "digest": digest,
                "manifest_id": manifest_id,
                "outcome": outcome,
                "at": at.isoformat(),
            }
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A crash may have left a truncated last line; start on a fresh line.
        prefix = ""
        try:
            with self._path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                if fh.tell() > 0:
                    fh.seek(-1, os.SEEK_END)
                    if fh.read(1) != b"\n":
                        prefix = "\n"
        except FileNotFoundError:
            pass
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        seen.add((container_key, digest))
