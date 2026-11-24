"""The Agent context setting in the real browser: hidden, collapsed, shown."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.flows import log_in_with_pin, open_settings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests_e2e.server import E2EServer

SESSION = "44444444-4444-4444-8444-444444444444"
TITLE = "Check the agent context setting"
NOTIFICATION = "<task-notification>Background build finished</task-notification>"
REMINDER = "<system-reminder>Remember the house style</system-reminder>"
COMMAND = "<command-name>/model</command-name><command-args>opus</command-args>"


def record(number: int, kind: str, text: str) -> str:
    return (
        json.dumps(
            {
                "uuid": f"entry-{number}",
                "sessionId": SESSION,
                "timestamp": "2026-11-02T10:00:00.000Z",
                "type": kind,
                "message": {
                    "role": kind,
                    "content": [{"type": "text", "text": text}] if kind == "assistant" else text,
                    "model": "test-model",
                },
            }
        )
        + "\n"
    )


FILE_REMINDER = "<system-reminder>The file is shorter than the offset</system-reminder>"


def tool_records(number: int) -> str:
    """A Read call whose result carries a reminder Claude Code appended."""
    call = {"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"file_path": "a"}}
    result = {
        "type": "tool_result",
        "tool_use_id": "toolu_read",
        "content": "file body\n\n" + FILE_REMINDER,
    }
    return "".join(
        json.dumps(
            {
                "uuid": f"entry-{number + i}",
                "sessionId": SESSION,
                "timestamp": "2026-11-02T10:00:00.000Z",
                "type": kind,
                "message": {"role": kind, "content": [block], "model": "test-model"},
            }
        )
        + "\n"
        for i, (kind, block) in enumerate([("assistant", call), ("user", result)])
    )


def open_session(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, mode: str | None
) -> list[str]:
    """Open the fixture session in ``mode``; returns the entry-content URLs fetched."""
    home = tmp_path / "agent-home"
    project = home / "projects" / "-work-repo"
    project.mkdir(parents=True)
    (project / f"{SESSION}.jsonl").write_text(
        record(0, "user", TITLE)
        + record(1, "user", NOTIFICATION)
        + record(2, "user", COMMAND)
        + record(3, "user", REMINDER)
        + tool_records(5)
        + record(4, "assistant", "Noted.")
    )
    server = make_server(agent_homes={"work": [("claude", home)]}.get)
    log_in_with_pin(page, server.url, server.pin.current)
    if mode is not None:
        page.evaluate(f"localStorage.setItem('jailbee.agentContext', '{mode}')")
    fetched: list[str] = []
    page.on("request", lambda r: fetched.append(r.url) if "/entries/" in r.url else None)
    page.goto(f"{server.url}/#/repos/work/sessions/claude/{SESSION}")
    page.reload()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    expect(page.get_by_text("Noted.", exact=True)).to_be_visible()
    return fetched


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
def test_hidden_is_the_default_and_leaves_no_meta_in_the_dom(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    open_session(page, make_server, tmp_path, None)

    expect(page.locator(".entry.user.meta")).to_have_count(1)  # the slash command only
    expect(page.locator("details")).to_have_count(0)
    expect(page.get_by_text("Command: /model opus", exact=True)).to_be_visible()
    html = page.content()
    assert "Background build finished" not in html and "house style" not in html
    assert "shorter than the offset" not in html
    assert "Task notification" not in html and "System reminder" not in html


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "phone"])
def test_collapsed_fetches_content_only_when_expanded(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    fetched = open_session(page, make_server, tmp_path, "collapsed")

    expect(page.locator("details.meta-record")).to_have_count(4)
    expect(page.get_by_text("Task notification", exact=True)).to_be_visible()
    assert "shorter than the offset" not in page.content()
    expect(page.get_by_text("Command: /model opus", exact=True)).to_be_visible()
    expect(page.get_by_text("Background build finished")).to_be_hidden()
    expect(page.get_by_text("house style")).to_be_hidden()
    assert "Background build finished" not in page.content()
    assert fetched == []

    page.get_by_text("Task notification", exact=True).click()
    expect(page.get_by_text("Background build finished")).to_be_visible()
    expect(page.get_by_text("house style")).to_be_hidden()
    assert len(fetched) == 1

    page.get_by_text("Task notification", exact=True).click()
    expect(page.get_by_text("Background build finished")).to_be_hidden()


def test_shown_displays_the_content(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    open_session(page, make_server, tmp_path, "shown")

    expect(page.get_by_text("Background build finished")).to_be_visible()
    expect(page.get_by_text("house style")).to_be_visible()
    expect(page.get_by_text("shorter than the offset")).to_be_visible()
    expect(page.get_by_text("Command: /model opus", exact=True)).to_be_visible()
    expect(page.locator("details")).to_have_count(0)


def test_the_settings_control_changes_the_open_transcript(
    page: Page, make_server: Callable[..., E2EServer], tmp_path: Path
) -> None:
    server_fetched = open_session(page, make_server, tmp_path, None)
    assert server_fetched == []
    open_settings(page)
    page.get_by_label("Agent context").select_option("collapsed")
    page.go_back()
    expect(page.get_by_role("heading", name=TITLE, exact=True)).to_be_visible()
    expect(page.locator("details.meta-record")).to_have_count(4)
