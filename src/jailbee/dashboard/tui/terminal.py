"""The terminal window title while the dashboard owns the screen."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, TextIO

from jailbee.dashboard.model import RepoGroup, Row, _find_group

if TYPE_CHECKING:
    from collections.abc import Iterator


_TERMINAL_TITLE_FALLBACK = "🐝 jailbee"


def terminal_title(groups: list[RepoGroup], selected: Row | None) -> str:
    """The xterm/tmux window title for the current selection.

    ``🐝 <repo>/<container>`` on a container row, ``🐝 <repo>`` on a repo
    header, and the bare tool name when nothing is selected or the selected
    container has vanished under the cursor. An orphan group's container shows
    its *full* name, matching the NAME column — there is no known repo prefix
    to have stripped.
    """
    if selected is None:
        return _TERMINAL_TITLE_FALLBACK
    if selected.kind == "repo":
        return f"🐝 {selected.key}"
    group = _find_group(groups, selected.key)
    if group is None:
        return _TERMINAL_TITLE_FALLBACK
    container = next((c for c in group.containers if c.name == selected.key), None)
    if container is None:
        return _TERMINAL_TITLE_FALLBACK
    name = container.name if group.repo_root is None else container.display_name
    return f"🐝 {group.prefix}/{name}"


def title_sequence(text: str) -> str:
    """One OSC 2 window-title sequence."""
    return f"\x1b]2;{text}\x07"


def set_terminal_title(text: str, *, stream: TextIO) -> None:
    """Write one OSC 2 window-title sequence.

    Best-effort: a terminal that does not implement it drops the sequence
    silently, so there is nothing to detect or guard against.
    """
    stream.write(title_sequence(text))
    stream.flush()


@contextmanager
def terminal_title_scope(stream: TextIO) -> Iterator[None]:
    """Save the terminal's own title on entry, restore it on exit.

    Uses the xterm title stack (``CSI 22;2t`` / ``CSI 23;2t``), implemented by
    xterm and tmux and ignored elsewhere. Without the pop the terminal would
    keep jailbee's title after the dashboard quits, since there is no way to
    read the old one back.
    """
    stream.write("\x1b[22;2t")
    stream.flush()
    try:
        yield
    finally:
        stream.write("\x1b[23;2t")
        stream.flush()
