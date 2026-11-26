"""Stdlib-only session hook decisions and JSON input."""

import json
import re
import sys
from typing import Any

_RESUME_CONTEXT = (
    "This session was resumed. Background processes from before the resume "
    "(shells, servers and watchers) are not running unless the task list shows them. "
    "The JailBee inbox follower has been restarted automatically."
)
_WAITER = re.compile(r"\b(?:jailbee|jb)\s+inbox\s+(?:follow|wait)\b")


def session_start(data: dict[str, Any], role: str) -> dict[str, Any] | None:
    if data.get("source") in ("resume", "fork") and role != "plain":
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": _RESUME_CONTEXT,
            }
        }
    return None


def stop(data: dict[str, Any], role: str) -> dict[str, Any] | None:
    tasks = data.get("background_tasks")
    if role != "child" or data.get("stop_hook_active") or not isinstance(tasks, list):
        return None
    if any(
        isinstance(task, dict)
        and task.get("status") == "running"
        and isinstance(task.get("command"), str)
        and _WAITER.search(task["command"])
        for task in tasks
    ):
        return None
    return {
        "decision": "block",
        "reason": (
            "Run jailbee inbox wait --timeout none with Bash run_in_background before stopping."
        ),
    }


def permission_request(data: dict[str, Any], *, unattended: bool) -> dict[str, Any] | None:
    """Reject unattended prompts, keeping user-entered plan approval interactive."""
    if not unattended or data.get("tool_name") == "ExitPlanMode":
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {
                "behavior": "deny",
                "message": (
                    "Permission prompts are auto-rejected in JailBee containers in unattended "
                    "mode. Find a safer form, for example avoid cd with relative rm paths. "
                    "If the action is unavoidable, ask the user in ❓ question format "
                    "instead of opening a dialog."
                ),
            },
        }
    }


def read_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read(65537)
        if len(raw) > 65536:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError, UnicodeError, RecursionError):
        return {}
