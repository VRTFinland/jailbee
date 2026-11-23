"""Agent wrapper markup shared by transcript metadata and rendering."""

from __future__ import annotations

import re

_META_WRAPPER = re.compile(
    r"\A\s*<(command-name|command-message|command-args|local-command-stdout|"
    r"local-command-caveat|system-reminder)>(.*?)</\1>",
    re.DOTALL,
)
"""Known agent wrappers, not arbitrary XML a person may type as a prompt."""


def prompt_text(text: str) -> str:
    """Keep typed text after any leading agent wrappers."""
    while match := _META_WRAPPER.match(text):
        text = text[match.end() :]
    return text.strip()


def is_meta_wrapper(text: str) -> bool:
    return _META_WRAPPER.match(text) is not None and not prompt_text(text)


def command_name(text: str) -> str:
    """The slash command in an agent wrapper, never its arguments or local output."""
    while match := _META_WRAPPER.match(text):
        if match.group(1) == "command-name":
            name = match.group(2).strip()
            return name if name.startswith("/") and not any(c in name for c in "<>\n\r") else ""
        text = text[match.end() :]
    return ""
