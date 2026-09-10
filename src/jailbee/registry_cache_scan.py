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
    outcome: str  # "ok" | "corrupt" | "no_digest" | "status" (not a plain 200)
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


def _subdirs(path: str, rel: str, tally: _Tally) -> list[os.DirEntry[str]]:
    """Sorted subdirectories of ``path``. An unreadable one is an error, not an
    empty directory: "none corrupt" must never mean "could not look"."""
    try:
        with os.scandir(path) as it:
            found = [e for e in it if e.is_dir(follow_symlinks=False)]
    except OSError as e:
        tally.error(rel, f"cannot list: {e.strerror}")
        return []
    return sorted(found, key=lambda e: e.name)


def _candidates(root: str, tally: _Tally) -> list[_Candidate]:
    """Every regular file at ``root/<level 1>/<level 2>/`` (nginx ``levels=1:2``)."""
    found: list[_Candidate] = []
    for top in _subdirs(root, ".", tally):
        for sub in _subdirs(top.path, top.name, tally):
            rel_dir = f"{top.name}/{sub.name}"
            try:
                with os.scandir(sub.path) as it:
                    files = sorted(
                        (e for e in it if e.is_file(follow_symlinks=False)),
                        key=lambda e: e.name,
                    )
            except OSError as e:
                tally.error(rel_dir, f"cannot list: {e.strerror}")
                continue
            for entry in files:
                rel = f"{rel_dir}/{entry.name}"
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except FileNotFoundError:
                    continue  # evicted between listing and stat
                except OSError as e:
                    tally.error(rel, str(e))
                    continue
                found.append(_Candidate(rel=rel, path=entry.path, size=size))
    return found


def _parse_head(head: bytes) -> tuple[str, int, bool, int]:
    """Return the entry's cache key, HTTP status, whether the stored body is
    content-encoded, and the body offset."""
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
    status_line, *header_lines = head[key_end + 1 : header_end].split(b"\r\n")
    parts = status_line.split()
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    encoded = False
    for line in header_lines:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-encoding":
            encoded = value.strip().lower() not in (b"", b"identity")
    key = head[key_start:key_end].decode("utf-8", "replace")
    return key, status, encoded, header_end + len(_HEADER_END)


def _inspect(path: str) -> _Verdict:
    """Classify one cache file. Raises ``OSError`` or ``_UnparseableError``."""
    with open(path, "rb") as fh:
        st = os.fstat(fh.fileno())
        key, status, encoded, body_offset = _parse_head(fh.read(_HEAD_BYTES))
        match = _DIGEST_KEY.match(key)
        if match is None:
            return _Verdict("no_digest", key=key)
        # Only a plain 200 body is the content the digest names: a 206 is part
        # of it, and a Content-Encoding (the client's Accept-Encoding reaches
        # the upstream through nginx) stores it compressed.
        if status != 200 or encoded:
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


def _unlink_if_unchanged(path: str, verdict: _Verdict) -> bool:
    """Remove a corrupt entry unless nginx replaced it after it was hashed.

    nginx swaps a new entry in by rename, so a different inode (or a touched
    mtime) means the file on disk is no longer the one found corrupt. The
    window between this stat and the unlink is microseconds; a copy landing
    inside it is lost and simply fetched again on the next pull.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False
    if (st.st_ino, st.st_mtime_ns) != (verdict.inode, verdict.mtime_ns):
        return False
    os.unlink(path)
    return True


def _record(tally: _Tally, cand: _Candidate, verdict: _Verdict, *, remove: bool) -> None:
    """Count one verdict; for a mismatch, remove it if asked and emit ``corrupt``."""
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
        purged = False
        if remove:
            try:
                purged = _unlink_if_unchanged(cand.path, verdict)
            except OSError as e:
                tally.error(cand.rel, f"could not remove: {e}")
        tally.purged += int(purged)
        _emit(
            {
                "type": "corrupt",
                "path": cand.rel,
                "key": verdict.key,
                "expected": verdict.expected,
                "actual": verdict.actual,
                "size": cand.size,
                "purged": purged,
            }
        )


def _process(cand: _Candidate, tally: _Tally, *, remove: bool) -> None:
    if not _ENTRY_NAME.match(os.path.basename(cand.rel)):
        tally.skipped_temp += 1
        return
    try:
        verdict = _inspect(cand.path)
    except FileNotFoundError:
        return  # evicted by nginx's cache manager mid-scan: no longer in the cache
    except (OSError, _UnparseableError) as e:
        tally.error(cand.rel, str(e))
        return
    _record(tally, cand, verdict, remove=remove)


def _walk(candidates: Sequence[_Candidate], tally: _Tally, *, remove: bool) -> None:
    """Process ``candidates`` in order, emitting throttled progress."""
    _emit(
        {"type": "total", "entries": len(candidates), "bytes": sum(c.size for c in candidates)}
    )
    done_entries = done_bytes = 0
    last = time.monotonic()
    for cand in candidates:
        _process(cand, tally, remove=remove)
        done_entries += 1
        done_bytes += cand.size
        now = time.monotonic()
        if now - last >= _PROGRESS_INTERVAL:
            _emit({"type": "progress", "entries_done": done_entries, "bytes_done": done_bytes})
            last = now
    _emit({"type": "progress", "entries_done": done_entries, "bytes_done": done_bytes})
    _emit(tally.summary())


def scan(root: str, *, purge: bool) -> None:
    tally = _Tally()
    _walk(_candidates(root, tally), tally, remove=purge)


def purge_paths(root: str, rels: Sequence[str]) -> None:
    """Re-verify ``rels`` (relative to ``root``) and remove those still corrupt.

    Used after the user confirmed removal of what a scan found: that can be
    minutes later, so each entry is checked again rather than trusted.
    """
    tally = _Tally()
    candidates: list[_Candidate] = []
    for rel in rels:
        norm = os.path.normpath(rel)
        if os.path.isabs(norm) or norm == ".." or norm.startswith("../"):
            tally.error(rel, "outside the cache")
            continue
        path = os.path.join(root, norm)
        try:
            size = os.stat(path).st_size
        except FileNotFoundError:
            continue  # already gone — which is what was asked for
        except OSError as e:
            tally.error(rel, str(e))
            continue
        candidates.append(_Candidate(rel=norm, path=path, size=size))
    _walk(candidates, tally, remove=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="registry_cache_scan")
    modes = parser.add_subparsers(dest="mode", required=True)
    scan_parser = modes.add_parser("scan")
    scan_parser.add_argument("--root", default=DEFAULT_ROOT)
    scan_parser.add_argument("--purge", action="store_true")
    purge_parser = modes.add_parser("purge")
    purge_parser.add_argument("--root", default=DEFAULT_ROOT)
    purge_parser.add_argument("paths", nargs="+")
    args = parser.parse_args(argv)
    try:
        os.listdir(args.root)
    except OSError as e:
        print(f"cannot read the cache root {args.root}: {e.strerror}", file=sys.stderr)
        return 2
    try:
        if args.mode == "scan":
            scan(args.root, purge=args.purge)
        else:
            purge_paths(args.root, args.paths)
    except BrokenPipeError:
        # The reader went away (Ctrl+C on the jailbee side). Point stdout at
        # /dev/null so the interpreter's own flush at exit cannot raise again.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
