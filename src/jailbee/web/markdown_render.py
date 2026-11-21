"""Markdown to safe HTML for transcript entries. Ported from codereader.

Raw HTML in the source is escaped, never passed through: the inline and
block HTML handlers are removed, so ``<script>`` arrives as text. Every link
and image URL is checked against a scheme allowlist; anything else
(``javascript:``, ``data:``, ``vbscript:``, ...) loses its URL. Images are
turned into plain links, so opening a transcript never loads a remote
resource. Fenced code is highlighted by Pygments with CSS classes (see
`highlight`), so a theme change needs no re-render.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

import markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

_SAFE_SCHEMES = frozenset({"http", "https", "mailto"})
_CONTROL = re.compile(r"[\x00-\x20\x7f]+")


def safe_url(url: str) -> str | None:
    """``url`` if browsers would not run it as code, else None.

    Relative URLs and fragments pass; an absolute URL must use an allowed
    scheme. Browsers ignore control characters and whitespace inside a
    scheme (``java\\tscript:``), so they are dropped before the check.
    """
    probe = _CONTROL.sub("", url)
    if not probe:
        return None
    scheme = urlsplit(probe).scheme.lower()
    if scheme and scheme not in _SAFE_SCHEMES:
        return None
    if not scheme and ":" in probe.split("/", 1)[0]:
        # "foo:bar" with an unparsable scheme: refuse rather than guess.
        return None
    return url


class _LinkSanitiser(Treeprocessor):
    def run(self, root: Element) -> None:
        for parent in root.iter():
            for index, child in enumerate(list(parent)):
                if child.tag == "img":
                    parent[index] = _image_as_link(child)
        for a in root.iter("a"):
            href = safe_url(a.get("href", ""))
            if href is None:
                a.attrib.pop("href", None)
                continue
            a.set("href", href)
            if not href.startswith("#"):
                a.set("target", "_blank")
                a.set("rel", "noopener noreferrer nofollow")


def _image_as_link(img: Element) -> Element:
    link = Element("a")
    link.text = img.get("alt") or img.get("src") or "image"
    link.tail = img.tail
    src = img.get("src", "")
    if src:
        link.set("href", src)
    return link


class _SafeExtension(Extension):
    def extendMarkdown(self, md: markdown.Markdown) -> None:  # noqa: N802 - Markdown's API name
        # No raw HTML: deregistered handlers leave the text to be escaped.
        md.preprocessors.deregister("html_block")
        md.inlinePatterns.deregister("html")
        md.treeprocessors.register(_LinkSanitiser(md), "jailbee_links", 1)


_TABLE_RE = re.compile(r"^\s*\|")
_LIST_RE = re.compile(r"^(\s*[-*+]|\s*\d+[.)]) ")
_ORDERED_RE = re.compile(r"^\s*\d+[.)] ")
_FENCED_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def _ensure_blank_lines(text: str) -> str:
    """Insert the blank line Python-Markdown needs before a table, list or fence.

    GitHub and agents' own output are lenient here; Python-Markdown is not.
    Lines inside a fenced block are left alone.
    """
    result: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        opening = _FENCED_RE.match(line)
        if fence is not None:
            if opening and opening.group(1).startswith(fence):
                fence = None
            result.append(line)
            continue
        if result and result[-1].strip():
            prev = result[-1]
            if (
                (_TABLE_RE.match(line) and not _TABLE_RE.match(prev))
                or (_LIST_RE.match(line) and not _LIST_RE.match(prev))
                or (
                    _LIST_RE.match(line)
                    and bool(_ORDERED_RE.match(line)) != bool(_ORDERED_RE.match(prev))
                )
                or opening
            ):
                result.append("")
        if opening:
            fence = opening.group(1)
        result.append(line)
    return "\n".join(result)


def render_markdown(text: str) -> str:
    """``text`` as HTML: Markdown structures rendered, raw HTML escaped."""
    md = markdown.Markdown(
        extensions=[
            "fenced_code",
            "codehilite",
            "tables",
            "sane_lists",
            _SafeExtension(),
        ],
        extension_configs={
            "codehilite": {"css_class": "highlight", "guess_lang": False},
        },
        output_format="html",
    )
    return md.convert(_ensure_blank_lines(text))
