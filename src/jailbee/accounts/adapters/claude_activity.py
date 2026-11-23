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

from jailbee.accounts.models import (
    RECENT_EVENTS,
    ActivityEvent,
    ActivityPaths,
    AgentActivity,
    AgentApiError,
)

TAIL_BYTES = 64 * 1024
MESSAGE_CHARS = 200
LAST_MESSAGE_CHARS = 3000
"""The latest message's own cap: the details panel renders it in full, as
Markdown, so its line breaks are kept. History entries keep `MESSAGE_CHARS`
and one line."""
ARG_CHARS = 80
SUBAGENT_FRESH_SECONDS = 30.0
"""A subagent file modified within this window counts as running. An estimate:
no record says a subagent finished, and one thinking for longer than this
with no output is missed."""

PROJECTS_DIRNAME = "projects"
SUBAGENTS_DIRNAME = "subagents"

SESSION_ID = re.compile(
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


def one_line(text: str, limit: int) -> str:
    """Whitespace collapsed, printable characters only, cut to `limit` with `…`."""
    flat = "".join(ch for ch in " ".join(text.split()) if ch.isprintable())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _lines(text: str, limit: int) -> str:
    """`text` with its line breaks, printable characters only, cut to `limit` with `…`.

    Trailing whitespace goes from each line, runs of blank lines become one and
    the text is stripped. Tabs become spaces: Markdown indents with either.
    """
    kept: list[str] = []
    for raw in text.replace("\t", "    ").splitlines():
        line = "".join(ch for ch in raw if ch.isprintable()).rstrip()
        if line or (kept and kept[-1]):
            kept.append(line)
    flat = "\n".join(kept).strip()
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


def tool_summary(block: dict[str, object]) -> str | None:
    name = block.get("name")
    if not isinstance(name, str):
        return None
    shown = one_line(name, ARG_CHARS)
    if not shown:
        return None
    key = _TOOL_ARGS.get(name)
    tool_input = block.get("input")
    if key is not None and isinstance(tool_input, dict):
        argument = tool_input.get(key)
        if isinstance(argument, str):
            text = one_line(argument, ARG_CHARS)
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
    api_error: AgentApiError | None = None
    """The API error the latest turn ended in, unless real progress followed it."""


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
    api_error: AgentApiError | None = None
    settled = False  # The newest error record or real-model reply has been seen.
    for line in reversed(raw.splitlines()):
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict) or record.get("type") not in ("assistant", "user"):
            continue
        failed = record["type"] == "assistant" and record.get("isApiErrorMessage") is True
        if not settled:
            if failed:
                api_error = classify_api_error(record, now=now)
                settled = True
            elif _real_reply(record):
                settled = True
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        kinds = (
            ("text", "thinking", "tool_use") if record["type"] == "assistant" else ("tool_result",)
        )
        date: datetime | None = None
        stamp = record.get("timestamp")
        if isinstance(stamp, str):
            try:
                parsed = datetime.fromisoformat(stamp)
                if parsed.tzinfo is not None and (now is None or parsed.timestamp() <= now):
                    date = parsed.astimezone(UTC)
            except (ValueError, OverflowError, OSError):
                pass
        if (
            not failed
            and date is not None
            and any(
                isinstance(block, dict) and block.get("type") in kinds for block in content
            )
        ):
            if last_event_at is None or date > last_event_at:
                last_event_at = date
        if record["type"] != "assistant":
            continue
        for block in reversed(content):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            event: ActivityEvent
            if kind == "tool_use":
                tool = tool_summary(block)
                if tool is None:
                    continue
                if last_tool is None:
                    last_tool = tool
                event = ActivityEvent("tool", tool, date)
            elif kind == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    continue
                if last_message is None:
                    last_message = _lines(text, LAST_MESSAGE_CHARS) or None
                short = one_line(text, MESSAGE_CHARS)
                if not short:
                    continue
                event = ActivityEvent("message", short, date)
            else:
                continue
            if len(newest_first) < RECENT_EVENTS:
                newest_first.append(event)
    return TailActivity(
        last_tool, last_message, last_event_at, tuple(reversed(newest_first)), api_error
    )


SYNTHETIC_MODEL = "<synthetic>"
"""The model name Claude Code writes on records it made up itself (errors, interrupts)."""
API_ERROR_DETAIL_CHARS = 160
_API_ERROR_SCAN_CHARS = 4096
"""Only the head of an error text is searched: the body is untrusted and unbounded."""
_QUOTA_MARKERS = ("usage_limit_reached", "usage limit", "insufficient_quota", "quota")
_RESETS_AT = re.compile(r'"resets_at"\s*:\s*(\d{1,12})\b')
_EMBEDDED_MESSAGE = re.compile(r'"message"\s*:\s*"([^"\\]{1,300})"')
_RESET_WINDOW_SECONDS = 400 * 86400
"""A reset further than this from now is not believed."""


def _real_reply(record: dict[str, object]) -> bool:
    """An assistant record a model actually produced: progress after an error."""
    if record.get("type") != "assistant" or record.get("isApiErrorMessage") is True:
        return False
    message = record.get("message")
    if not isinstance(message, dict) or message.get("model") == SYNTHETIC_MODEL:
        return False
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") in ("text", "thinking", "tool_use")
        for block in content
    )


def _record_time(record: dict[str, object], now: float | None) -> datetime | None:
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
        if parsed.tzinfo is None or (now is not None and parsed.timestamp() > now):
            return None
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _error_text(record: dict[str, object]) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "\n".join(parts)[:_API_ERROR_SCAN_CHARS]


def _resets_at(text: str, now: float | None) -> datetime | None:
    match = _RESETS_AT.search(text)
    if match is None:
        return None
    value = int(match[1])
    reference = now if now is not None else datetime.now(UTC).timestamp()
    if abs(value - reference) > _RESET_WINDOW_SECONDS:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _error_detail(text: str) -> str:
    """One short line about the error, never the raw response body."""
    embedded = _EMBEDDED_MESSAGE.search(text)
    if embedded is not None:
        summary = embedded[1]
    else:
        summary = next(iter(text.split("{", 1)[0].splitlines()), "")
        summary = summary.removeprefix("API Error:").strip(" ·-:")
    return one_line(summary, API_ERROR_DETAIL_CHARS)


def classify_api_error(record: dict[str, object], *, now: float | None = None) -> AgentApiError:
    """What kind of API error a synthetic `isApiErrorMessage` record reports.

    Tolerant of absent or mistyped fields: an unknown shape is `other`.
    """
    text = _error_text(record)
    raw_status = record.get("apiErrorStatus")
    status = raw_status if type(raw_status) is int and 100 <= raw_status <= 599 else None
    names = " ".join(
        value.lower()
        for value in (record.get("error"), record.get("apiErrorType"))
        if isinstance(value, str)
    )[:200]
    lowered = text.lower()
    resets_at: datetime | None = None
    if any(marker in lowered or marker in names for marker in _QUOTA_MARKERS):
        kind = "quota"
        resets_at = _resets_at(text, now)
    elif status == 429 or "rate_limit" in names:
        kind = "rate_limit"
    elif status in (401, 403) or "authentication" in names or "permission" in names:
        kind = "auth"
    elif status in (503, 529) or "overloaded" in names:
        kind = "overloaded"
    else:
        kind = "other"
    at = _record_time(record, now)
    uuid = record.get("uuid")
    ident = one_line(uuid, 64) if isinstance(uuid, str) else ""
    return AgentApiError(
        kind=kind,
        at=at,
        status=status,
        detail=_error_detail(text),
        resets_at=resets_at,
        key=f"{at.isoformat() if at is not None else ''}|{ident}",
    )


def locate(config_home: Path, session_id: str | None) -> ActivityPaths | None:
    """Where a session's transcript and subagent files are, or None.

    `session_id` came from a file the container wrote, so it is accepted only
    as a UUID: no separator or `..` can reach the path. `projects/` is shared
    by every container of the repo, so the transcript is found by its UUID
    name in whichever project directory holds it; a symlinked project
    directory is skipped.
    """
    if session_id is None or SESSION_ID.fullmatch(session_id) is None:
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


def _background_clear(directory: Path) -> bool | None:
    """A stale transcript does not prove completion; only no history is clear."""
    try:
        with os.scandir(directory) as entries:
            return not any(
                e.name.startswith("agent-") and e.name.endswith(".jsonl") for e in entries
            )
    except FileNotFoundError:
        return True
    except OSError:
        return None


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
        background_clear=_background_clear(paths.subagents),
        modified=modified,
        recent=parsed.recent,
        last_event_at=parsed.last_event_at,
        api_error=parsed.api_error,
    )
