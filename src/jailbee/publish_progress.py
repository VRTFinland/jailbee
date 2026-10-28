"""Progress reporting for outbox publication."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from io import TextIOBase
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

PROGRESS_ENV = "JAILBEE_PROGRESS_LINES"
PROGRESS_PREFIX = "jailbee-progress: "


def progress_line(n: int, total: int) -> str:
    return f"{PROGRESS_PREFIX}{n}/{total}"


def parse_progress(line: str) -> tuple[int, int] | None:
    match = re.fullmatch(re.escape(PROGRESS_PREFIX) + r"([0-9]+)/([0-9]+)\s*", line)
    return (int(match[1]), int(match[2])) if match else None


@contextmanager
def reporting(
    total: int, *, env: Mapping[str, str] | None = None, console: Console | None = None
) -> Iterator[Callable[[], None]]:
    if total == 0:
        yield lambda: None
        return
    n = 0
    if PROGRESS_ENV in (os.environ if env is None else env):

        def line_tick() -> None:
            nonlocal n
            n += 1
            print(progress_line(n, total), file=sys.stderr, flush=True)

        yield line_tick
        return

    if console is None:
        from rich.console import Console

        console = Console(file=sys.stderr)
    if not console.is_terminal:
        yield lambda: None
        return
    from rich.file_proxy import FileProxy

    # Rich consoles unwrap FileProxy, bypassing the live display. Hide that
    # attribute so independent consoles write through the active live console.
    class ConsoleProxy(TextIOBase):
        def __init__(self, proxy: FileProxy) -> None:
            self.proxy = proxy

        def write(self, text: str) -> int:
            return self.proxy.write(text)

        def flush(self) -> None:
            self.proxy.flush()

        def isatty(self) -> bool:
            return self.proxy.isatty()

        def fileno(self) -> int:
            return self.proxy.fileno()

    with console.status(f"Publishing 0/{total}…") as status:
        stdout, stderr = sys.stdout, sys.stderr
        if isinstance(stdout, FileProxy):
            sys.stdout = ConsoleProxy(stdout)
        if isinstance(stderr, FileProxy):
            sys.stderr = ConsoleProxy(stderr)
        try:

            def status_tick() -> None:
                nonlocal n
                n += 1
                status.update(f"Publishing {n}/{total}…", spinner="dots")

            yield status_tick
        finally:
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            finally:
                sys.stdout, sys.stderr = stdout, stderr
