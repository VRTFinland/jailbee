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
import stat
from pathlib import Path

TAIL_BYTES = 64 * 1024
MESSAGE_CHARS = 200
ARG_CHARS = 80
SUBAGENT_FRESH_SECONDS = 30.0
"""A subagent file modified within this window counts as running. An estimate:
no record says a subagent finished, and one thinking for longer than this
with no output is missed."""

PROJECTS_DIRNAME = "projects"
SUBAGENTS_DIRNAME = "subagents"

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


def parse_tail(raw: bytes) -> tuple[str | None, str | None]:
    """`(last tool call, last assistant text)` from the tail of a transcript.

    Each is the latest of its kind and may come from different records. Only
    `assistant` records are read; everything else in the file is ignored.
    """
    last_tool: str | None = None
    last_message: str | None = None
    for line in reversed(raw.splitlines()):
        if last_tool is not None and last_message is not None:
            break
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use" and last_tool is None:
                last_tool = _tool_text(block)
            elif kind == "text" and last_message is None:
                text = block.get("text")
                if isinstance(text, str):
                    last_message = _one_line(text, MESSAGE_CHARS) or None
    return last_tool, last_message
