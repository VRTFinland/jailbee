"""Hand-written sessions and user-level steps shared by transcript browser tests."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from uuid import uuid4

from playwright.sync_api import expect

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from tests_e2e.server import E2EServer

SESSION_ID = "11111111-1111-4111-8111-111111111111"
SESSION_TITLE = "Fix the login bug in the auth module"
OTHER_TITLE = "hello there"
SUBAGENT_ID = "a1b2c3d4e5f6a7b8c"


def session_path(home: Path) -> Path:
    return home / "projects" / "-work-repo" / f"{SESSION_ID}.jsonl"


def open_session(page: Page, title: str = SESSION_TITLE) -> None:
    """Open a session from a repository or container's session list."""
    page.get_by_role("link", name=title).click()
    expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
    expect(page.get_by_text("Loading…", exact=True)).to_have_count(0)


def append_entry(server: E2EServer, path: Path, text: str, *, role: str = "assistant") -> None:
    """Write one Claude entry and notify its fake watch, like a live agent would.

    With the server stopped, only the file changes: reconnect must catch it up.
    Each entry gets its own id so identical text can still be distinct replies.
    """
    record = {
        "uuid": str(uuid4()),
        "timestamp": "2026-11-23T12:00:00.000Z",
        "sessionId": path.stem,
        "type": role,
        "message": {
            "role": role,
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}],
        },
    }
    with path.open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    for watch in server.watches.open:
        if watch.path == path:
            watch.fire()
