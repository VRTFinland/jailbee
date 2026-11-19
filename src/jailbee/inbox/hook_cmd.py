"""Claude session hooks, independent of the broker connection."""

import json
import re
import sys
from typing import Any

import typer

from jailbee.inbox.role import local_role


_RESUME_CONTEXT = (
    "This session was resumed. Background processes from before the resume "
    "(shells, servers and watchers) are not running unless the task list shows them. "
    "The JailBee inbox follower has been restarted automatically."
)
_WAITER = re.compile(r"\b(?:jailbee|jb)\s+inbox\s+(?:follow|wait)\b")


def register_hooks(inbox_app: typer.Typer) -> None:
    hooks = typer.Typer(help="Handle session lifecycle hooks.")
    inbox_app.add_typer(hooks, name="hook")

    @hooks.command("session-start")
    def session_start() -> None:
        data = _input()
        if data.get("source") in ("resume", "fork") and local_role() != "plain":
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": _RESUME_CONTEXT,
                        }
                    }
                )
            )

    @hooks.command("stop")
    def stop() -> None:
        data = _input()
        tasks = data.get("background_tasks")
        if local_role() != "child" or data.get("stop_hook_active") or not isinstance(tasks, list):
            return
        if any(
            isinstance(task, dict)
            and task.get("status") == "running"
            and isinstance(task.get("command"), str)
            and _WAITER.search(task["command"])
            for task in tasks
        ):
            return
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": "Run jailbee inbox wait --timeout none with Bash run_in_background before stopping.",
                }
            )
        )


def _input() -> dict[str, Any]:
    try:
        data = json.load(sys.stdin)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError, UnicodeError):
        return {}
