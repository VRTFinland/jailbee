"""The dashboard's notification queue: one entry per action it started.

Frontend-agnostic and in memory only: the terminal dashboard keeps one queue
per session, and a future web frontend can hold the same model. An entry is
created when an action starts and finishes with its exit status and output;
it stays until the user dismisses it. Alongside them the queue lists the
background lifecycle jobs of the latest snapshot (:class:`JobRow`): read-only,
never dismissed, gone when the job is. Actions that were refused or never
started are normally not entries: :meth:`NotificationQueue.drop` takes such
an entry back out without a trace. Creation is the exception: its shared runner
retains pre-worker failures, including spawn errors, since no job can report them.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from jailbee import background

if TYPE_CHECKING:
    from jailbee.dashboard.model import RepoGroup
    from jailbee.prompt_channel import Prompt

from jailbee.dashboard.runner import OUTPUT_DROPPED_MARKER, OUTPUT_KEEP_CHARS

EntryState = Literal["running", "awaiting", "done", "failed"]


def is_critical(argv: Sequence[str]) -> bool:
    """Whether ``argv`` (a command without the program name) is a critical action.

    ``merge`` (also ``git merge``), ``git push --merge``, ``git pull`` and
    ``pr push``: outcomes the user must not miss, so they open as a modal and
    block terminal hand-offs until the user sends them to the background.
    The verb leads: options come after it, never before.
    """
    words = list(argv[:2])
    if words[:1] == ["merge"] or words == ["git", "merge"]:
        return True
    if words == ["git", "pull"] or words == ["pr", "push"]:
        return True
    return words == ["git", "push"] and "--merge" in argv


def should_show_question(
    *,
    critical: bool,
    foreground: bool,
    on_top: bool,
    overlay: Literal["none", "queue", "other"],
    dispatch_interaction: int | None,
    answer_interaction: int | None,
    interaction: int,
    suspended: bool,
) -> bool:
    """Whether a question may open now, without changing its queue or UI state.

    ``on_top`` reports whether this action's question surface is unobstructed
    (the owning critical output screen, or the dashboard for ordinary actions).
    Dispatch and the last answer keep attention only until the next input.
    """
    if suspended or not on_top:
        return False
    if critical and foreground:
        return True
    if foreground or overlay == "other":
        return False
    return dispatch_interaction == interaction or answer_interaction == interaction


@dataclass(frozen=True)
class QueueEntry:
    """One started action: what ran, on what, and how it went."""

    id: int
    command: str  # "jailbee merge alpha-a" — what the user would have typed
    target: str  # the container or repo it ran on
    state: EntryState = "running"
    pending_question: Prompt | None = None
    status: int | None = None  # the exit status once finished
    output: str = ""
    critical: bool = False  # a critical action (see :func:`is_critical`)
    foreground: bool = False  # a critical action the user has not sent to the background

    @property
    def finished(self) -> bool:
        return self.state not in ("running", "awaiting")

    @property
    def needs_attention(self) -> bool:
        """Failed, or blocked on a question the user has not answered."""
        return self.state in ("failed", "awaiting")


# The background jobs the queue lists: those that move a container to being
# usable, and a background destroy (a failed one stays visible); stop and
# refresh are quick or already the user's own action.
LIFECYCLE_JOB_KINDS = frozenset(
    {background.JOB_CREATE, background.JOB_BOOT, background.JOB_AUTOSTART, background.JOB_DESTROY}
)


@dataclass(frozen=True)
class JobRow:
    """One background lifecycle job from the snapshot, as the queue lists it."""

    target: str  # the container
    phase: str  # the phase label the JOB column shows, e.g. "create"
    failed: bool  # the job ended in failure or its worker died

    @property
    def running(self) -> bool:
        """A live job: its log can be followed."""
        return not self.failed


def job_rows(groups: Iterable[RepoGroup]) -> tuple[JobRow, ...]:
    """The lifecycle jobs ``groups`` show, in listing order."""
    rows: list[JobRow] = []
    for group in groups:
        for c in group.containers:
            if c.job_phase is None or c.job_kind not in LIFECYCLE_JOB_KINDS:
                continue
            # Dead = a terminal phase, or a worker that vanished mid-phase.
            if c.job_pid is None:
                failed = c.job_phase in background.TERMINAL_PHASES
            else:
                failed = background.clearable(c.job_phase, c.job_pid, live=c.job_live)
            label = background.job_label_or_empty(
                c.job_phase, c.job_pid, kind=c.job_kind, live=c.job_live
            )
            rows.append(JobRow(c.name, label or c.job_phase, failed))
    return tuple(rows)


@dataclass(frozen=True)
class QueueCounts:
    """What the badge shows."""

    running: int
    pending: int = 0
    failed: int = 0


class NotificationQueue:
    """The entries of one dashboard session, oldest first."""

    def __init__(self) -> None:
        self._entries: dict[int, QueueEntry] = {}
        self.jobs: tuple[JobRow, ...] = ()

    def job(self, target: str) -> JobRow | None:
        return next((j for j in self.jobs if j.target == target), None)

    def set_jobs(self, jobs: tuple[JobRow, ...]) -> None:
        """Replace the snapshot's job rows; a job the snapshot dropped is gone."""
        self.jobs = jobs

    @property
    def empty(self) -> bool:
        return not self._entries and not self.jobs

    def __iter__(self) -> Iterator[QueueEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, entry_id: int) -> QueueEntry | None:
        return self._entries.get(entry_id)

    def start(self, command: str, target: str, *, critical: bool = False) -> int:
        """A new running entry; returns its id. A critical one starts in the foreground."""
        entry_id = secrets.randbits(63) or 1
        while entry_id in self._entries:
            entry_id = secrets.randbits(63) or 1
        self._entries[entry_id] = QueueEntry(
            entry_id, command, target, critical=critical, foreground=critical
        )
        return entry_id

    def send_to_background(self, entry_id: int) -> None:
        """The user continued a critical entry in the background (or closed its modal)."""
        entry = self._entries.get(entry_id)
        if entry is not None and entry.foreground:
            self._entries[entry_id] = replace(entry, foreground=False)

    def foreground_entries(self) -> list[QueueEntry]:
        """The critical entries still in the foreground, oldest first."""
        return [e for e in self._entries.values() if e.foreground]

    def blocking_entry(self) -> QueueEntry | None:
        """The running critical entry that forbids terminal hand-offs, if any."""
        return next((e for e in self._entries.values() if e.foreground and not e.finished), None)

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
        self._entries[entry_id] = replace(
            entry,
            state="awaiting" if awaiting else "running",
            pending_question=entry.pending_question if awaiting else None,
        )

    def set_question(self, entry_id: int, prompt: Prompt) -> None:
        """Keep the received question until its waiting command gets an answer."""
        entry = self._entries.get(entry_id)
        if entry is not None and not entry.finished:
            self._entries[entry_id] = replace(entry, state="awaiting", pending_question=prompt)

    def finish(self, entry_id: int, status: int, output: str) -> None:
        """The action ended with exit ``status`` after printing ``output``."""
        entry = self._entries.get(entry_id)
        if entry is None:
            return
        self._entries[entry_id] = replace(
            entry,
            state="done" if status == 0 else "failed",
            status=status,
            output=output,
            pending_question=None,
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

    def attention_items(self) -> frozenset[tuple[str, str | int]]:
        """What waits on the user, by identity: failed job targets and entry ids."""
        items: set[tuple[str, str | int]] = {("job", j.target) for j in self.jobs if j.failed}
        items |= {("entry", e.id) for e in self._entries.values() if e.needs_attention}
        return frozenset(items)

    def counts(self) -> QueueCounts:
        entries = self._entries.values()
        return QueueCounts(
            running=sum(e.state == "running" for e in entries)
            + sum(not j.failed for j in self.jobs),
            pending=sum(e.state == "awaiting" for e in entries),
            failed=sum(e.state == "failed" for e in entries) + sum(j.failed for j in self.jobs),
        )
