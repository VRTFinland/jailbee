"""Assistant record identity, not rendered text-block boundaries, defines a question set."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.server import E2EServer
from tests_e2e.test_answer_panel import open_questions
from tests_e2e.test_session_reading import record


def test_question_sets_group_real_assistant_content_blocks(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_questions(page, make_server, tmp_path, monkeypatch, blocks=[
        {"type": "text", "text": "❓ **Q40** - **First**: First question?"},
        {"type": "text", "text": "a) First option\nb) Second option\n➡️ a: Safer choice"},
        {"type": "text", "text": "❓ **Q41** - **Second**: Second question?"},
        {"type": "text", "text": "A closing note with no questions."},
    ])
    panel = page.get_by_role("complementary", name="Answer questions")
    panel.get_by_role("button", name="Expand answers", exact=True).click()
    panel.get_by_role("button", name="Question 40", exact=True).click()
    panel.get_by_role("button", name="Show in conversation").click()
    expect(page.locator(".question-highlight").first).to_contain_text("First question?")
    assert page.locator(".question-highlight").first.evaluate("el => el.closest('.entry').id") == "entry-entry-2:0"
    expect(page.locator(".question-highlight").last).to_contain_text("Safer choice")
    panel.get_by_role("button", name="Question 41", exact=True).click()
    panel.get_by_role("button", name="Show in conversation").click()
    expect(page.locator(".question-highlight").first).to_contain_text("Second question?")
    assert page.locator(".question-highlight").first.evaluate("el => el.closest('.entry').id") == "entry-entry-2:2"


@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_tool_only_assistant_record_marks_existing_draft_as_older(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    server, transcript, _ = open_questions(page, make_server, tmp_path, monkeypatch, profile=profile)
    panel = page.get_by_role("complementary", name="Answer questions")
    panel.get_by_role("button", name="Expand answers", exact=True).click()
    tool = json.loads(record(3, "assistant", ""))
    tool["message"]["content"] = [{"type": "tool_use", "id": "call-1", "name": "Read", "input": {"file_path": "/work/example"}}]
    with transcript.open("a") as stream:
        stream.write(json.dumps(tool) + "\n")
    server.watches.open[-1].fire()
    expect(panel.get_by_role("status")).to_contain_text("A newer message arrived")
    expect(panel.get_by_role("heading", name="Q12: Deployment")).to_be_visible()


def test_eink_long_question_and_summary_turn_pages_without_swipe_scrolling(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page.set_viewport_size({"width": 390, "height": 800})
    long = "❓ **Q50** - **Long question**: " + "Read the full reasoning carefully. " * 60 + "\na) Canary\nb) All\n➡️ a: Safe"
    open_questions(page, make_server, tmp_path, monkeypatch, profile="eink", latest=long)
    panel = page.get_by_role("complementary", name="Answer questions")
    panel.get_by_role("button", name="Expand answers", exact=True).click()
    next_page = panel.get_by_role("button", name="Next answer page", exact=True)
    expect(next_page).to_be_visible()
    next_page.click()
    expect(panel.locator('.answer-pages > span')).to_have_text(re.compile(r'^2 /'))
    panel.get_by_role('button', name='Previous answer page', exact=True).click()
    panel.get_by_role('button', name='Show in conversation').focus()
    page.keyboard.press('PageDown')
    assert panel.locator('.panel-content').evaluate('el => el.scrollTop') > 0
    panel.get_by_role('button', name='Previous answer page', exact=True).click()
    page.evaluate("document.documentElement.dataset.profile = 'normal'")
    expect(next_page).to_have_count(0)
    page.evaluate("document.documentElement.dataset.profile = 'eink'")
    expect(next_page).to_be_visible()
    assert panel.locator(".panel-content").evaluate("el => getComputedStyle(el).overflowY") == "hidden"
    turns = 0
    while next_page.is_enabled():
        next_page.click()
        turns += 1
        assert turns < 30
    expect(panel.get_by_role("button", name="Next step")).to_be_in_viewport()
    panel.get_by_role("textbox", name="Optional note").fill("An exact note. " * 100)
    panel.get_by_role("button", name="Next step").click()
    expect(next_page).to_be_enabled()
    turns = 0
    while next_page.is_enabled():
        next_page.click()
        turns += 1
        assert turns < 30
    expect(panel.get_by_role("button", name="Send answers")).to_be_in_viewport()
    panel.get_by_role("button", name="Send answers").click()
    expect(panel).to_have_count(0)
