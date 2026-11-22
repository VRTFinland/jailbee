"""Readable console rendering of the Markdown bodies an outbox proposal carries.

A body is Markdown written by an agent in a container, so it routinely holds
paragraphs that are one line long. Printed verbatim they run off the terminal
and the structure (lists, code, headings) is lost. `render_markdown` lays a
body out for a terminal of a given width using Rich's Markdown renderer.

The body is untrusted, and `jb outbox show` / the apply plans exist so a human
reads what will be published, so the rendering must not hide anything GitHub
would show. Three rules follow:

* HTML is switched off in the parser, so a tag or block is shown as the text
  it is instead of silently dropped, and an image shows its URL next to its alt
  text.
* Terminal controls are removed from the *rendered* segments, after the parser
  has decoded entities (``&#x202e;`` becomes a bidi override only at that
  point), so the escape codes in the output are only ever Rich's own styling.
  A link becomes an OSC 8 hyperlink only when `_link_host` accepts its target
  (printable ASCII, http/https); any other is shown as text with its target.
* Those lines are marked `AnsiLine`. The renderers that produce lines stay pure
  and keep returning ``list[str]``; `print_lines` is where an `AnsiLine` is told
  apart from an ordinary line, which still gets `safe_text` and no markup. A
  mark lost along the way (an f-string, a concatenation) degrades to a plain
  line whose escapes are stripped — never to an unsanitised one.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import sys
from collections.abc import Iterable
from functools import lru_cache
from typing import ClassVar
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token
from rich.console import Console
from rich.markdown import Heading, Markdown, MarkdownElement
from rich.segment import Segment, Segments
from rich.style import Style
from rich.text import Text

from jailbee.hyperlinks import hyperlinks_enabled

# Never wrap narrower than this, however deep the indent: a few columns of
# text per line is less readable than a long line.
_MIN_WIDTH = 20
_ROWS = 25


class AnsiLine(str):
    """A line of Rich-generated ANSI output; only `render_markdown` creates one."""

    __slots__ = ()


# Printable ASCII only: no controls, no whitespace, no bidi or other invisible characters.
_SAFE_URL = re.compile(r"https?://[!-~]+", re.IGNORECASE)
_BRACKETS = re.compile(r"%5([BD])", re.IGNORECASE)
# The separator a label is unlikely to imitate convincingly: "(host)" is easy to fake.
_HOST_SEP = " \u2192 "


def _link_host(url: str) -> str | None:
    """The host of a linkable URL, or None when it must not become a hyperlink."""
    if not _SAFE_URL.fullmatch(url):
        return None
    try:
        return urlsplit(url).hostname or None
    except ValueError:
        return None


def _normalise_href(href: str) -> str:
    """`href` with a lower-case scheme and the brackets of an IPv6 literal un-encoded.

    markdown-it percent-encodes ``[`` and ``]`` everywhere, which breaks the
    authority of ``https://[::1]/``; only the authority is repaired.
    """
    scheme, sep, rest = href.partition("://")
    if not sep:
        return href
    end = min((i for i in (rest.find(c) for c in "/?#") if i >= 0), default=len(rest))
    authority = rest[:end]
    userinfo, at, hostport = authority.rpartition("@")
    if hostport[:3].upper() == "%5B":  # only an IPv6 literal, never userinfo
        hostport = _BRACKETS.sub(lambda m: "[" if m[1] in "bB" else "]", hostport)
    authority = f"{userinfo}{at}{hostport}"
    return f"{scheme.lower()}://{authority}{rest[end:]}"


def _text_token(content: str) -> Token:
    token = Token("text", "", 0)
    token.content = content
    return token


def _plain_origin(label: str, href: str, host: str) -> bool:
    """Whether `label` is exactly the scheme and host of `href`, so it cannot mislead."""
    scheme = href.partition("://")[0]
    if urlsplit(href).netloc.lower() != host:  # a port or userinfo is worth showing
        return False
    return label.lower() in (f"{scheme}://{host}".lower(), f"{scheme}://{host}/".lower())


def _rewrite_links(children: list[Token]) -> list[Token]:
    """Keep a safe link, adding its host after any label but the bare origin; flatten others."""
    out: list[Token] = []
    label = ""
    href = ""
    in_link = False
    for child in children:
        if child.type == "link_open":
            href = _normalise_href(str(child.attrs.get("href", "")))
            label, in_link = "", True
            if _link_host(href) is None:
                continue  # dropped: the close below shows the target as text
            child.attrs["href"] = href
        elif child.type == "link_close":
            in_link = False
            host = _link_host(href)
            if host is None:
                if label != href:
                    out.append(_text_token(f"{_HOST_SEP}{href}"))
                continue
            out.append(child)
            if not _plain_origin(label, href, host):
                out.append(_text_token(f"{_HOST_SEP}{host.lower()}"))
            continue
        elif in_link:
            label += child.content
        out.append(child)
    return out


def _safe_style(style: Style | None) -> Style | None:
    """`style` without a link unless the link is one `_link_host` accepts and links are on."""
    if (
        style is not None
        and style.link
        and (not hyperlinks_enabled() or _link_host(style.link) is None)
    ):
        return style.update_link(None)
    return style


class _VerbatimMarkdown(Markdown):
    """Rich's Markdown, minus the places it hides what the author wrote; links vetted."""

    def __init__(self, markup: str) -> None:
        super().__init__(markup, hyperlinks=True)
        # Rich's own parser has HTML on, and Rich then renders no HTML token at all.
        parser = MarkdownIt("commonmark", {"html": False}).enable("strikethrough").enable("table")
        self.parsed = parser.parse(markup)
        for token in self.parsed:
            if token.children:
                token.children = _rewrite_links(token.children)
            for child in token.children or []:
                if child.type == "image":
                    # Rich lifts an image out of its line into a block of its own, which
                    # reorders the text; show its Markdown source in place instead.
                    src = str(child.attrs.get("src", ""))
                    child.type, child.tag, child.children = "text", "", None
                    child.content = f"![{child.content}]({src})"


class _LeftHeading(Heading):
    """A heading flush left: a centred one floats oddly in a narrow panel."""

    LEVEL_ALIGN = dict.fromkeys(Heading.LEVEL_ALIGN, "left")


class _PanelMarkdown(_VerbatimMarkdown):
    """`_VerbatimMarkdown` for a panel: every heading left-aligned."""

    elements: ClassVar[dict[str, type[MarkdownElement]]] = {
        **Markdown.elements,
        "heading_open": _LeftHeading,
    }


@lru_cache(maxsize=64)
def _markdown_lines(text: str, width: int, links: bool) -> tuple[Text, ...]:
    from jailbee.outbox.inspect import safe_text

    console = Console(width=width, height=_ROWS, force_terminal=True, highlight=False)
    rendered = console.render_lines(
        _PanelMarkdown(text), console.options.update(width=width), pad=False
    )
    lines: list[Text] = []
    for row in rendered:
        line = Text()
        for segment in row:
            if not segment.control:
                line.append(safe_text(segment.text), _safe_style(segment.style))
        # Not stripped: a code block's trailing spaces carry its background.
        lines.append(line)
    while lines and not lines[-1].plain.strip():
        lines.pop()
    while lines and not lines[0].plain.strip():
        lines.pop(0)
    return tuple(lines)


def markdown_text_lines(text: str, width: int) -> list[Text]:
    """`text` laid out as Markdown `width` cells wide, one styled `Text` per line.

    For a Rich-drawn panel (the dashboard's details): the same sanitising as
    `render_markdown`, but the lines stay `Text`, so no ANSI round trip, and
    headings are flush left. Leading and trailing blank lines are dropped.
    Cached per (text, width): a panel is redrawn on every refresh, and each
    call returns copies, so a caller may style them.
    """
    return [line.copy() for line in _markdown_lines(text, max(1, width), hyperlinks_enabled())]


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
        Segment(safe_text(segment.text), _safe_style(segment.style))
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
