"""What a Claude Code session is doing, read from its transcript.

Claude writes `<config home>/projects/<encoded cwd>/<sessionId>.jsonl`, one
JSON record per line, and a `<sessionId>/subagents/agent-*.jsonl` per subagent
next to it. Undocumented, internal format (observed on 2.1.291): every
unexpected shape degrades to "nothing", never to an exception. The files are
written from inside a container, so they are read bounded and without
following links.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jailbee.accounts.models import RECENT_EVENTS, ActivityEvent, ActivityPaths, AgentActivity

TAIL_BYTES = 64 * 1024
MESSAGE_CHARS = 200
LAST_MESSAGE_CHARS = 1000
"""The latest message's own cap: the details panel wraps it in full. History
entries keep `MESSAGE_CHARS`."""
ARG_CHARS = 80
SUBAGENT_FRESH_SECONDS = 30.0
"""A subagent file modified within this window counts as running. An estimate:
no record says a subagent finished, and one thinking for longer than this
with no output is missed."""

PROJECTS_DIRNAME = "projects"
SUBAGENTS_DIRNAME = "subagents"

_SESSION_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

_TOOL_ARGS = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Grep": "pattern",
    "Glob": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "Agent": "description",
    "Task": "description",
    "Skill": "skill",
}
"""Which input field says what a tool call is about."""


def _one_line(text: str, limit: int) -> str:
    """Whitespace collapsed, printable characters only, cut to `limit` with `…`."""
    flat = "".join(ch for ch in " ".join(text.split()) if ch.isprintable())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def read_tail(path: Path, limit: int = TAIL_BYTES) -> bytes | None:
    """The last `limit` bytes of a regular file, from a whole line on.

    None for anything but a regular file: a missing file, a symlink (not
    followed) or a FIFO (opened non-blocking, then refused), because the file
    is written from a container and a blocking open would hang the refresh.
    When the cut lands inside a line, that partial first line is dropped.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        start = max(0, info.st_size - limit)
        os.lseek(fd, start, os.SEEK_SET)
        chunks: list[bytes] = []
        size = 0
        while size < limit:
            chunk = os.read(fd, limit - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if start > 0:
        _, newline, data = data.partition(b"\n")
        if not newline:
            return b""
    return data


def _tool_text(block: dict[str, object]) -> str | None:
    name = block.get("name")
    if not isinstance(name, str):
        return None
    shown = _one_line(name, ARG_CHARS)
    if not shown:
        return None
    key = _TOOL_ARGS.get(name)
    tool_input = block.get("input")
    if key is not None and isinstance(tool_input, dict):
        argument = tool_input.get(key)
        if isinstance(argument, str):
            text = _one_line(argument, ARG_CHARS)
            if text:
                return f"{shown}  {text}"
    return shown


@dataclass(frozen=True)
class TailActivity:
    """What the tail of a transcript says the session last did."""

    last_tool: str | None = None
    last_message: str | None = None
    last_event_at: datetime | None = None
    recent: tuple[ActivityEvent, ...] = ()
    """Up to `RECENT_EVENTS` tool calls and messages, oldest first."""


def parse_tail(raw: bytes, *, now: float | None = None) -> TailActivity:
    """The latest tool call, the latest assistant text and the recent events.

    The latest tool and text may come from different records. Only
    `assistant` records supply display text; assistant text/thinking/tool calls
    and user tool results supply event timestamps. Prompts and metadata are ignored. One
    pass over the same buffer: events are collected newest first and returned
    in chronological order.
    """
    last_tool: str | None = None
    last_message: str | None = None
    newest_first: list[ActivityEvent] = []
    last_event_at: datetime | None = None
    for line in reversed(raw.splitlines()):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict) or record.get("type") not in ("assistant", "user"):
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        kinds = (
            ("text", "thinking", "tool_use") if record["type"] == "assistant" else ("tool_result",)
        )
        if any(isinstance(block, dict) and block.get("type") in kinds for block in content):
            stamp = record.get("timestamp")
            if isinstance(stamp, str):
                try:
                    date = datetime.fromisoformat(stamp)
                    if date.tzinfo is not None and (now is None or date.timestamp() <= now):
                        date = date.astimezone(UTC)
                        if last_event_at is None or date > last_event_at:
                            last_event_at = date
                except (ValueError, OverflowError, OSError):
                    pass
        if record["type"] != "assistant":
            continue
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            event: ActivityEvent
            if kind == "tool_use":
                tool = _tool_text(block)
                if tool is None:
                    continue
                if last_tool is None:
                    last_tool = tool
                event = ActivityEvent("tool", tool)
            elif kind == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    continue
                if last_message is None:
                    last_message = _one_line(text, LAST_MESSAGE_CHARS) or None
                short = _one_line(text, MESSAGE_CHARS)
                if not short:
                    continue
                event = ActivityEvent("message", short)
            else:
                continue
            if len(newest_first) < RECENT_EVENTS:
                newest_first.append(event)
    return TailActivity(last_tool, last_message, last_event_at, tuple(reversed(newest_first)))


def locate(config_home: Path, session_id: str | None) -> ActivityPaths | None:
    """Where a session's transcript and subagent files are, or None.

    `session_id` came from a file the container wrote, so it is accepted only
    as a UUID: no separator or `..` can reach the path. `projects/` is shared
    by every container of the repo, so the transcript is found by its UUID
    name in whichever project directory holds it; a symlinked project
    directory is skipped.
    """
    if session_id is None or _SESSION_ID.fullmatch(session_id) is None:
        return None
    try:
        projects = sorted((config_home / PROJECTS_DIRNAME).iterdir())
    except OSError:
        return None
    for project in projects:
        if project.is_symlink():
            continue
        transcript = project / f"{session_id}.jsonl"
        if transcript.exists():
            return ActivityPaths(transcript, project / session_id / SUBAGENTS_DIRNAME)
    return None


def count_fresh_subagents(
    directory: Path, now: float, window: float = SUBAGENT_FRESH_SECONDS
) -> int | None:
    """How many `agent-*.jsonl` files under `directory` changed within `window`.

    A missing directory is 0 (it exists only once a subagent was started);
    any other failure is None (unknown). Symlinks are not counted.
    """
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    count = 0
    for entry in entries:
        if not (entry.name.startswith("agent-") and entry.name.endswith(".jsonl")):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_mtime >= now - window:
            count += 1
    return count


def read_activity(paths: ActivityPaths, *, now: float) -> AgentActivity | None:
    """The session's activity, or None when its transcript cannot be read.

    `shells` stays None: the process tree, not the transcript, knows them.
    """
    tail = read_tail(paths.transcript)
    if tail is None:
        return None
    try:
        info = paths.transcript.lstat()
        modified = info.st_mtime if stat.S_ISREG(info.st_mode) else None
    except OSError:
        modified = None
    parsed = parse_tail(tail, now=now)
    return AgentActivity(
        last_tool=parsed.last_tool,
        last_message=parsed.last_message,
        subagents=count_fresh_subagents(paths.subagents, now),
        modified=modified,
        recent=parsed.recent,
        last_event_at=parsed.last_event_at,
    )
