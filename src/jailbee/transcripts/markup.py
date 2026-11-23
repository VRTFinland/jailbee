"""Agent wrapper markup shared by transcript metadata and rendering."""

from __future__ import annotations

import re

_META_WRAPPER = re.compile(
    r"\s*<(command-[a-z-]+|local-command-[a-z-]+|system-reminder|"
    r"task-notification|task_notification|bash-input|bash-stdout|bash-stderr|"
    r"user-prompt-submit-hook|teammate-message|coordinator-task|ide_selection|"
    r"terminal|webhook-payload)(?:\s+[^<>]*?)?>(.*?)</\1>",
    re.DOTALL,
)
"""Claude's leading wrappers: command/local-command families, injected reminders,
background task/monitor notifications, !-mode input/output, prompt-submit hooks,
teammate/coordinator messages, IDE selections/terminals and webhook payloads.
The recorded corpus covers commands, reminders and task notifications; the
remaining names were checked against Claude Code 2.1.330. Attributes are allowed
on known names (for example teammate_id or nonce). Nested tags belong
to their outer wrapper. Unknown XML (such as <example>) stays typed text, and a
known name only counts with a matching closing tag at the start of the text.
"""


def split_prompt(text: str) -> tuple[str, str]:
    """Separate the leading agent markup from typed text for titles and rendering."""
    end = 0
    while match := _META_WRAPPER.match(text, end):
        end = match.end()
    return text[:end], text[end:].strip()


def prompt_text(text: str) -> str:
    """Keep typed text after any leading agent wrappers."""
    return split_prompt(text)[1]


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
