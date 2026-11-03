"""Inotify watcher with a portable polling fallback."""

import ctypes
import os
import select
import time
from pathlib import Path
from typing import Literal


class InboxWatcher:
    def __init__(self, path: Path, *, poll: float, inotify: bool = True) -> None:
        self.poll = poll
        self.mode: Literal['inotify', 'poll'] = 'poll'
        self.fd = -1
        if not inotify or os.environ.get('JAILBEE_INBOX_INOTIFY') == '0':
            return
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                return
            self.fd = fd
            if libc.inotify_add_watch(fd, os.fsencode(path), 0x8 | 0x80) < 0:
                self.close()
                return
            self.mode = 'inotify'
        except (AttributeError, OSError):
            self.close()

    def wait(self, timeout: float) -> None:
        if self.mode == 'poll':
            time.sleep(min(self.poll, timeout))
            return
        if select.select([self.fd], [], [], timeout)[0]:
            while True:
                try:
                    os.read(self.fd, 65536)
                except BlockingIOError:
                    break

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
