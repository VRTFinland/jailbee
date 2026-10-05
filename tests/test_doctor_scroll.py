"""Keyboard scrolling of `jailbee doctor`'s live table."""

from __future__ import annotations

import os
import pty
import termios
import time
from io import StringIO

import pytest
from rich.console import Console

from jailbee.cli import _DeferredDetail, _DoctorLiveView
from jailbee.doctor import CheckResult
from jailbee.doctor_scroll import PAGE_ROWS, ScrollState, parse_scroll_key, scroll_keys


@pytest.mark.parametrize(
    ("data", "key"),
    [
        (b"\x1b[A", "up"),
        (b"k", "up"),
        (b"\x1b[B", "down"),
        (b"j", "down"),
        (b"\x1b[5~", "page-up"),
        (b"\x1b[6~", "page-down"),
        (b"\x1b[H", "home"),
        (b"\x1b[F", "end"),
        (b"f", "follow"),
        (b"x", ""),
        (b"", ""),
    ],
)
def test_parse_scroll_key(data: bytes, key: str) -> None:
    assert parse_scroll_key(data) == key


def test_a_first_scroll_starts_from_the_running_row() -> None:
    state = ScrollState()

    state.apply("up", running=30, count=40)

    assert state.cursor == 29


def test_scrolling_is_clamped_to_the_table() -> None:
    state = ScrollState()

    state.apply("home", running=30, count=40)
    state.apply("up", running=30, count=40)
    assert state.cursor == 0

    state.apply("end", running=30, count=40)
    state.apply("page-down", running=30, count=40)
    assert state.cursor == 39

    state.apply("page-up", running=30, count=40)
    assert state.cursor == 39 - PAGE_ROWS


def test_follow_resumes_tracking_the_running_row() -> None:
    state = ScrollState()
    state.apply("home", running=30, count=40)

    state.apply("follow", running=30, count=40)

    assert state.cursor is None


def _render(rows: int, running: int, scroll: ScrollState) -> str:
    results = [CheckResult(f"check-{i}", True, "fine") for i in range(rows)]
    console = Console(force_terminal=True, width=80, height=12, file=StringIO())
    console.print(_DoctorLiveView(results, (running, _DeferredDetail()), scroll))
    return console.file.getvalue()  # type: ignore[attr-defined,no-any-return]


def test_the_view_follows_the_scroll_cursor_instead_of_the_running_row() -> None:
    scroll = ScrollState()
    scroll.apply("home", running=30, count=40)

    out = _render(rows=40, running=30, scroll=scroll)

    assert "check-0 " in out
    assert "check-30" not in out
    assert "scroll" in out
    assert len(out.splitlines()) <= 12


def test_the_view_follows_the_running_row_until_scrolled() -> None:
    out = _render(rows=40, running=30, scroll=ScrollState())

    assert "check-30" in out
    assert len(out.splitlines()) <= 12


def test_scroll_keys_is_a_no_op_without_a_terminal() -> None:
    seen: list[str] = []

    with scroll_keys(None, seen.append):
        pass

    assert seen == []


def test_scroll_keys_reads_keys_and_restores_the_terminal() -> None:
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        seen: list[str] = []

        with scroll_keys(slave, seen.append):
            assert termios.tcgetattr(slave) != before, "cbreak mode is on inside the block"
            os.write(master, b"j")
            deadline = time.monotonic() + 2
            while not seen and time.monotonic() < deadline:
                time.sleep(0.01)

        assert seen == ["down"]
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)


def test_scroll_keys_restores_the_terminal_when_the_block_raises() -> None:
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)

        with pytest.raises(KeyboardInterrupt), scroll_keys(slave, lambda _k: None):
            raise KeyboardInterrupt

        assert termios.tcgetattr(slave) == before
    finally:
        os.close(master)
        os.close(slave)
