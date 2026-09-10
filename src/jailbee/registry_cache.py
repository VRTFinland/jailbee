"""Registry mirror cache integrity: find, and on request remove, cache entries
whose content does not match the digest they are stored under.

The walking and hashing run inside the mirror container — see
`registry_cache_scan`, whose source this module ships there — and this side
turns its JSON-line output into a `CacheReport`. Callers make sure the mirror
is running first (`registry.registry_status`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any

from jailbee.maintenance import humanize
from jailbee.registry import MIRROR_CONTAINER_NAME

if TYPE_CHECKING:
    from jailbee.incus import Incus

_SCAN_MODULE = "registry_cache_scan.py"
_KEY = re.compile(r"^/v2/(?P<repo>.+)/(?P<kind>blobs|manifests)/sha256:")


@dataclass(frozen=True)
class CacheProgress:
    entries_done: int
    entries_total: int
    bytes_done: int
    bytes_total: int


@dataclass(frozen=True)
class CorruptEntry:
    path: str
    """Relative to the cache root — what `purge_entries` takes back."""
    key: str
    expected: str
    actual: str
    size: int
    purged: bool

    @property
    def repo(self) -> str:
        """The image repository the entry belongs to, e.g. ``gisgro/typster``.

        The registry host is not part of nginx's cache key, so it cannot be
        shown.
        """
        m = _KEY.match(self.key)
        return m["repo"] if m else self.key

    @property
    def kind(self) -> str:
        m = _KEY.match(self.key)
        return "manifest" if m is not None and m["kind"] == "manifests" else "blob"


@dataclass(frozen=True)
class CacheReport:
    corrupt: tuple[CorruptEntry, ...]
    checked: int
    ok: int
    purged: int
    skipped_no_digest: int
    skipped_status: int
    skipped_temp: int
    errors: int
    error_samples: tuple[str, ...]
    bytes_checked: int


def _no_progress(_progress: CacheProgress) -> None:
    """Default ``on_progress``: report nowhere."""


def format_progress(progress: CacheProgress) -> str:
    """``812/1352 entries · 11.2 GB of 18.0 GB``."""
    return (
        f"{progress.entries_done}/{progress.entries_total} entries · "
        f"{humanize(progress.bytes_done)} of {humanize(progress.bytes_total)}"
    )


def verify_cache(
    incus: Incus,
    *,
    purge: bool = False,
    on_progress: Callable[[CacheProgress], None] = _no_progress,
) -> CacheReport:
    """Hash every digest-keyed entry of the mirror's cache against its key.

    ``purge`` removes corrupt entries during the same pass (an entry nginx
    replaced after it was hashed is left alone). Takes minutes on a large
    cache; ``on_progress`` hears about it several times a second.
    """
    return _run_scan(incus, ["scan", "--purge"] if purge else ["scan"], on_progress)


def purge_entries(incus: Incus, paths: Sequence[str]) -> CacheReport:
    """Remove ``paths`` (``CorruptEntry.path`` values) that are still corrupt.

    Each is re-verified first: the scan that found it may be minutes old, and a
    pull in between may have replaced it with a sound copy.
    """
    return _run_scan(incus, ["purge", *paths], _no_progress)


def _mismatch(line: str, reason: object) -> RuntimeError:
    return RuntimeError(
        f"unexpected output from the cache scan ({reason}): {line[:200]!r} — "
        "the scan module and jailbee disagree on its format"
    )


def _run_scan(
    incus: Incus, args: list[str], on_progress: Callable[[CacheProgress], None]
) -> CacheReport:
    source = resources.files("jailbee").joinpath(_SCAN_MODULE).read_text()
    totals = (0, 0)
    corrupt: list[CorruptEntry] = []
    summary: dict[str, Any] | None = None
    # closing(): an exception out of this loop (a format mismatch, a failing
    # on_progress) must stop the exec now, not whenever the traceback holding
    # the suspended generator is collected — the scan may be a purging one.
    command = ["python3", "-c", source, *args]
    with closing(incus.exec_lines(MIRROR_CONTAINER_NAME, command)) as lines:
        for line in lines:
            if not line.strip():
                continue
            progress: CacheProgress | None = None
            try:
                record = json.loads(line)
                kind = record["type"]
                if kind == "total":
                    totals = (int(record["entries"]), int(record["bytes"]))
                elif kind == "progress":
                    progress = CacheProgress(
                        int(record["entries_done"]), totals[0], int(record["bytes_done"]), totals[1]
                    )
                elif kind == "corrupt":
                    corrupt.append(
                        CorruptEntry(
                            path=str(record["path"]),
                            key=str(record["key"]),
                            expected=str(record["expected"]),
                            actual=str(record["actual"]),
                            size=int(record["size"]),
                            purged=bool(record["purged"]),
                        )
                    )
                elif kind == "summary":
                    summary = record
                else:
                    raise ValueError(f"unknown record type {kind!r}")
            except (ValueError, KeyError, TypeError) as e:
                raise _mismatch(line, e) from e
            if progress is not None:
                on_progress(progress)
    if summary is None:
        raise RuntimeError("the cache scan ended without a summary")
    try:
        return CacheReport(
            corrupt=tuple(corrupt),
            checked=int(summary["checked"]),
            ok=int(summary["ok"]),
            purged=int(summary["purged"]),
            skipped_no_digest=int(summary["skipped_no_digest"]),
            skipped_status=int(summary["skipped_status"]),
            skipped_temp=int(summary["skipped_temp"]),
            errors=int(summary["errors"]),
            error_samples=tuple(str(s) for s in summary["error_samples"]),
            bytes_checked=int(summary["bytes_checked"]),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise _mismatch(json.dumps(summary), e) from e
