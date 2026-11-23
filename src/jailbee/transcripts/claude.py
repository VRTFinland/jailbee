"""Claude Code transcripts (`claude` and `claude-jb`).

Paths come from the agent-activity code (`claude_activity.locate`): a session
is `<config home>/projects/<encoded cwd>/<sessionId>.jsonl` with its
subagents in `<sessionId>/subagents/agent-<agentId>.jsonl`. `claude-jb` runs
the same Claude Code against the LiteLLM gateway, so it writes the same files
and differs only by the model names in them.

Undocumented, internal format: every unexpected shape degrades to "nothing".
The files are written from inside a container, so they are opened without
following links and only when they are regular files.
"""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from jailbee.accounts.adapters.claude_activity import (
    PROJECTS_DIRNAME,
    SESSION_ID,
    locate,
    one_line,
    tool_summary,
)
from jailbee.transcripts.base import (
    AssistantText,
    Entry,
    ReadResult,
    SessionFiles,
    SessionInfo,
    SubagentRef,
    Thinking,
    ToolCall,
    ToolResult,
    UserMessage,
    register,
)
from jailbee.transcripts.markup import command_name, prompt_text

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.transcripts.base import SessionInfoCache

RESULT_CHARS = 4000
"""A tool result is cut to this many characters; the entry says it was cut."""

SYNTHETIC_MODEL = "<synthetic>"
"""What Claude writes as the model of a message no model wrote (an API error)."""

TITLE_CHARS = 120
"""A session title is the first typed prompt (or command) on one line, cut to this."""

TITLE_CANDIDATES = 32
"""Once a command fallback exists, inspect at most this many user candidates."""

_TIMESTAMP = re.compile(rb'"timestamp"\s*:\s*"([^"]{1,64})"')
"""Found without parsing the line: a listing reads every line of every session."""
_USER_RECORD = (b'"type":"user"', b'"type": "user"')
_MODEL = re.compile(rb'"model"\s*:\s*"([^"]{1,200})"')
"""Candidate model names in a line; only a parse says which one is the record's."""

AGENT_ID = re.compile(r"[0-9A-Za-z]{1,64}")
"""A subagent id as Claude writes it (`a` + hex). It becomes part of a file
name, so nothing else is accepted."""


def _read_bytes(path: Path, offset: int) -> tuple[int, bytes] | None:
    """Where the read started and the bytes of a regular file from there on.

    None when `path` is not a regular file. An `offset` past the end means the
    file was replaced by a shorter one, so it is read from the start.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if offset > info.st_size:
            offset = 0
        os.lseek(fd, offset, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    return offset, b"".join(chunks)


def _is_regular(path: Path) -> bool:
    """A regular file, not reached through a symlink."""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _subagents_dir(config_home: Path, session_id: str) -> Path | None:
    """The session's subagent directory, unless a link sits on the way to it."""
    paths = locate(config_home, session_id)
    if paths is None:
        return None
    directory = paths.subagents
    if directory.parent.is_symlink() or directory.is_symlink():
        return None
    return directory


def _model_name(value: object) -> str | None:
    """A model name, or None for none and for Claude's own `<synthetic>` messages."""
    if not isinstance(value, str) or not value or value == SYNTHETIC_MODEL:
        return None
    return value


def _timestamp(record: dict[str, object]) -> datetime | None:
    value = record.get("timestamp")
    return _parse_time(value) if isinstance(value, str) else None


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _result_text(content: object) -> str:
    """A tool result's content as text: a string, or its text blocks joined."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "image":
            parts.append("[image]")
    return "\n".join(parts)


def _tool_call(
    block: dict[str, object], entry_id: str, when: datetime | None, model: str | None
) -> ToolCall | None:
    name = block.get("name")
    call_id = block.get("id")
    summary = tool_summary(block)
    if not isinstance(name, str) or not isinstance(call_id, str) or summary is None:
        return None
    tool_input = block.get("input")
    return ToolCall(
        id=entry_id,
        timestamp=when,
        call_id=call_id,
        name=name,
        summary=summary,
        input=dict(tool_input) if isinstance(tool_input, dict) else {},
        model=model,
    )


def _tool_result(
    block: dict[str, object], record: dict[str, object], entry_id: str, when: datetime | None
) -> list[Entry]:
    call_id = block.get("tool_use_id")
    if not isinstance(call_id, str):
        return []
    text = _result_text(block.get("content"))
    found: list[Entry] = [
        ToolResult(
            id=entry_id,
            timestamp=when,
            call_id=call_id,
            text=text[:RESULT_CHARS],
            truncated=len(text) > RESULT_CHARS,
            full_length=len(text),
            is_error=block.get("is_error") is True,
        )
    ]
    outcome = record.get("toolUseResult")
    if isinstance(outcome, dict):
        agent_id = outcome.get("agentId")
        if isinstance(agent_id, str) and AGENT_ID.fullmatch(agent_id):
            agent_type = outcome.get("agentType")
            description = outcome.get("description")
            found.append(
                SubagentRef(
                    id=f"{entry_id}:agent",
                    timestamp=when,
                    call_id=call_id,
                    agent_id=agent_id,
                    agent_type=agent_type if isinstance(agent_type, str) else None,
                    description=description if isinstance(description, str) else None,
                )
            )
    return found


_INTERRUPTED = "[Request interrupted by user"


def _is_notice(text: str) -> bool:
    """Claude Code's own note in the person's turn, such as an interruption."""
    return text.startswith(_INTERRUPTED)


def _entries(record: dict[str, object], record_id: str) -> list[Entry]:
    """The normalised entries of one JSONL record."""
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    kind = record.get("type")
    when = _timestamp(record)
    content = message.get("content")
    found: list[Entry] = []
    if kind == "user":
        if record.get("isMeta") is True:
            return []
        meta = record.get("isCompactSummary") is True
        if isinstance(content, str):
            return (
                [
                    UserMessage(
                        id=f"{record_id}:0",
                        timestamp=when,
                        text=content,
                        meta=meta or _is_notice(content),
                    )
                ]
                if content
                else []
            )
        if not isinstance(content, list):
            return []
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            entry_id = f"{record_id}:{index}"
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str) and block["text"]:
                found.append(
                    UserMessage(
                        id=entry_id,
                        timestamp=when,
                        text=block["text"],
                        meta=meta or _is_notice(block["text"]),
                    )
                )
            elif block_type == "tool_result":
                found.extend(_tool_result(block, record, entry_id, when))
    elif kind == "assistant":
        model = _model_name(message.get("model"))
        if not isinstance(content, list):
            return []
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            entry_id = f"{record_id}:{index}"
            text = block.get("text")
            block_type = block.get("type")
            if block_type == "text" and isinstance(text, str) and text:
                found.append(AssistantText(id=entry_id, timestamp=when, text=text, model=model))
            elif block_type == "thinking":
                thought = block.get("thinking")
                found.append(
                    Thinking(
                        id=entry_id,
                        timestamp=when,
                        text=thought if isinstance(thought, str) else "",
                        model=model,
                    )
                )
            elif block_type == "tool_use":
                call = _tool_call(block, entry_id, when, model)
                if call is not None:
                    found.append(call)
    return found


def _parse(data: bytes, base_offset: int) -> list[Entry]:
    """Entries of the complete lines in `data`, which starts at `base_offset`."""
    found: list[Entry] = []
    position = base_offset
    for line in data.splitlines(keepends=True):
        start = position
        position += len(line)
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        uuid = record.get("uuid")
        record_id = uuid if isinstance(uuid, str) and uuid else f"@{start}"
        found.extend(_entries(record, record_id))
    return found


def _time_at(data: bytes, at: int) -> datetime | None:
    match = _TIMESTAMP.match(data, at) if at >= 0 else None
    if match is None:
        return None
    return _parse_time(match.group(1).decode("utf-8", "replace"))


def _line_around(data: bytes, at: int) -> bytes:
    return data[data.rfind(b"\n", 0, at) + 1 : (data.find(b"\n", at) + 1 or len(data) + 1) - 1]


def _record(line: bytes) -> dict[str, object] | None:
    try:
        record = json.loads(line)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _record_model(record: dict[str, object]) -> str | None:
    message = record.get("message")
    if record.get("type") != "assistant" or not isinstance(message, dict):
        return None
    return _model_name(message.get("model"))


def session_info(session_id: str, path: Path) -> SessionInfo | None:
    """A session's list metadata.

    Sessions run to tens of megabytes and a listing reads every one, so the
    bytes are searched rather than parsed line by line: the first and last
    timestamps bound the session, only candidate user lines are parsed until
    the first typed prompt (falling back to the first command after at most
    TITLE_CANDIDATES candidates once that command is found), and only a
    line naming a model not seen yet is parsed to tell whether that
    is the record's model or a tool input's. A caller listing many sessions
    should cache the result by the file's size and mtime.
    """
    read = _read_bytes(path, 0)
    if read is None:
        return None
    data = read[1]
    title = ""
    first_command = ""
    at = 0
    candidates = 0
    while not title and at < len(data):
        end = data.find(b"\n", at)
        end = len(data) if end < 0 else end
        line = data[at:end]
        at = end + 1
        if any(marker in line for marker in _USER_RECORD):
            if first_command and candidates >= TITLE_CANDIDATES:
                break
            candidates += 1
            record = _record(line)
            users = (
                [e for e in _entries(record, "") if isinstance(e, UserMessage)] if record else []
            )
            for user in users:
                if user.meta:
                    continue
                if not first_command:
                    first_command = command_name(user.text)
                typed = prompt_text(user.text)
                if typed:
                    title = one_line(typed[: TITLE_CHARS * 8], TITLE_CHARS)
                    break
    models: dict[str, None] = {}
    for match in _MODEL.finditer(data):
        if match.group(1).decode("utf-8", "replace") in models:
            continue
        record = _record(_line_around(data, match.start()))
        model = _record_model(record) if record is not None else None
        if model is not None:
            models.setdefault(model, None)
    return SessionInfo(
        session_id=session_id,
        path=path,
        title=title or one_line(first_command[: TITLE_CHARS * 8], TITLE_CHARS),
        started=_time_at(data, data.find(b'"timestamp"')),
        last_activity=_time_at(data, data.rfind(b'"timestamp"')),
        models=tuple(models),
    )


def _cached_info(session_id: str, path: Path, cache: SessionInfoCache | None) -> SessionInfo | None:
    if cache is None:
        return session_info(session_id, path)
    try:
        st = path.stat()
    except OSError:
        return None
    return cache.get(path, lambda: session_info(session_id, path), (st.st_size, st.st_mtime_ns))


def _session_files(config_home: Path) -> list[tuple[str, Path]]:
    """Every `<sessionId>.jsonl` directly in a project directory; no links followed."""
    try:
        projects = sorted((config_home / PROJECTS_DIRNAME).iterdir())
    except OSError:
        return []
    found: list[tuple[str, Path]] = []
    for project in projects:
        if project.is_symlink():
            continue
        try:
            names = sorted(os.listdir(project))
        except OSError:
            continue
        for name in names:
            session_id = name.removesuffix(".jsonl")
            if name.endswith(".jsonl") and SESSION_ID.fullmatch(session_id):
                if _is_regular(project / name):
                    found.append((session_id, project / name))
    return found


class ClaudeSource:
    """`TranscriptSource` for Claude Code."""

    name = "claude"

    def locate(self, config_home: Path, session_id: str) -> SessionFiles | None:
        paths = locate(config_home, session_id)
        if paths is None:
            return None
        subagents: dict[str, Path] = {}
        directory = _subagents_dir(config_home, session_id)
        try:
            names = sorted(os.listdir(directory)) if directory is not None else []
        except OSError:
            names = []
        for name in names:
            agent_id = name.removeprefix("agent-").removesuffix(".jsonl")
            if f"agent-{agent_id}.jsonl" == name and AGENT_ID.fullmatch(agent_id):
                candidate = paths.subagents / name
                if _is_regular(candidate):
                    subagents[agent_id] = candidate
        return SessionFiles(paths.transcript, subagents)

    def list_sessions(
        self, config_home: Path, cache: SessionInfoCache | None = None
    ) -> list[SessionInfo]:
        sessions = [
            info
            for session_id, path in _session_files(config_home)
            if (info := _cached_info(session_id, path, cache)) is not None
        ]
        oldest = datetime.min.replace(tzinfo=UTC)
        return sorted(sessions, key=lambda s: s.last_activity or oldest, reverse=True)

    def session_info(
        self, session_id: str, path: Path, cache: SessionInfoCache | None = None
    ) -> SessionInfo | None:
        return _cached_info(session_id, path, cache)

    def subagent_transcript(self, config_home: Path, session_id: str, agent_id: str) -> Path | None:
        if AGENT_ID.fullmatch(agent_id) is None:
            return None
        directory = _subagents_dir(config_home, session_id)
        if directory is None:
            return None
        candidate = directory / f"agent-{agent_id}.jsonl"
        return candidate if _is_regular(candidate) else None

    def read(self, path: Path) -> ReadResult:
        return self.read_from(path, 0)

    def read_from(self, path: Path, offset: int) -> ReadResult:
        read = _read_bytes(path, offset)
        if read is None:
            return ReadResult((), offset)
        offset, data = read
        complete = data[: data.rfind(b"\n") + 1]
        return ReadResult(tuple(_parse(complete, offset)), offset + len(complete))


register(ClaudeSource(), "claude-jb")
