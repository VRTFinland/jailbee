"""Validate saved Claude launchers and prepare safe resume commands."""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from jailbee.accounts.adapters.claude import ClaudeAdapter


@dataclass(frozen=True)
class ResumeEntry:
    session_id: str
    cwd: str
    launcher: tuple[str, ...]
    window: str | None


_VALUE_OPTIONS = {
    "--model",
    "--system-prompt",
    "--system-prompt-file",
    "--tools",
    "--allowedTools",
    "--disallowedTools",
    "--permission-mode",
    "--settings",
    "--mcp-config",
    "-P",
    "--profile",
    "-C",
    "--context",
    "--add-dir",
    "--append-system-prompt",
    "--append-system-prompt-file",
}
_REMOVE_FLAGS = {"-r", "--resume", "-c", "--continue", "--fork-session", "-p", "--print"}


def parse_snapshot(raw: str) -> list[ResumeEntry]:
    """Parse untrusted snapshot JSON, dropping malformed session rows."""
    if len(raw) > 65536:
        return []
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError, TypeError):
        return []
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != 1
    ):
        return []
    if payload.get("source") not in ("capture", "stale"):
        return []
    rows = payload.get("sessions")
    if not isinstance(rows, list):
        return []

    entries: list[ResumeEntry] = []
    for row in rows:
        try:
            entry = _parse_entry(row)
        except (TypeError, ValueError, RecursionError):
            continue
        if entry is not None:
            entries.append(entry)
    return entries


def _parse_entry(row: Any) -> ResumeEntry | None:
    if not isinstance(row, dict) or row.get("agent") != "claude":
        return None
    session_id = row.get("session_id")
    cwd = row.get("cwd")
    launcher = row.get("launcher")
    window = row.get("window")
    if not isinstance(session_id, str) or ClaudeAdapter().resume_args(session_id) is None:
        return None
    if (
        not isinstance(cwd, str)
        or not os.path.isabs(cwd)
        or "\0" in cwd
        or "\n" in cwd
        or "\r" in cwd
    ):
        return None
    if (
        not isinstance(launcher, list)
        or not launcher
        or any(not isinstance(arg, str) or "\0" in arg for arg in launcher)
    ):
        return None
    if os.path.basename(launcher[0]) not in {"claude", "claude-jb"}:
        return None
    if window is not None and (not isinstance(window, str) or "\0" in window):
        return None
    return ResumeEntry(session_id, cwd, tuple(launcher), window)


def replay_argv(launcher: Sequence[str]) -> list[str]:
    """Remove the prior session selector and prompt while preserving options."""
    args = list(launcher)
    if not args:
        return []
    result = [args[0]]
    index = 1
    while index < len(args):
        arg = args[index]
        if arg in {"-r", "--resume", "--session-id"}:
            index += 1
            if index < len(args) and not args[index].startswith("-"):
                index += 1
            continue
        if arg == "--":
            break
        if arg.startswith("--resume=") or arg.startswith("--session-id=") or arg in _REMOVE_FLAGS:
            index += 1
            continue
        if arg in _VALUE_OPTIONS:
            result.append(arg)
            index += 1
            if index < len(args):
                result.append(args[index])
                index += 1
            continue
        if arg.startswith("-"):
            result.append(arg)
        else:
            # The CLI prompt is a trailing positional argument, never an earlier token.
            if index == len(args) - 1:
                break
            result.append(arg)
        index += 1
    return result


def resume_run(entry: ResumeEntry) -> str:
    """Build a shell-safe command to reopen the saved Claude session."""
    resume = ClaudeAdapter().resume_args(entry.session_id)
    if resume is None:
        raise ValueError("invalid Claude session id")
    return f"cd {shlex.quote(entry.cwd)} && exec {shlex.join(replay_argv(entry.launcher))} {resume}"
