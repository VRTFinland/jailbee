"""`jailbee.web.file_watch`: a change to the watched file calls back; a closed watch is quiet.

Against a real file in ``tmp_path``: inotify is a kernel facility, not a
subprocess, and the polling fallback runs with a short interval.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from jailbee.web.file_watch import FileWatch, InotifyWatch, PollingWatch, watch_file
from tests.web_fakes import wait_until


class Changes:
    def __init__(self) -> None:
        self.count = 0
        self._lock = threading.Lock()

    def __call__(self) -> None:
        with self._lock:
            self.count += 1


def polling(path: Path, on_change: Callable[[], None]) -> FileWatch:
    return PollingWatch(path, on_change, interval=0.01)


WATCHERS: list[Callable[[Path, Callable[[], None]], FileWatch]] = [polling]
if sys.platform.startswith("linux"):
    WATCHERS.append(InotifyWatch)


@pytest.mark.parametrize("make", WATCHERS)
def test_an_append_is_reported_and_close_stops_it(
    tmp_path: Path, make: Callable[[Path, Callable[[], None]], FileWatch]
) -> None:
    path = tmp_path / "t.jsonl"
    path.write_text("one\n")
    changes = Changes()
    watch = make(path, changes)
    try:
        with path.open("a") as f:
            f.write("two\n")
        wait_until(lambda: changes.count > 0)
    finally:
        watch.close()
    seen = changes.count
    with path.open("a") as f:
        f.write("three\n")
    watch.close()  # idempotent
    threading.Event().wait(0.1)
    assert changes.count == seen


def test_a_missing_file_falls_back_to_polling(tmp_path: Path) -> None:
    watch = watch_file(tmp_path / "absent.jsonl", Changes())
    try:
        assert isinstance(watch, PollingWatch)
    finally:
        watch.close()
