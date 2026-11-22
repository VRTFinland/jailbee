"""The dashboard's notification queue: one entry per action it started.

Frontend-agnostic and in memory only: the terminal dashboard keeps one queue
per session, and a future web frontend can hold the same model. An entry is
created when an action starts and finishes with its exit status and output;
it stays until the user dismisses it. Actions that were refused or never
started are not entries: :meth:`NotificationQueue.drop` takes such an entry
back out without a trace.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Literal

from jailbee.dashboard.runner import OUTPUT_DROPPED_MARKER, OUTPUT_KEEP_CHARS

EntryState = Literal["running", "awaiting", "done", "failed"]


@dataclass(frozen=True)
class QueueEntry:
    """One started action: what ran, on what, and how it went."""

    id: int
    command: str  # "jailbee merge alpha-a" — what the user would have typed
    target: str  # the container or repo it ran on
    state: EntryState = "running"
    status: int | None = None  # the exit status once finished
    output: str = ""

    @property
    def finished(self) -> bool:
        return self.state not in ("running", "awaiting")

    @property
    def needs_attention(self) -> bool:
        """Failed, or blocked on a question the user has not answered."""
        return self.state in ("failed", "awaiting")


@dataclass(frozen=True)
class QueueCounts:
    """What the badge shows."""

    running: int
    attention: int


class NotificationQueue:
    """The entries of one dashboard session, oldest first."""

    def __init__(self) -> None:
        self._entries: dict[int, QueueEntry] = {}
        self._next_id = 1

    def __iter__(self) -> Iterator[QueueEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, entry_id: int) -> QueueEntry | None:
        return self._entries.get(entry_id)

    def start(self, command: str, target: str) -> int:
        """A new running entry; returns its id."""
        entry_id = self._next_id
        self._next_id += 1
        self._entries[entry_id] = QueueEntry(entry_id, command, target)
        return entry_id

    def append(self, entry_id: int, chunk: str) -> None:
        """Output of a running action arrived; kept to the same tail cap as a finished one."""
        entry = self._entries.get(entry_id)
        if entry is None or entry.finished:
            return
        text = entry.output + chunk
        if len(text) > OUTPUT_KEEP_CHARS + len(OUTPUT_DROPPED_MARKER):
            body = text.removeprefix(OUTPUT_DROPPED_MARKER)
            text = OUTPUT_DROPPED_MARKER + body[-OUTPUT_KEEP_CHARS:]
        self._entries[entry_id] = replace(entry, output=text)

    def set_awaiting(self, entry_id: int, awaiting: bool) -> None:
        """A prompt-channel question opened (``awaiting``) or was answered."""
        entry = self._entries.get(entry_id)
        if entry is None or entry.finished:
            return
        self._entries[entry_id] = replace(entry, state="awaiting" if awaiting else "running")

    def finish(self, entry_id: int, status: int, output: str) -> None:
        """The action ended with exit ``status`` after printing ``output``."""
        entry = self._entries.get(entry_id)
        if entry is None:
            return
        self._entries[entry_id] = replace(
            entry, state="done" if status == 0 else "failed", status=status, output=output
        )

    def drop(self, entry_id: int) -> None:
        """Forget an entry whose action never started (refused, could not start)."""
        self._entries.pop(entry_id, None)

    def dismiss(self, entry_id: int) -> bool:
        """Remove a finished entry; False (and nothing removed) for a running one."""
        entry = self._entries.get(entry_id)
        if entry is None or not entry.finished:
            return False
        del self._entries[entry_id]
        return True

    def dismiss_finished(self) -> int:
        """Remove every finished entry; returns how many went."""
        gone = [e.id for e in self._entries.values() if e.finished]
        for entry_id in gone:
            del self._entries[entry_id]
        return len(gone)

    def counts(self) -> QueueCounts:
        entries = self._entries.values()
        return QueueCounts(
            running=sum(e.state == "running" for e in entries),
            attention=sum(e.needs_attention for e in entries),
        )
