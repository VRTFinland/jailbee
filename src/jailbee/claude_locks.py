"""Cooperate with Claude Code's own advisory locks while swapping credentials.

Claude Code guards its OAuth token refresh with the npm ``proper-lockfile``
package, and its ``.claude.json`` writes with the same mechanism on the config
file. The protocol, and the constants below, are as documented by cswap
(claude-swap, MIT, Copyright (c) 2026 Onur Cetinkol) in its own
``claude_locks.py``, verified there against the Claude Code 2.1.218 bundle:

- The lock artifact is a **directory**; ``mkdir`` atomicity is the mutex.
- The refresh path takes two locks in order — the primary
  ``<dir>/.oauth_refresh.lock``, then the legacy sibling ``<dir>.lock`` kept
  for external tools. Both run ``stale: 60000, update: 5000``.
- The config lock keeps the older defaults: stale after 10s, touched every 5s.
- Claude Code retries a held credentials lock 5 times with 1-2s jittered
  sleeps before giving up, so briefly holding it is fully cooperative.

Holding these while swapping closes the one real race with a running Claude
Code: its refresh reads the credential, POSTs, and saves, all under both
credential locks — a swap landing inside that window would be overwritten by
the refreshed *old-account* token. Under the lock, Claude Code's own
double-checked re-read sees the swapped credential and skips the refresh.

**One jailbee-specific deviation.** cswap derives both credential locks from
``CLAUDE_CONFIG_DIR`` because it never sets ``CLAUDE_SECURESTORAGE_CONFIG_DIR``.
jailbee does set it, so the two directories differ by design and the lock
belongs with the *credential*: callers pass the holder directory, not the
config home.
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

CREDENTIALS_STALENESS_S = 60.0
"""A credential lock younger than this belongs to a live holder.

Never lower it: the holder's toucher can stall well past 10s (a suspended
laptop, a blocked event loop) while still legitimately owning the lock.
"""

CONFIG_STALENESS_S = 10.0
"""``.claude.json.lock`` keeps proper-lockfile's older default."""

TOUCH_INTERVAL_S = 3.0
"""We touch a little faster than Claude Code's 5s, for margin."""

DEFAULT_TIMEOUT_S = 9.0
"""Per-lock wait budget.

Claude Code holds the credential lock for one token-endpoint round trip
(sub-second to a few seconds) and the config lock for a local
read-modify-write. Note this is *per lock*: `credential_locks` acquires two
sequentially, so its worst case is roughly twice this.
"""


class ClaudeLockTimeoutError(RuntimeError):
    """A Claude Code advisory lock stayed held past the wait budget."""


def _age_s(path: Path) -> float | None:
    """Seconds since `path` was last touched, or None if it vanished."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _touch_until(path: Path, stop: threading.Event, interval_s: float) -> None:
    """Keep `path`'s mtime fresh so no other holder judges it stale."""
    while not stop.wait(interval_s):
        try:
            os.utime(path, None)
        except OSError:
            return  # the lock is gone; the holder is on its way out


@contextmanager
def held_lock(
    path: Path,
    *,
    stale_s: float,
    timeout_s: float,
    touch_interval_s: float = TOUCH_INTERVAL_S,
) -> Iterator[None]:
    """Hold one proper-lockfile-style lock directory.

    Waits up to `timeout_s` for a contended lock, stealing it only once it is
    older than `stale_s`. A lock we cannot take is left alone — stealing a
    fresh one would race a live Claude Code mid-refresh.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            path.mkdir(parents=True)
            break
        except FileExistsError:
            age = _age_s(path)
            if age is not None and age > stale_s:
                try:
                    path.rmdir()
                except OSError:
                    # Persistent (a non-empty lock dir, a permissions problem) or
                    # a race with another stealer — either way, fall through to
                    # the deadline check rather than spinning on it.
                    pass
                else:
                    continue  # cleared it; retry immediately
            if time.monotonic() >= deadline:
                raise ClaudeLockTimeoutError(
                    f"{path} is held by another process (waited {timeout_s:.0f}s). "
                    "A Claude Code session may be refreshing its token; retry shortly."
                ) from None
            time.sleep(random.uniform(0.02, 0.06))

    stop = threading.Event()
    toucher = threading.Thread(
        target=_touch_until, args=(path, stop, touch_interval_s), daemon=True
    )
    toucher.start()
    try:
        yield
    finally:
        stop.set()
        toucher.join(timeout=1.0)
        try:
            path.rmdir()
        except OSError:
            pass  # already stolen as stale, or removed under us


@contextmanager
def credential_locks(holder: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Iterator[None]:
    """Hold both credential locks for `holder`, primary first.

    `ExitStack` is what guarantees the primary is released when the legacy
    lock times out — leaving it behind would hold off a live Claude Code's
    refresh for a full staleness window.
    """
    with ExitStack() as stack:
        stack.enter_context(
            held_lock(
                holder / ".oauth_refresh.lock",
                stale_s=CREDENTIALS_STALENESS_S,
                timeout_s=timeout_s,
            )
        )
        stack.enter_context(
            held_lock(
                holder.parent / (holder.name + ".lock"),
                stale_s=CREDENTIALS_STALENESS_S,
                timeout_s=timeout_s,
            )
        )
        yield


@contextmanager
def config_lock(config_home: Path, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Iterator[None]:
    """Hold Claude Code's `.claude.json` write lock for one config home."""
    with held_lock(
        config_home / ".claude.json.lock",
        stale_s=CONFIG_STALENESS_S,
        timeout_s=timeout_s,
    ):
        yield
