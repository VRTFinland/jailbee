"""The Claude Code advisory-lock protocol."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from jailbee.claude_locks import (
    ClaudeLockTimeout,
    config_lock,
    credential_locks,
    held_lock,
)


def test_held_lock_creates_and_removes_the_directory(tmp_path: Path) -> None:
    lock = tmp_path / "x.lock"
    with held_lock(lock, stale_s=60.0, timeout_s=1.0):
        assert lock.is_dir()
    assert not lock.exists()


def test_held_lock_times_out_on_a_fresh_foreign_lock(tmp_path: Path) -> None:
    lock = tmp_path / "x.lock"
    lock.mkdir()
    with pytest.raises(ClaudeLockTimeout) as excinfo:
        with held_lock(lock, stale_s=60.0, timeout_s=0.1):
            pass  # pragma: no cover - the context must not be entered
    assert str(lock) in str(excinfo.value)
    # The foreign lock is left alone: we time out rather than steal it.
    assert lock.is_dir()


def test_held_lock_steals_a_stale_lock(tmp_path: Path) -> None:
    lock = tmp_path / "x.lock"
    lock.mkdir()
    old = time.time() - 120.0
    os.utime(lock, (old, old))
    with held_lock(lock, stale_s=60.0, timeout_s=0.1):
        assert lock.is_dir()
    assert not lock.exists()


def test_held_lock_touches_while_held(tmp_path: Path) -> None:
    lock = tmp_path / "x.lock"
    with held_lock(lock, stale_s=60.0, timeout_s=1.0, touch_interval_s=0.01):
        before = lock.stat().st_mtime_ns
        time.sleep(0.15)
        after = lock.stat().st_mtime_ns
    assert after > before


def test_held_lock_releases_on_exception(tmp_path: Path) -> None:
    lock = tmp_path / "x.lock"
    with pytest.raises(ValueError):
        with held_lock(lock, stale_s=60.0, timeout_s=1.0):
            raise ValueError("boom")
    assert not lock.exists()


def test_credential_locks_takes_both_in_order(tmp_path: Path) -> None:
    holder = tmp_path / "creds"
    holder.mkdir()
    with credential_locks(holder, timeout_s=1.0):
        assert (holder / ".oauth_refresh.lock").is_dir()
        assert (tmp_path / "creds.lock").is_dir()
    assert not (holder / ".oauth_refresh.lock").exists()
    assert not (tmp_path / "creds.lock").exists()


def test_credential_locks_releases_the_primary_when_the_legacy_is_held(
    tmp_path: Path,
) -> None:
    holder = tmp_path / "creds"
    holder.mkdir()
    (tmp_path / "creds.lock").mkdir()
    with pytest.raises(ClaudeLockTimeout):
        with credential_locks(holder, timeout_s=0.1):
            pass  # pragma: no cover - the context must not be entered
    # The primary must not be left behind holding off a live Claude Code.
    assert not (holder / ".oauth_refresh.lock").exists()


def test_config_lock_is_a_sibling_of_the_config_file(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    with config_lock(home, timeout_s=1.0):
        assert (home / ".claude.json.lock").is_dir()
    assert not (home / ".claude.json.lock").exists()


def test_held_lock_times_out_when_a_stale_lock_cannot_be_removed(tmp_path: Path) -> None:
    """A stale lock we cannot clear must still honour the wait budget.

    `rmdir` on a non-empty directory raises ENOTEMPTY every time, so a steal
    branch that retries unconditionally would spin here forever.
    """
    lock = tmp_path / "x.lock"
    lock.mkdir()
    (lock / "leftover").write_text("", encoding="utf-8")
    old = time.time() - 120.0
    os.utime(lock, (old, old))  # after the write: the write bumps the dir's mtime

    started = time.monotonic()
    with pytest.raises(ClaudeLockTimeout):
        with held_lock(lock, stale_s=60.0, timeout_s=0.1):
            pass  # pragma: no cover - the context must not be entered
    assert time.monotonic() - started < 5.0
