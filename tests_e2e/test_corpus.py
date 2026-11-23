"""Walk the recorded corpus of genuine Claude Code sessions in the browser.

The corpus (`tests/fixtures/transcripts/claude-corpus`, recorded by
`make web-corpus`) is the agent home of repository `toy`. Content assertions
use only the markers the recorder's prompts guarantee (its `corpus.json`);
everything else a model wrote may change with the next recording.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer

CORPUS = Path(__file__).resolve().parents[1] / "tests/fixtures/transcripts/claude-corpus"
MANIFEST: dict[str, Any] = json.loads((CORPUS / "corpus.json").read_text())
SESSIONS: list[dict[str, Any]] = MANIFEST["sessions"]
REPO = "toy"


def prompt_marker(prompt: str) -> str:
    """The `CORPUS-…` word a recorded prompt starts with."""
    return prompt.split(":", 1)[0]


@pytest.fixture
def corpus_home(tmp_path: Path) -> Path:
    home = tmp_path / "claude"
    shutil.copytree(CORPUS / "projects", home / "projects")
    return home


@pytest.fixture
def server(make_server: Callable[..., E2EServer], corpus_home: Path) -> E2EServer:
    return make_server(agent_homes={REPO: [("claude", corpus_home)]}.get)


@pytest.fixture
def reader(server: E2EServer, page: Page) -> Page:
    log_in_with_pin(page, server.url, server.pin.current, "corpus reader")
    return page


def open_from_list(page: Page, session: dict[str, Any]) -> None:
    """Open ``session`` from the repository's session list by its first prompt."""
    page.goto(page.url.split("#", 1)[0] + f"#/repos/{REPO}")
    first = prompt_marker(session["prompts"][0])
    page.locator(".session a", has_text=first).click()
    expect(page.locator("h1", has_text=first)).to_be_visible()
    expect(page.get_by_text("Loading…", exact=True)).to_have_count(0)


@pytest.mark.parametrize("session", SESSIONS, ids=[s["name"] for s in SESSIONS])
def test_every_session_opens_with_its_prompts(reader: Page, session: dict[str, Any]) -> None:
    open_from_list(reader, session)
    # Only what the person typed is a prompt: a slash command, a compaction
    # summary or an interruption notice is shown as context.
    typed = [prompt_marker(p) for p in session["prompts"] if not p.startswith("/")]
    prompts = reader.locator(".entry.user:not(.meta)")
    expect(prompts).to_have_count(len(typed))
    for index, marker in enumerate(typed):
        expect(prompts.nth(index)).to_contain_text(marker)


def expand_every_tool_call(page: Page) -> None:
    """Unfold each tool call, checking it starts folded and opens on a click."""
    calls = page.locator(".tool-call")
    for index in range(calls.count()):
        call = calls.nth(index)
        summary = call.locator(".tool-summary")
        expect(summary).to_have_attribute("aria-expanded", "false")
        expect(call.locator(".tool-body")).to_have_count(0)
        summary.click()
        expect(summary).to_have_attribute("aria-expanded", "true")
        expect(call.locator(".tool-body .tool-input")).to_be_visible()
        expect(call.locator(".tool-body .tool-result")).to_be_visible()


TOOLS = [s for s in SESSIONS if "tool_call" in s["shapes"]]


@pytest.mark.parametrize("session", TOOLS, ids=[s["name"] for s in TOOLS])
def test_tool_calls_fold_and_expand_to_their_results(reader: Page, session: dict[str, Any]) -> None:
    open_from_list(reader, session)
    calls = reader.locator(".tool-call")
    expect(calls.first).to_be_visible()
    expect(reader.locator(".tool-body")).to_have_count(0)
    expand_every_tool_call(reader)
    transcript = reader.locator("article.transcript")
    for marker in session["markers"]:
        expect(transcript).to_contain_text(marker)
    if "tool_error" in session["shapes"]:
        expect(reader.locator(".tool-summary.error").first).to_be_visible()
        expect(reader.locator(".tool-result.error").first).to_be_visible()
    if "long_output" in session["shapes"]:
        expect(reader.locator(".tool-result .truncated")).to_have_count(1)

    first = reader.locator(".tool-summary").first
    first.click()
    expect(first).to_have_attribute("aria-expanded", "false")
    expect(reader.locator(".tool-call").first.locator(".tool-body")).to_have_count(0)


SUBAGENTS = [(s, a) for s in SESSIONS for a in s["subagents"]]


@pytest.mark.parametrize(
    ("session", "subagent"), SUBAGENTS, ids=[a["agent_id"] for _, a in SUBAGENTS]
)
def test_every_subagent_link_opens_its_transcript(
    reader: Page, session: dict[str, Any], subagent: dict[str, Any]
) -> None:
    open_from_list(reader, session)
    reader.get_by_role("link", name=f"Subagent: {subagent['description']}", exact=True).click()
    expect(reader).to_have_url(
        f"{reader.url.split('#', 1)[0]}#/repos/{REPO}/sessions/claude/"
        f"{session['session_id']}/subagents/{subagent['agent_id']}"
    )
    expect(reader.get_by_text("Loading…", exact=True)).to_have_count(0)
    transcript = reader.locator("article.transcript")
    expect(transcript.locator(".entry.assistant").first).to_be_visible()
    expand_every_tool_call(reader)
    # The subagent's task is to read a toy file, so it holds that file's marker.
    for marker in session["markers"]:
        if marker.startswith("TOY-MARKER-"):
            expect(transcript).to_contain_text(marker)

    reader.get_by_role("link", name="Parent session").click()
    expect(reader.locator("h1", has_text=prompt_marker(session["prompts"][0]))).to_be_visible()
    expect(
        reader.get_by_role("link", name=f"Subagent: {subagent['description']}", exact=True)
    ).to_be_visible()


THINKING = [s for s in SESSIONS if "thinking" in s["shapes"]]


@pytest.mark.parametrize("session", THINKING, ids=[s["name"] for s in THINKING])
def test_thinking_follows_the_setting(reader: Page, session: dict[str, Any]) -> None:
    open_from_list(reader, session)
    thinking = reader.locator(".entry.thinking")
    expect(reader.locator(".entry.assistant").first).to_be_visible()
    expect(thinking).to_have_count(0)

    reader.get_by_role("link", name="Settings", exact=True).click()
    reader.get_by_role("checkbox", name="Show thinking").check()
    reader.go_back()
    expect(thinking.first).to_be_visible()

    reader.get_by_role("link", name="Settings", exact=True).click()
    reader.get_by_role("checkbox", name="Show thinking").uncheck()
    reader.go_back()
    expect(reader.locator(".entry.assistant").first).to_be_visible()
    expect(thinking).to_have_count(0)


def test_the_manifest_names_every_recorded_transcript() -> None:
    """A recording the manifest misses would silently go unwalked."""
    project = CORPUS / "projects" / "-work-toy"
    assert {p.stem for p in project.glob("*.jsonl")} == {s["session_id"] for s in SESSIONS}
    assert {p.stem.removeprefix("agent-") for p in project.glob("*/subagents/*.jsonl")} == {
        a["agent_id"] for _, a in SUBAGENTS
    }
