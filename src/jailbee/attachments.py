"""Validate and resolve Markdown attachment destinations without changing source layout."""

import html
import re
import secrets
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token


class AttachmentError(ValueError):
    """An attachment reference or upload URL is invalid."""


@dataclass(frozen=True)
class Reference:
    id: str
    start: int
    end: int


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_URL = re.compile(
    r"https://github\.com/user-attachments/assets/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_CANDIDATE = re.compile(r"attachment:[A-Za-z0-9_-]*", re.IGNORECASE)


def references(body: str) -> tuple[Reference, ...]:
    original_env: dict[str, Any] = {}
    original_tokens = MarkdownIt("commonmark", {"html": True}).parse(body, original_env)
    expected: Counter[str] = Counter()

    def collect(tokens: list[Token]) -> None:
        for token in tokens:
            if token.type in ("html_inline", "html_block") and (
                "attachment:" in html.unescape(token.content).lower()
            ):
                raise AttachmentError("Unsupported attachment in HTML")
            if token.type in ("link_open", "image"):
                attr = str(token.attrGet("href" if token.type == "link_open" else "src") or "")
                if html.unescape(attr).lower().startswith("attachment:"):
                    if token.markup == "autolink":
                        raise AttachmentError("Unsupported attachment autolink")
                    expected[attr] += 1
            if token.children:
                collect(token.children)

    collect(original_tokens)
    prefix = f"https://attachment.invalid/jailbee-{secrets.token_hex(16)}-"
    matches = list(_CANDIDATE.finditer(body))
    pieces: list[str] = []
    cursor = 0
    candidates: dict[str, re.Match[str]] = {}
    for index, match in enumerate(matches):
        placeholder = f"{prefix}{index}-end"
        pieces.extend((body[cursor : match.start()], placeholder))
        candidates[placeholder] = match
        cursor = match.end()
    pieces.append(body[cursor:])
    rendered = "".join(pieces)
    env: dict[str, Any] = {}
    tokens = MarkdownIt("commonmark", {"html": True}).parse(rendered, env)
    used: set[str] = set()
    observed: Counter[str] = Counter()

    def inspect(tokens: list[Token]) -> None:
        for token in tokens:
            if token.type in ("html_inline", "html_block"):
                raw = html.unescape(token.content).lower()
                if "attachment:" in raw or prefix in raw:
                    raise AttachmentError("Unsupported attachment in HTML")
            if token.type in ("link_open", "image"):
                attr = str(token.attrGet("href" if token.type == "link_open" else "src") or "")
                decoded = html.unescape(attr).lower()
                if attr in candidates:
                    if token.markup == "autolink":
                        raise AttachmentError("Unsupported attachment autolink")
                    used.add(attr)
                    observed[candidates[attr].group()] += 1
                elif decoded.startswith("attachment:") or attr.startswith(prefix):
                    raise AttachmentError("Unsupported attachment destination")
            if token.children:
                inspect(token.children)

    inspect(tokens)
    for definition in env.get("references", {}).values():
        destination = definition["href"]
        if destination in candidates and destination not in used:
            raise AttachmentError("Unused attachment definition")
        if destination not in candidates and (
            html.unescape(destination).lower().startswith("attachment:")
            or destination.startswith(prefix)
        ):
            raise AttachmentError("Unsupported attachment definition")
    if observed != expected:
        raise AttachmentError("Cannot safely map attachment destinations")
    refs = []
    for placeholder in used:
        match = candidates[placeholder]
        identifier = match.group()[len("attachment:") :]
        if not match.group().startswith("attachment:") or not _ID.fullmatch(identifier):
            raise AttachmentError("Invalid attachment ID")
        refs.append(Reference(identifier, match.start(), match.end()))
    return tuple(sorted(refs, key=lambda ref: ref.start))


def validate_references(bodies: Iterable[str], declared_ids: Iterable[str]) -> None:
    declared = set(declared_ids)
    if any(not _ID.fullmatch(item) for item in declared):
        raise AttachmentError("Invalid attachment ID")
    used = {ref.id for body in bodies for ref in references(body)}
    if used != declared:
        raise AttachmentError("Missing or unused attachment IDs")


def rewrite(body: str, urls: Mapping[str, str]) -> str:
    refs = references(body)
    for ref in refs:
        url = urls.get(ref.id)
        if url is None or not _URL.fullmatch(url):
            raise AttachmentError("Missing or invalid attachment URL")
    pieces: list[str] = []
    cursor = 0
    for ref in refs:
        pieces.extend((body[cursor : ref.start], urls[ref.id]))
        cursor = ref.end
    pieces.append(body[cursor:])
    return "".join(pieces)


_URL_SHAPE = _URL.pattern.removesuffix(r"\Z")


def matches_rendered(body: str, rendered: str) -> bool:
    """Whether `rendered` is `body` with every attachment placeholder replaced by an upload URL.

    Recognises a comment that already holds an edit whose uploads were not
    recorded: each placeholder matches any attachment URL.
    """
    try:
        refs = references(body)
    except AttachmentError:
        return False
    if not refs:
        return rendered == body
    pieces: list[str] = []
    cursor = 0
    for ref in refs:
        pieces.extend((re.escape(body[cursor : ref.start]), _URL_SHAPE))
        cursor = ref.end
    pieces.append(re.escape(body[cursor:]))
    return re.fullmatch("".join(pieces), rendered) is not None
