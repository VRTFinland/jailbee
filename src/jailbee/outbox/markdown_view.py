"""Readable console rendering of the Markdown bodies an outbox proposal carries.

A body is Markdown written by an agent in a container, so it routinely holds
paragraphs that are one line long. Printed verbatim they run off the terminal
and the structure (lists, code, headings) is lost. `render_markdown` lays a
body out for a terminal of a given width using Rich's Markdown renderer.

The body is untrusted, and `jb review show` / the apply plans exist so a human
reads what will be published, so the rendering must not hide anything GitHub
would show. Three rules follow:

* HTML is switched off in the parser, so a tag or block is shown as the text
  it is instead of silently dropped, and an image shows its URL next to its alt
  text.
* Terminal controls are removed from the *rendered* segments, after the parser
  has decoded entities (``&#x202e;`` becomes a bidi override only at that
  point), so the escape codes in the output are only ever Rich's own styling.
  Links are shown as text, never as OSC 8 hyperlinks.
* Those lines are marked `AnsiLine`. The renderers that produce lines stay pure
  and keep returning ``list[str]``; `print_lines` is where an `AnsiLine` is told
  apart from an ordinary line, which still gets `safe_text` and no markup. A
  mark lost along the way (an f-string, a concatenation) degrades to a plain
  line whose escapes are stripped — never to an unsanitised one.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
from collections.abc import Iterable

from markdown_it import MarkdownIt
from rich.console import Console
from rich.markdown import Markdown
from rich.segment import Segment, Segments

# Never wrap narrower than this, however deep the indent: a few columns of
# text per line is less readable than a long line.
_MIN_WIDTH = 20
_ROWS = 25


class AnsiLine(str):
    """A line of Rich-generated ANSI output; only `render_markdown` creates one."""

    __slots__ = ()


class _VerbatimMarkdown(Markdown):
    """Rich's Markdown, minus the two places it hides what the author wrote."""

    def __init__(self, markup: str) -> None:
        super().__init__(markup, hyperlinks=False)
        # Rich's own parser has HTML on, and Rich then renders no HTML token at all.
        parser = MarkdownIt("commonmark", {"html": False}).enable("strikethrough").enable("table")
        self.parsed = parser.parse(markup)
        for token in self.parsed:
            for child in token.children or []:
                if child.type == "image":
                    # Rich lifts an image out of its line into a block of its own, which
                    # reorders the text; show its Markdown source in place instead.
                    src = str(child.attrs.get("src", ""))
                    child.type, child.tag, child.children = "text", "", None
                    child.content = f"![{child.content}]({src})"


def _terminal_width() -> int | None:
    """The terminal's width, or None when stdout is not a terminal.

    A pipe or a log gets the body verbatim, so `jb ... show | grep` keeps
    matching what the agent wrote.
    """
    if not sys.stdout.isatty():
        return None
    return shutil.get_terminal_size().columns


def render_width(color: bool | None) -> int | None:
    """The width to lay bodies out at, or None for the verbatim text.

    `color` is a command's `--color/--no-color`: None follows stdout
    (`_terminal_width`), False is always verbatim, and True renders even into a
    pipe — `jailbee dashboard` pipes `outbox show` into a pager, which draws on
    the terminal stderr is still attached to, so that is the width taken.
    """
    if color is False:
        return None
    width = _terminal_width()
    if width is not None or not color:
        return width
    try:
        return os.get_terminal_size(sys.stderr.fileno()).columns
    except (OSError, ValueError):
        return shutil.get_terminal_size().columns


def render_markdown(text: str, *, indent: str = "", width: int | None = None) -> list[str]:
    """Lay `text` out as Markdown for a terminal, one string per line, each prefixed by `indent`.

    `width` is the full line width including `indent`; when omitted it is the
    terminal's, and a non-terminal stdout returns the text unwrapped and
    unrendered.
    """
    # Imported here: `outbox.inspect` imports `pr_outbox`, which imports this module.
    from jailbee.outbox.inspect import safe_text

    if not text:
        return []
    if width is None:
        width = _terminal_width()
    if width is None:
        return [f"{indent}{safe_text(line)}" for line in text.split("\n")]
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        width=max(width - len(indent), _MIN_WIDTH),
        # Rich ignores an explicit width on a dumb terminal unless the height is
        # given too; the height is otherwise irrelevant to a Markdown render.
        height=_ROWS,
        force_terminal=True,
        color_system="auto",
        highlight=False,
    )
    # Sanitise what the parser produced, not what it was given: entities decode late.
    segments = [
        Segment(safe_text(segment.text), segment.style)
        for segment in console.render(_VerbatimMarkdown(text))
        if not segment.control
    ]
    console.print(Segments(segments), end="")
    lines = [line.rstrip() for line in buffer.getvalue().split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return [AnsiLine(f"{indent}{line}") if line else "" for line in lines]


def print_lines(lines: Iterable[str], *, color: bool = False) -> None:
    """Print outbox lines: `AnsiLine`s as the styled text they are, the rest as inert text.

    `color` keeps the styling when stdout is not a terminal, for a pager that
    renders it (see `render_width`).
    """
    from rich.text import Text

    from jailbee.outbox.inspect import safe_text
    from jailbee.tui import console

    if color and not console.is_terminal:
        console = Console(file=console.file, force_terminal=True)
    for line in lines:
        if isinstance(line, AnsiLine):
            console.print(Text.from_ansi(line), highlight=False, soft_wrap=True)
        else:
            console.print(safe_text(line), markup=False, highlight=False, soft_wrap=True)
