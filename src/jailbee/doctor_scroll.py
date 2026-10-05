"""Keyboard scrolling for `jailbee doctor`'s live table.

While a slow check runs, `Live` owns the screen and the table may be taller
than the terminal. :class:`ScrollState` holds where the user has scrolled to;
:func:`scroll_keys` reads the keys that move it on a background thread, with
the terminal in cbreak mode (ISIG stays on, so Ctrl-C still reaches the
running check).
"""

from __future__ import annotations

import os
import select
import termios
import threading
import tty
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

PAGE_ROWS = 10

# Raw byte sequences a terminal sends for the keys that scroll.
_KEYS: dict[bytes, str] = {
    b"\x1b[A": "up",
    b"\x1bOA": "up",
    b"k": "up",
    b"\x1b[B": "down",
    b"\x1bOB": "down",
    b"j": "down",
    b"\x1b[5~": "page-up",
    b"\x1b[6~": "page-down",
    b"\x1b[H": "home",
    b"\x1b[1~": "home",
    b"\x1b[F": "end",
    b"\x1b[4~": "end",
    b"f": "follow",
}


def parse_scroll_key(data: bytes) -> str:
    """The scroll action for one raw stdin read ('' if it is not one)."""
    return _KEYS.get(data, "")


class ScrollState:
    """Where the user has scrolled the table to; ``None`` follows the running row."""

    def __init__(self) -> None:
        self.cursor: int | None = None

    def apply(self, key: str, *, running: int, count: int) -> None:
        """Move the cursor for ``key``; ``running`` is where a scroll starts from."""
        if key == "follow" or count <= 0:
            self.cursor = None
            return
        here = running if self.cursor is None else self.cursor
        target = {
            "up": here - 1,
            "down": here + 1,
            "page-up": here - PAGE_ROWS,
            "page-down": here + PAGE_ROWS,
            "home": 0,
            "end": count - 1,
        }.get(key)
        if target is not None:
            self.cursor = max(0, min(count - 1, target))


@contextmanager
def scroll_keys(fd: int | None, on_key: Callable[[str], None]) -> Iterator[None]:
    """Feed scroll keys read from ``fd`` to ``on_key`` until the block ends.

    Does nothing when ``fd`` is None or has no terminal attributes (a pipe, a
    file). The terminal's mode is always put back, including when the block
    raises.
    """
    saved = None
    if fd is not None:
        try:
            saved = termios.tcgetattr(fd)
        except termios.error:
            pass
    if fd is None or saved is None:
        yield
        return
    stop = threading.Event()

    def read_loop() -> None:
        while not stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready or stop.is_set():
                continue
            try:
                data = os.read(fd, 32)
            except OSError:
                return
            if not data:
                return
            key = parse_scroll_key(data)
            if key:
                on_key(key)

    tty.setcbreak(fd)
    reader = threading.Thread(target=read_loop, name="doctor-scroll-keys", daemon=True)
    reader.start()
    try:
        yield
    finally:
        stop.set()
        reader.join()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
