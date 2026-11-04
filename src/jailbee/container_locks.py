"""Stable host-side locks for per-container authority and metadata publication."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from jailbee.paths import xdg_data_home


@contextmanager
def locked(full: str) -> Iterator[None]:
    """Serialize threads and processes; never replace or unlink the lock inode."""
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
