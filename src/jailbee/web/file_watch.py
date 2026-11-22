"""Watch one file for changes: inotify where the kernel has it, polling otherwise.

Standard library only: inotify is reached through `ctypes` on libc. A watch
runs one daemon thread that calls ``on_change`` (from that thread) whenever
the file may have changed; the caller reads to find out what. `close` stops
the thread and frees the descriptors. The web server opens a watch only
while a browser follows the transcript (`jailbee.web.transcript_follow`).
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
import select
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_MOVE_SELF = 0x00000800
IN_DELETE_SELF = 0x00000400
IN_NONBLOCK = os.O_NONBLOCK
IN_CLOEXEC = os.O_CLOEXEC

_MASK = IN_MODIFY | IN_CLOSE_WRITE | IN_ATTRIB | IN_MOVE_SELF | IN_DELETE_SELF

DEFAULT_POLL_SECONDS = 1.0


class FileWatch(Protocol):
    """A running watch; `close` is idempotent and stops further callbacks."""

    def close(self) -> None: ...


WatchFactory = Callable[[Path, Callable[[], None]], FileWatch]
"""Starts a watch on a path; the callback may be called from any thread."""


class InotifyWatch:
    """One inotify instance with one watch, read by a daemon thread.

    A self-pipe wakes the thread on `close`, so closing never waits on a
    timeout. Raises `OSError` when inotify is unavailable or the path cannot
    be watched (the caller falls back to polling).
    """

    def __init__(self, path: Path, on_change: Callable[[], None]) -> None:
        libc = _libc()
        fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            raise _errno_error("inotify_init1", path)
        if libc.inotify_add_watch(fd, os.fsencode(path), _MASK) < 0:
            error = _errno_error("inotify_add_watch", path)
            os.close(fd)
            raise error
        self._fd = fd
        self._wake_r, self._wake_w = os.pipe()
        self._on_change = on_change
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"watch {path.name}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        os.write(self._wake_w, b"x")
        if self._thread is not threading.current_thread():
            self._thread.join()
        for fd in (self._fd, self._wake_r, self._wake_w):
            with contextlib.suppress(OSError):
                os.close(fd)

    def _run(self) -> None:
        while not self._closed.is_set():
            ready, _, _ = select.select([self._fd, self._wake_r], [], [])
            if self._closed.is_set():
                return
            if self._fd in ready:
                self._drain()
                self._on_change()

    def _drain(self) -> None:
        with contextlib.suppress(BlockingIOError, OSError):
            while os.read(self._fd, 4096):
                pass


class PollingWatch:
    """Compares the file's (size, mtime) every ``interval`` seconds."""

    def __init__(
        self, path: Path, on_change: Callable[[], None], interval: float = DEFAULT_POLL_SECONDS
    ) -> None:
        self._path = path
        self._on_change = on_change
        self._interval = interval
        self._stop = threading.Event()
        self._last = self._stamp()
        self._thread = threading.Thread(target=self._run, name=f"poll {path.name}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not threading.current_thread():
            self._thread.join()

    def _stamp(self) -> tuple[int, int] | None:
        try:
            st = self._path.stat()
        except OSError:
            return None
        return st.st_size, st.st_mtime_ns

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            stamp = self._stamp()
            if stamp != self._last:
                self._last = stamp
                self._on_change()


def watch_file(path: Path, on_change: Callable[[], None]) -> FileWatch:
    """The default `WatchFactory`: inotify, or polling where it fails."""
    try:
        return InotifyWatch(path, on_change)
    except OSError:
        return PollingWatch(path, on_change)


def _libc() -> ctypes.CDLL:
    name = ctypes.util.find_library("c")
    try:
        libc = ctypes.CDLL(name, use_errno=True)
        libc.inotify_init1.argtypes = [ctypes.c_int]
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        libc.inotify_add_watch.restype = ctypes.c_int
    except (OSError, AttributeError) as exc:  # no libc, or one without inotify (macOS)
        raise OSError(f"inotify unavailable: {exc}") from exc
    return libc


def _errno_error(call: str, path: Path) -> OSError:
    errno = ctypes.get_errno()
    return OSError(errno, f"{call}: {os.strerror(errno)}", str(path))
