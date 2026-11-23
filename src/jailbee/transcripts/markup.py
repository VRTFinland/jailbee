"""Agent wrapper markup shared by transcript metadata and rendering."""

from __future__ import annotations

import re
from collections.abc import Iterator

_META_WRAPPER = re.compile(
    r"\s*<(command-[a-z-]+|local-command-[a-z-]+|system-reminder|"
    r"task-notification|task_notification|bash-input|bash-stdout|bash-stderr|"
    r"user-prompt-submit-hook|teammate-message|coordinator-task|ide_selection|"
    r"terminal|webhook-payload)(?:\s[^<>]*)?>",
    re.DOTALL,
)
"""Claude's leading wrappers: command/local-command families, injected reminders,
background task/monitor notifications, !-mode input/output, prompt-submit hooks,
teammate/coordinator messages, IDE selections/terminals and webhook payloads.
The recorded corpus covers commands, reminders and task notifications; the
remaining names were checked against Claude Code 2.1.330. Attributes are allowed
on known names (for example teammate_id or nonce); the attribute part starts
with exactly one whitespace character so the match stays linear. ``terminal`` is
generic but real: Claude Code writes ``<terminal name="...">`` for an IDE
terminal selection, beside ``<ide_selection>``. Nested tags belong to their
outer wrapper; when an inner same-name tag is never closed, the first closing
tag ends the wrapper. Unknown XML (such as <example>) stays typed text, and a
known name only counts with a matching closing tag at the start of the text.
"""


def _wrappers(text: str) -> Iterator[tuple[str, str, int]]:
    """Yield complete leading wrappers, balancing nested tags with the same name.

    When balancing fails (an inner same-name tag is never closed), the first
    closing tag of that name ends the wrapper.
    """
    end = 0
    while opening := _META_WRAPPER.match(text, end):
        tag = opening.group(1)
        tokens = re.compile(rf"<(?P<close>/)?{re.escape(tag)}(?:\s[^<>]*)?>")
        depth = 1
        first_close: re.Match[str] | None = None
        closing: re.Match[str] | None = None
        for token in tokens.finditer(text, opening.end()):
            if token.group("close"):
                depth -= 1
                first_close = first_close or token
            else:
                depth += 1
            if depth == 0:
                closing = token
                break
        end_tag = closing or first_close
        if end_tag is None:
            return
        end = end_tag.end()
        yield tag, text[opening.end() : end_tag.start()], end


def split_prompt(text: str) -> tuple[str, str]:
    """Separate the leading agent markup from typed text for titles and rendering."""
    end = 0
    for _, _, wrapper_end in _wrappers(text):
        end = wrapper_end
    return text[:end], text[end:].strip()


def prompt_text(text: str) -> str:
    """Keep typed text after any leading agent wrappers."""
    return split_prompt(text)[1]


def is_meta_wrapper(text: str) -> bool:
    markup, typed = split_prompt(text)
    return bool(markup) and not typed


def command_name(text: str) -> str:
    """The slash command in an agent wrapper, never its arguments or local output."""
    for tag, content, _ in _wrappers(text):
        if tag == "command-name":
            name = content.strip()
            return name if name.startswith("/") and not any(c in name for c in "<>\n\r") else ""
    return ""


_LABELS = {
    "task-notification": "Task notification",
    "task_notification": "Task notification",
    "system-reminder": "System reminder",
    "local-command-stdout": "Command output",
    "local-command-stderr": "Command output",
    "local-command-caveat": "Command caveat",
    "command-message": "Command",
    "command-args": "Command",
    "bash-stdout": "Shell output",
    "bash-stderr": "Shell output",
    "user-prompt-submit-hook": "Hook",
    "teammate-message": "Teammate message",
    "coordinator-task": "Coordinator task",
    "ide_selection": "IDE selection",
    "terminal": "Terminal",
    "webhook-payload": "Webhook payload",
}
_LABEL_DETAIL_MAX = 60


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    if len(line) > _LABEL_DETAIL_MAX:
        return line[: _LABEL_DETAIL_MAX - 1] + "…"
    return line


def _command_args(text: str) -> str:
    """The arguments of the slash command in an agent wrapper, one line."""
    for tag, content, _ in _wrappers(text):
        if tag == "command-args":
            return _one_line(content)
    return ""


def meta_label(text: str) -> str:
    """A short label for leading agent markup: one mapping from wrapper tag to name.

    A slash command reads ``Command: /model`` (with its arguments), shell input
    ``Shell: ls``; every other wrapper has a fixed name. Empty without markup.
    """
    if name := command_name(text):
        args = _command_args(text)
        return f"Command: {name} {args}" if args else f"Command: {name}"
    for tag, content, _ in _wrappers(text):
        if tag == "bash-input":
            detail = _one_line(content)
            return f"Shell: {detail}" if detail else "Shell"
        return _LABELS.get(tag, "Agent context")
    return ""
