"""Stable host-side locks for per-container authority and metadata publication."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from jailbee.paths import xdg_data_home


@contextmanager
def locked(full: str) -> Iterator[None]:
    """Serialize threads and processes; keep the empty inode even after destroy.

    A waiter may already have opened it: unlinking would let new callers lock
    a different inode for the same name. See docs/architecture.md.
    """
    if not full or Path(full).name != full or full in (".", ".."):
        raise ValueError("invalid container name")
    directory = xdg_data_home() / "jailbee" / "container-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{full}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
