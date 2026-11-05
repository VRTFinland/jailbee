"""Stable per-repository exclusion for removal and egress refresh."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from jailbee.db import state_dir


@contextmanager
def locked(prefix: str, *, blocking: bool = True) -> Iterator[bool]:
    """Yield whether acquired; never unlink an inode another caller may hold."""
    if not prefix or Path(prefix).name != prefix or prefix in (".", ".."):
        raise ValueError("invalid repository prefix")
    directory = state_dir() / "repo-locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{prefix}.lock").open("a") as lock:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(lock, flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
