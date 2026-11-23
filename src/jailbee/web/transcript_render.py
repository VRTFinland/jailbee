"""Transcript entries to their displayable form (`protocol.RenderedEntry`).

`render_entries` is the one conversion: the REST session view uses it for a
whole session and the WebSocket's live appends for the entries a later read
returns, so both carry the same HTML. Markdown rendering is the expensive
part; `RenderCache` keeps the HTML per entry, bounded, owned by the app.
"""

from __future__ import annotations

import html
import json
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import replace

from jailbee.transcripts.base import (
    AssistantText,
    Entry,
    SubagentRef,
    Thinking,
    ToolCall,
    ToolResult,
    UserMessage,
)
from jailbee.transcripts.markup import is_meta_wrapper, split_prompt
from jailbee.web.highlight import highlight_json
from jailbee.web.markdown_render import render_markdown
from jailbee.web.protocol import (
    RenderedAssistant,
    RenderedEntry,
    RenderedSubagent,
    RenderedThinking,
    RenderedToolCall,
    RenderedToolResult,
    RenderedUser,
)

DEFAULT_CACHE_ENTRIES = 4096


class RenderCache:
    """HTML by (entry id, text), least recently used dropped beyond ``limit``.

    The text is part of the key, so an id reused with other content (a
    rewritten transcript) never shows stale HTML. Thread-safe: routes render
    in the thread pool.
    """

    def __init__(self, limit: int = DEFAULT_CACHE_ENTRIES) -> None:
        self._limit = limit
        self._items: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, entry_id: str, text: str, render: Callable[[str], str]) -> str:
        key = (entry_id, text)
        with self._lock:
            hit = self._items.get(key)
            if hit is not None:
                self._items.move_to_end(key)
                return hit
        rendered = render(text)
        with self._lock:
            self._items[key] = rendered
            self._items.move_to_end(key)
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        return rendered

    def __len__(self) -> int:
        return len(self._items)


def _pre(text: str, css_class: str) -> str:
    return f'<pre class="{css_class}">{html.escape(text)}</pre>'


def _input_json(value: dict[str, object]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False, default=str)


def render_entry(entry: Entry, cache: RenderCache) -> RenderedEntry:
    match entry:
        case UserMessage():
            if entry.meta or is_meta_wrapper(entry.text):
                return RenderedUser(
                    id=entry.id, timestamp=entry.timestamp, html=_pre(entry.text, "meta"), meta=True
                )
            return RenderedUser(
                id=entry.id,
                timestamp=entry.timestamp,
                html=cache.get(entry.id, entry.text, render_markdown),
            )
        case AssistantText():
            body = cache.get(entry.id, entry.text, render_markdown)
            return RenderedAssistant(
                id=entry.id, timestamp=entry.timestamp, html=body, model=entry.model
            )
        case Thinking():
            body = cache.get(entry.id, entry.text, render_markdown) if entry.text else ""
            return RenderedThinking(
                id=entry.id, timestamp=entry.timestamp, html=body, model=entry.model
            )
        case ToolCall():
            arguments = _input_json(entry.input)
            return RenderedToolCall(
                id=entry.id,
                timestamp=entry.timestamp,
                call_id=entry.call_id,
                name=entry.name,
                summary=entry.summary,
                input_html=cache.get(entry.id, arguments, highlight_json),
                model=entry.model,
            )
        case ToolResult():
            return RenderedToolResult(
                id=entry.id,
                timestamp=entry.timestamp,
                call_id=entry.call_id,
                html=_pre(entry.text, "tool-output"),
                truncated=entry.truncated,
                full_length=entry.full_length,
                is_error=entry.is_error,
            )
        case SubagentRef():
            return RenderedSubagent(
                id=entry.id,
                timestamp=entry.timestamp,
                call_id=entry.call_id,
                agent_id=entry.agent_id,
                agent_type=entry.agent_type,
                description=entry.description,
            )


def subagent_descriptions(entries: Iterable[Entry]) -> dict[str, str]:
    """Subagent id → what it was asked to do.

    A reference carries its own description when the agent logged one; otherwise
    it is the ``description`` argument of the tool call that started it.
    """
    entries = tuple(entries)
    asked = {
        e.call_id: d
        for e in entries
        if isinstance(e, ToolCall) and isinstance(d := e.input.get("description"), str) and d
    }
    found: dict[str, str] = {}
    for e in entries:
        if isinstance(e, SubagentRef):
            text = e.description or asked.get(e.call_id)
            if text:
                found[e.agent_id] = text
    return found


def render_entries(entries: Iterable[Entry], cache: RenderCache) -> list[RenderedEntry]:
    """Every entry in order, rendered; HTML reused from ``cache`` where it can be."""
    entries = tuple(entries)
    described = subagent_descriptions(entries)
    rendered: list[RenderedEntry] = []
    for e in entries:
        if isinstance(e, UserMessage) and not e.meta:
            markup, typed = split_prompt(e.text)
            if markup and typed:
                rendered.append(
                    RenderedUser(
                        id=f"{e.id}:meta",
                        timestamp=e.timestamp,
                        html=_pre(markup, "meta"),
                        meta=True,
                    )
                )
                e = replace(e, text=typed)
        r = render_entry(e, cache)
        if isinstance(r, RenderedSubagent) and r.description is None:
            r = r.model_copy(update={"description": described.get(r.agent_id)})
        rendered.append(r)
    return rendered
