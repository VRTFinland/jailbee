"""Verify the registry mirror's nginx cache against the digests in its keys.

jailbee never imports this module at runtime: `registry_cache` reads its
*source* and runs it inside the `jailbee-registry-mirror` container as
`python3 -c <source> <mode> …`. There, root is the host user (via the mirror
profile's `raw.idmap`) and can read the files rpardini's nginx writes as
`user nginx;` with mode 0600 — which the host user, reading the bind-mount
source directly, most likely cannot. Hence the constraints: standard library
only, no jailbee imports, and nothing but JSON lines on stdout.

An entry keyed by digest (`/v2/<repo>/blobs/sha256:<hex>`, likewise
`manifests`) must hash to that digest. nginx never checks, so a corrupt body
is served to every pull until the file is removed — and removing it under a
running nginx is safe: a missing cache file is a miss, fetched again.

Output records, by `type`: `total`, `progress`, `corrupt`, `summary` — parsed
by `jailbee.registry_cache`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

DEFAULT_ROOT = "/docker_mirror_cache"

# nginx names a finished entry after the md5 of its key. With
# `use_temp_path=off` (rpardini's setting) an entry still being written sits in
# the same directory as `<md5>.<digits>` — incomplete, so it would read as
# corrupt.
_ENTRY_NAME = re.compile(r"^[0-9a-f]{32}$")
_DIGEST_KEY = re.compile(r"^/v2/.+/(?:blobs|manifests)/sha256:([0-9a-f]{64})$")
_KEY_MARKER = b"\nKEY: "
_HEADER_END = b"\r\n\r\n"
# Binary header + key + stored response header of a registry response fit in a
# few KiB; 64 KiB leaves room for an unusually long header set.
_HEAD_BYTES = 64 * 1024
_CHUNK_BYTES = 1024 * 1024
_PROGRESS_INTERVAL = 0.25
_ERROR_SAMPLES = 5


class _UnparseableError(Exception):
    """The file is not a cache entry this module understands."""


@dataclass(frozen=True)
class _Candidate:
    rel: str
    path: str
    size: int


@dataclass(frozen=True)
class _Verdict:
    outcome: str  # "ok" | "corrupt" | "no_digest" | "status"
    key: str = ""
    expected: str = ""
    actual: str = ""
    inode: int = 0
    mtime_ns: int = 0


@dataclass
class _Tally:
    ok: int = 0
    corrupt: int = 0
    purged: int = 0
    skipped_no_digest: int = 0
    skipped_status: int = 0
    skipped_temp: int = 0
    errors: int = 0
    error_samples: list[str] = field(default_factory=list)
    bytes_checked: int = 0

    def error(self, rel: str, reason: str) -> None:
        self.errors += 1
        if len(self.error_samples) < _ERROR_SAMPLES:
            self.error_samples.append(f"{rel}: {reason}")

    def summary(self) -> dict[str, object]:
        return {
            "type": "summary",
            "checked": self.ok + self.corrupt,
            "ok": self.ok,
            "corrupt": self.corrupt,
            "purged": self.purged,
            "skipped_no_digest": self.skipped_no_digest,
            "skipped_status": self.skipped_status,
            "skipped_temp": self.skipped_temp,
            "errors": self.errors,
            "error_samples": self.error_samples,
            "bytes_checked": self.bytes_checked,
        }


def _emit(record: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def _entries(path: str, *, dirs: bool) -> list[os.DirEntry[str]]:
    """Sorted subdirectories (or regular files) of ``path``; empty if unreadable."""
    try:
        with os.scandir(path) as it:
            found = [
                e
                for e in it
                if (e.is_dir(follow_symlinks=False) if dirs else e.is_file(follow_symlinks=False))
            ]
    except OSError:
        return []
    return sorted(found, key=lambda e: e.name)


def _candidates(root: str) -> list[_Candidate]:
    """Every regular file at ``root/<level 1>/<level 2>/`` (nginx ``levels=1:2``)."""
    found: list[_Candidate] = []
    for top in _entries(root, dirs=True):
        for sub in _entries(top.path, dirs=True):
            for entry in _entries(sub.path, dirs=False):
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue  # evicted between listing and stat
                rel = f"{top.name}/{sub.name}/{entry.name}"
                found.append(_Candidate(rel=rel, path=entry.path, size=size))
    return found


def _parse_head(head: bytes) -> tuple[str, int, int]:
    """Return the entry's cache key, HTTP status and body offset."""
    marker = head.find(_KEY_MARKER)
    if marker < 0:
        raise _UnparseableError("no KEY line")
    key_start = marker + len(_KEY_MARKER)
    key_end = head.find(b"\n", key_start)
    if key_end < 0:
        raise _UnparseableError("unterminated KEY line")
    header_end = head.find(_HEADER_END, key_end)
    if header_end < 0:
        raise _UnparseableError("no end of the stored response header")
    status_line = head[key_end + 1 : head.find(b"\r\n", key_end + 1)]
    parts = status_line.split()
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    key = head[key_start:key_end].decode("utf-8", "replace")
    return key, status, header_end + len(_HEADER_END)


def _inspect(path: str) -> _Verdict:
    """Classify one cache file. Raises ``OSError`` or ``_UnparseableError``."""
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        key, status, body_offset = _parse_head(fh.read(_HEAD_BYTES))
        match = _DIGEST_KEY.match(key)
        if match is None:
            return _Verdict("no_digest", key=key)
        if status != 200:
            return _Verdict("status", key=key)
        fh.seek(body_offset)
        digest = hashlib.sha256()
        while chunk := fh.read(_CHUNK_BYTES):
            digest.update(chunk)
    expected, actual = match[1], digest.hexdigest()
    return _Verdict(
        "ok" if actual == expected else "corrupt",
        key=key,
        expected=expected,
        actual=actual,
        inode=st.st_ino,
        mtime_ns=st.st_mtime_ns,
    )


def _record(tally: _Tally, cand: _Candidate, verdict: _Verdict) -> None:
    """Count one verdict; emit a ``corrupt`` record for a mismatch."""
    if verdict.outcome == "no_digest":
        tally.skipped_no_digest += 1
    elif verdict.outcome == "status":
        tally.skipped_status += 1
    elif verdict.outcome == "ok":
        tally.ok += 1
        tally.bytes_checked += cand.size
    else:
        tally.corrupt += 1
        tally.bytes_checked += cand.size
        _emit(
            {
                "type": "corrupt",
                "path": cand.rel,
                "key": verdict.key,
                "expected": verdict.expected,
                "actual": verdict.actual,
                "size": cand.size,
                "purged": False,
            }
        )


def _process(cand: _Candidate, tally: _Tally) -> None:
    if not _ENTRY_NAME.match(os.path.basename(cand.rel)):
        tally.skipped_temp += 1
        return
    try:
        verdict = _inspect(cand.path)
    except (OSError, _UnparseableError) as e:
        tally.error(cand.rel, str(e))
        return
    _record(tally, cand, verdict)


def _walk(candidates: Sequence[_Candidate], tally: _Tally) -> None:
    """Process ``candidates`` in order, emitting throttled progress."""
    _emit(
        {"type": "total", "entries": len(candidates), "bytes": sum(c.size for c in candidates)}
    )
    done_entries = done_bytes = 0
    last = time.monotonic()
    for cand in candidates:
        _process(cand, tally)
        done_entries += 1
        done_bytes += cand.size
        now = time.monotonic()
        if now - last >= _PROGRESS_INTERVAL:
            _emit({"type": "progress", "entries_done": done_entries, "bytes_done": done_bytes})
            last = now
    _emit({"type": "progress", "entries_done": done_entries, "bytes_done": done_bytes})
    _emit(tally.summary())


def scan(root: str) -> None:
    _walk(_candidates(root), _Tally())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="registry_cache_scan")
    modes = parser.add_subparsers(dest="mode", required=True)
    scan_parser = modes.add_parser("scan")
    scan_parser.add_argument("--root", default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"cache root {args.root} does not exist", file=sys.stderr)
        return 2
    try:
        scan(args.root)
    except BrokenPipeError:
        # The reader went away (Ctrl+C on the jailbee side). Point stdout at
        # /dev/null so the interpreter's own flush at exit cannot raise again.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
