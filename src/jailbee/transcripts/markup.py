"""Agent wrapper markup shared by transcript metadata and rendering."""

from __future__ import annotations

import re

_META_WRAPPER = re.compile(r"\A\s*<([a-z][a-z0-9-]*)>(.*?)</\1>", re.DOTALL)
"""An agent's own markup in a user record: command names, reminders, local output."""


def is_meta_wrapper(text: str) -> bool:
    return _META_WRAPPER.match(text) is not None


def command_name(text: str) -> str:
    """The slash command in an agent wrapper, never its arguments or local output."""
    match = _META_WRAPPER.match(text)
    if match is None or match.group(1) != "command-name":
        return ""
    name = match.group(2).strip()
    return name if name.startswith("/") and not any(c in name for c in "<>\n\r") else ""
