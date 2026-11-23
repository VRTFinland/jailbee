"""Agent-agnostic transcript reading: the normalised model and the source protocol.

An agent writes its sessions somewhere under its config home, in its own
format. A `TranscriptSource` knows one agent's format and turns it into the
entries below, so a dashboard renders every agent's session the same way. A
new agent is a new source module that calls `register`, not a change here.

Standard library only: the terminal dashboard reads transcripts too, and must
not need the web dashboard to do so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Collection
    from datetime import datetime
    from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class UserMessage:
    """What the person typed (not tool results, not injected meta text)."""

    id: str
    """Stable across reads: the same record always gets the same id, whether it
    was read whole or incrementally."""
    timestamp: datetime | None
    text: str
    kind: Literal["user"] = "user"


@dataclass(frozen=True, kw_only=True)
class AssistantText:
    """A text block the agent answered with, and the model that wrote it."""

    id: str
    timestamp: datetime | None
    text: str
    model: str | None
    kind: Literal["assistant"] = "assistant"


@dataclass(frozen=True, kw_only=True)
class Thinking:
    """A thinking block. `text` is empty when the agent stored it redacted."""

    id: str
    timestamp: datetime | None
    text: str
    model: str | None
    kind: Literal["thinking"] = "thinking"


@dataclass(frozen=True, kw_only=True)
class ToolCall:
    """A tool the agent called. `call_id` links it to its `ToolResult`."""

    id: str
    timestamp: datetime | None
    call_id: str
    name: str
    summary: str
    """One line: the tool name and the argument that says what the call is about."""
    input: dict[str, object] = field(default_factory=dict)
    model: str | None = None
    kind: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True, kw_only=True)
class ToolResult:
    """What a tool returned, cut to the source's limit."""

    id: str
    timestamp: datetime | None
    call_id: str
    """The `ToolCall.call_id` this result answers."""
    text: str
    truncated: bool = False
    full_length: int = 0
    """Length of the untruncated text, in characters."""
    is_error: bool = False
    kind: Literal["tool_result"] = "tool_result"


@dataclass(frozen=True, kw_only=True)
class SubagentRef:
    """A subagent the session started; `agent_id` opens its transcript."""

    id: str
    timestamp: datetime | None
    call_id: str
    """The `ToolCall.call_id` that started the subagent."""
    agent_id: str
    agent_type: str | None = None
    description: str | None = None
    kind: Literal["subagent"] = "subagent"


Entry = UserMessage | AssistantText | Thinking | ToolCall | ToolResult | SubagentRef


@dataclass(frozen=True)
class ReadResult:
    """Entries read from a transcript and where the next read starts."""

    entries: tuple[Entry, ...]
    offset: int
    """Byte offset just past the last complete line consumed. A partial trailing
    line is not consumed: the next read from `offset` picks it up."""


@dataclass(frozen=True)
class SessionFiles:
    """A session's transcript and its subagents' transcripts, by agent id."""

    transcript: Path
    subagents: dict[str, Path] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionInfo:
    """One session as a list shows it."""

    session_id: str
    path: Path
    title: str
    """Derived from the first user message; empty when there is none yet."""
    started: datetime | None
    last_activity: datetime | None
    models: tuple[str, ...]
    """Every model that answered, in first-use order."""


Stamp = tuple[int, int]
"""A transcript file's ``(size, mtime_ns)``: while it is unchanged, so is its info."""


class SessionInfoStore(Protocol):
    """Where a `SessionInfoCache` keeps its entries beyond the process (a database)."""

    def load(self, path: Path) -> tuple[Stamp, SessionInfo] | None:
        """The info stored for ``path`` and the stamp it was read at, or None."""
        ...

    def save(self, path: Path, stamp: Stamp, info: SessionInfo) -> None:
        """Remember ``info`` for ``path`` at ``stamp``. Never raises."""
        ...


class SessionInfoCache:
    """Remembers `SessionInfo` by the file's path, size and mtime.

    Listing reads every transcript; a long-lived caller (the web server) passes
    one cache so only a file that grew or was rewritten is read again. With a
    `store`, what was read survives a restart: a miss in memory asks the store
    before the file is read, and every read is saved to it. Owned by the
    caller, never a module global.
    """

    def __init__(self, store: SessionInfoStore | None = None) -> None:
        self._entries: dict[Path, tuple[Stamp, SessionInfo | None]] = {}
        self._store = store

    def get(
        self, path: Path, load: Callable[[], SessionInfo | None], stamp: Stamp | None
    ) -> SessionInfo | None:
        """`load()`'s result, reused while `stamp` (size, mtime_ns) is unchanged."""
        if stamp is None:
            return load()
        hit = self._entries.get(path)
        if hit is not None and hit[0] == stamp:
            return hit[1]
        stored = self._store.load(path) if self._store is not None else None
        if stored is not None and stored[0] == stamp:
            self._entries[path] = stored
            return stored[1]
        info = load()
        self._entries[path] = (stamp, info)
        if info is not None and self._store is not None:
            self._store.save(path, stamp, info)
        return info


@runtime_checkable
class TranscriptSource(Protocol):
    """One agent's transcript format.

    Every operation degrades to "nothing" on an unexpected shape or an
    unreadable file; a malformed line is skipped, never raised.
    """

    name: str
    """The agent name this source is registered under."""

    def locate(self, config_home: Path, session_id: str) -> SessionFiles | None:
        """A session's transcript and subagent transcripts, or None."""
        ...

    def list_sessions(
        self,
        config_home: Path,
        cache: SessionInfoCache | None = None,
        cwds: Collection[str] | None = None,
    ) -> list[SessionInfo]:
        """Every session under `config_home`, newest activity first.

        With a `cache`, a transcript unchanged since the last call is not read.
        With `cwds`, only sessions started in one of those directories (or
        below one) are listed, and no other transcript is read."""
        ...

    def session_info(
        self, session_id: str, path: Path, cache: SessionInfoCache | None = None
    ) -> SessionInfo | None:
        """One session's list row, from the transcript `locate` found."""
        ...

    def subagent_transcript(self, config_home: Path, session_id: str, agent_id: str) -> Path | None:
        """The transcript a `SubagentRef.agent_id` names, or None."""
        ...

    def read(self, path: Path) -> ReadResult:
        """Every complete entry in a transcript."""
        ...

    def read_from(self, path: Path, offset: int) -> ReadResult:
        """The entries written since `offset`, and the offset to read from next."""
        ...


SOURCES: dict[str, TranscriptSource] = {}
"""Agent name → source. Populated by each source module at import time."""


def register(source: TranscriptSource, *names: str) -> None:
    """Register `source` under its own name and any `names` that share its format."""
    for name in (source.name, *names):
        SOURCES[name] = source


_MODULES = {"claude-jb": "claude"}
"""Agent names whose source lives in a module named differently."""


def get_source(name: str) -> TranscriptSource:
    """The source for agent `name`, importing the module that registers it.

    Imported here rather than at module level so `base` stays free of every
    source's code; an agent with no source raises `KeyError`.
    """
    if name not in SOURCES:
        import importlib

        module = _MODULES.get(name, name)
        try:
            importlib.import_module(f"jailbee.transcripts.{module}")
        except ModuleNotFoundError:
            pass
    return SOURCES[name]
