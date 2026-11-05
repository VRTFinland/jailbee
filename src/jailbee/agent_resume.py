"""Validate saved Claude launchers and prepare safe resume commands."""

from __future__ import annotations

import json
import logging
import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jailbee.accounts.adapters.claude import ClaudeAdapter
from jailbee.config.models_agents import AutostartStep
from jailbee.incus import IncusError

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

log = logging.getLogger(__name__)


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
    "-n",
    "--agent",
    "--agents",
    "--effort",
    "--plugin-dir",
    "--plugin-url",
    "--fallback-model",
    "--name",
    "--debug-file",
    "--input-format",
    "--json-schema",
    "--max-budget-usd",
    "--output-format",
    "--setting-sources",
    "--system-prompt-snapshot",
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
    "--allowed-tools",
    "--disallowed-tools",
}
_REMOVE_FLAGS = {"-r", "--resume", "-c", "--continue", "--fork-session", "-p", "--print"}
_VARIADIC_OPTIONS = {
    "--add-dir",
    "--allowedTools",
    "--allowed-tools",
    "--disallowedTools",
    "--disallowed-tools",
    "--betas",
    "--file",
    "--mcp-config",
    "--tools",
}


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
        if arg == "--from-pr":
            index += 1
            if index < len(args) and not args[index].startswith("-"):
                index += 1
            continue
        if arg == "--session-id":
            index += 1
            if index < len(args):
                index += 1
            continue
        if arg in {"-r", "--resume"}:
            index += 1
            if index < len(args) and not args[index].startswith("-"):
                index += 1
            continue
        if arg == "--":
            break
        if (
            arg.startswith("--resume=")
            or arg.startswith("--session-id=")
            or arg.startswith("--from-pr=")
            or arg in _REMOVE_FLAGS
        ):
            index += 1
            continue
        if arg in _VALUE_OPTIONS or arg in _VARIADIC_OPTIONS:
            result.append(arg)
            index += 1
            if arg in _VARIADIC_OPTIONS:
                while index < len(args) and not args[index].startswith("-"):
                    result.append(args[index])
                    index += 1
            elif index < len(args):
                result.append(args[index])
                index += 1
            continue
        if arg.startswith("-"):
            result.append(arg)
        # Bare positional arguments are prompts and must not be replayed.
        index += 1
    return result


def resume_run(entry: ResumeEntry) -> str:
    """Build a shell-safe command to reopen the saved Claude session."""
    resume = ClaudeAdapter().resume_args(entry.session_id)
    if resume is None:
        raise ValueError("invalid Claude session id")
    return f"cd {shlex.quote(entry.cwd)} && exec {shlex.join(replay_argv(entry.launcher))} {resume}"


def capture(cfg: Config, incus: Incus, container: str) -> None:
    """Capture live sessions before a clean shutdown, best effort."""
    try:
        incus.exec(container, ["jailbee", "_agent-snapshot", "--capture"],
                   uid=cfg.container_user.uid, timeout=15)
    except IncusError:
        log.debug("Could not capture agent sessions", exc_info=True)


def boot_entries(cfg: Config, incus: Incus, container: str) -> list[ResumeEntry]:
    """Consume the boot snapshot and discard sessions without transcripts."""
    try:
        raw = incus.exec_capped(container, ["jailbee", "_agent-snapshot", "--boot"],
                                max_bytes=65536, uid=cfg.container_user.uid, timeout=15)
    except IncusError:
        log.debug("Could not consume agent sessions", exc_info=True)
        return []
    adapter = ClaudeAdapter()
    return [entry for entry in parse_snapshot(raw)
            if adapter.session_exists(cfg, container, entry.session_id)]


def restore_steps(cfg: Config, steps: list[AutostartStep], entries: Sequence[ResumeEntry],
                  *, prompted: str | None, extra_env: Mapping[str, str]) -> list[AutostartStep]:
    """Replace matching agent steps and append other saved windows."""
    from jailbee.tmux import window_for

    result = list(steps)
    names = {step.name for step in result}
    windows = {window_for(name) for name in names}
    claimed: set[int] = set()
    prompted_window = window_for(prompted) if prompted is not None else None
    for entry in entries:
        if entry.window is not None and entry.window == prompted_window:
            continue
        match = next((i for i, step in enumerate(steps)
                      if entry.window is not None and window_for(step.name) == entry.window
                      and i not in claimed), None)
        if match is not None:
            result[match] = result[match].model_copy(update={"run": resume_run(entry)})
            claimed.add(match)
            continue
        base = entry.window or "claude"
        name = base
        suffix = 2
        while name in names or window_for(name) in windows:
            name = f"{base}-{suffix}"
            suffix += 1
        names.add(name)
        windows.add(window_for(name))
        env = dict(extra_env)
        for spec in cfg.agents.values():
            try:
                command = shlex.split(spec.command)
            except ValueError:
                continue
            if command and os.path.basename(command[0]) == os.path.basename(entry.launcher[0]):
                env.update(dict(spec.env))
                break
        result.append(AutostartStep(name=name, run=resume_run(entry), background=True,
                                    continue_on_error=True, env=env))
    return result
