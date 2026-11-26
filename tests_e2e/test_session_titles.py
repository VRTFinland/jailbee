"""Session titles and agent markup through a real browser and fixture home."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Page, expect

from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer

SESSION = "11111111-1111-4111-8111-111111111111"


def test_session_list_prefers_the_typed_prompt_and_transcript_marks_clear_as_meta(
    make_server: Callable[..., E2EServer], page: Page, tmp_path: Path
) -> None:
    home = tmp_path / "claude"
    project = home / "projects" / "-work-repo"
    project.mkdir(parents=True)
    wrapper = "<command-name>/clear</command-name><command-args></command-args>"
    prompt = "Fix the session titles"
    records = [
        {"type": "user", "uuid": "clear", "message": {"content": wrapper}},
        {"type": "user", "uuid": "prompt", "message": {"content": prompt}},
    ]
    (project / f"{SESSION}.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records)
    )
    server = make_server(
        agent_homes=lambda prefix: [("claude", home)] if prefix == "work" else None
    )
    log_in_with_pin(page, server.url, server.pin.current)

    page.goto(f"{server.url}/#/repos/work")

    session_link = page.get_by_role("link", name=prompt)
    expect(session_link).to_be_visible()
    expect(session_link).to_have_text(prompt)
    session_link.click()
    expect(page.get_by_role("heading", name=prompt, exact=True)).to_be_visible()
    # A typed slash command is a label line in every Agent context mode.
    expect(page.locator(".entry.user.meta")).to_have_text("Command: /clear")
    expect(page.locator(".entry.user:not(.meta)")).to_have_text(prompt)


def test_a_mixed_user_record_titles_by_the_typed_text_and_shows_the_reminder_as_meta(
    make_server: Callable[..., E2EServer], page: Page, tmp_path: Path
) -> None:
    home = tmp_path / "claude"
    project = home / "projects" / "-work-repo"
    project.mkdir(parents=True)
    reminder = "<system-reminder>Injected context</system-reminder>"
    typed = "Fix the mixed title"
    record = {"type": "user", "uuid": "mixed", "message": {"content": f"{reminder}\n{typed}"}}
    (project / f"{SESSION}.jsonl").write_text(json.dumps(record) + "\n")
    server = make_server(
        agent_homes=lambda prefix: [("claude", home)] if prefix == "work" else None
    )
    log_in_with_pin(page, server.url, server.pin.current)
    # Reminders are hidden by default; showing agent context renders them as meta.
    page.evaluate("localStorage.setItem('jailbee.agentContext', 'shown')")

    # A hash-only goto right after a reload can race the app's router; load
    # the repository's URL afresh instead, as test_agent_context does.
    page.goto(f"{server.url}/#/repos/work")
    page.reload()

    expect(page.get_by_role("link", name=f"{typed} claude unattributed", exact=True)).to_have_text(typed)
    page.get_by_role("link", name=typed).click()
    expect(page.get_by_role("heading", name=typed, exact=True)).to_be_visible()
    expect(page.locator(".entry.user.meta pre.meta")).to_have_text(reminder)
    expect(page.locator(".entry.user:not(.meta)")).to_have_text(typed)
