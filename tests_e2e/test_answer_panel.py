"""Answer the latest assistant's questions through the real reader and send route."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import Page, Route, expect

from jailbee.accounts.adapters.base import get_adapter
from jailbee.agent_status import AgentSummary
from jailbee.config import Config, resolve_agents_raw
from tests.web_fakes import container, repo_group, snapshot_of, wait_until
from tests_e2e.flows import log_in_with_pin
from tests_e2e.server import E2EServer
from tests_e2e.test_session_reading import SESSION, TITLE, record

QUESTIONS = """This paragraph mentions Q1 and Q12 but is not either question.

```markdown
❓ **Q1** - **Example only**: Never highlight this code sample.
```

❓ **Q12** - **Deployment**: Choose a deployment.

a) Canary deployment
b) All at once
➡️ a: Safer rollout.

❓ **Q1** - **Owner**: Who should own this?

Keep this context visible while answering.
"""


def open_questions(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str = "normal",
    controls: str = "bar",
    latest: str = QUESTIONS,
) -> tuple[E2EServer, Path, Mock]:
    home = tmp_path / "home"
    project = home / "projects" / "-work-repo"
    project.mkdir(parents=True)
    transcript = project / f"{SESSION}.jsonl"
    transcript.write_text(
        record(0, "user", TITLE)
        + record(1, "assistant", QUESTIONS)
        + record(2, "assistant", "Earlier reasoning.\n\n" * 40 + latest)
    )
    live = tmp_path / "live" / "sessions"
    live.mkdir(parents=True)
    (live / "session.json").write_text(
        json.dumps({"pid": 42, "procStart": 123, "status": "idle", "sessionId": SESSION})
    )
    adapter = get_adapter("claude")
    monkeypatch.setattr(type(adapter), "session_home", lambda self, cfg, name: live.parent)
    cfg = Config.model_validate(resolve_agents_raw({"agents": {"claude": {"enabled": True}}}))
    monkeypatch.setattr("jailbee.web.chat.load_repo_config", lambda path: cfg)
    incus = Mock()
    incus.list_containers.return_value = [{"name": "work-1", "status": "Running"}]

    def execute(name: str, command: list[str]) -> str:
        cmd = " ".join(command)
        if "python3" in cmd:
            return "42 10 123 S 42 42 5\n10 1 22 S 10 42 5\n"
        if "list-panes" in cmd:
            return "%1 10 1\n"
        if "capture-pane" in cmd:
            return "────────\n> \n────────\n"
        return ""

    incus.exec.side_effect = execute
    homes = {"work": [("claude", home)]}
    server = make_server(agent_chat=True, incus_factory=lambda: incus, agent_homes=homes.get)
    summary = AgentSummary("claude", "busy", None, None, 1, session_ids=(SESSION,))
    server.push(snapshot_of(repo_group("work", container("work-1", agent_status=(summary,)))))
    log_in_with_pin(page, server.url, server.pin.current)
    page.evaluate(
        """({profile, controls}) => {
            localStorage.setItem('jailbee.displayProfile', profile);
            localStorage.setItem('jailbee.pageControls', controls);
            localStorage.setItem('jailbee.theme', profile === 'eink' ? 'eink' : 'light');
        }""",
        {"profile": profile, "controls": controls},
    )
    page.goto(f"{server.url}/#/repos/work/sessions/claude/{SESSION}")
    page.reload()
    expect(page.get_by_role("textbox", name="Message agent")).to_be_enabled()
    wait_until(lambda: len(server.watches.open) == 1)
    return server, transcript, incus


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize(
    "profile,controls",
    [("normal", "bar"), ("eink", "bar"), ("eink", "side"), ("eink", "indicator")],
)
def test_question_jump_reserves_readable_space(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    profile: str,
    controls: str,
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    open_questions(page, make_server, tmp_path, monkeypatch, profile=profile, controls=controls)
    panel = page.get_by_role("complementary", name="Answer questions")
    panel.get_by_role("button", name="Expand answers", exact=True).click()
    panel.get_by_role("button", name="Show in conversation").click()
    expect(page.locator(".question-highlight").first).to_contain_text("Choose a deployment.")
    expect(page.locator(".question-highlight").last).to_contain_text("Safer rollout.")
    panel.get_by_role("button", name="Question 1", exact=True).click()
    panel.get_by_role("button", name="Show in conversation").click()
    highlighted = page.locator(".question-highlight").first
    expect(highlighted).to_contain_text("Who should own this?")
    expect(highlighted).not_to_contain_text("Deployment")
    expect(highlighted).not_to_contain_text("Example only")
    expect(highlighted).not_to_contain_text("This paragraph mentions")
    assert highlighted.evaluate("el => el.closest('.entry').id") == "entry-entry-2:0"
    for action in [None, "Expand answer panel", "Reduce answer panel", "Collapse answers"]:
        if action:
            panel.get_by_role("button", name=action, exact=True).click()
        if action != "Collapse answers":
            panel.get_by_role("button", name="Show in conversation").click()
        page.wait_for_function("""() => {
            const panel = document.querySelector('.answer-panel').getBoundingClientRect();
            const composer = document.querySelector('.agent-composer').getBoundingClientRect();
            const header = document.querySelector('.title-bar').getBoundingClientRect();
            const target = document.querySelector('.question-highlight').getBoundingClientRect();
            const article = document.querySelector('.transcript').getBoundingClientRect();
            const nav = document.querySelector('.page-nav')?.getBoundingClientRect();
            const separate = (a, b) => a.bottom <= b.top + 1 || b.bottom <= a.top + 1 ||
                a.right <= b.left + 1 || b.right <= a.left + 1;
            const bottom = panel.left < article.right ? panel.top : composer.top;
            return panel.top >= header.bottom && panel.bottom <= innerHeight &&
                bottom - header.bottom >= 150 && target.top >= header.bottom - 2 &&
                target.top < bottom && separate(panel, composer) &&
                (!nav || separate(panel, nav) && separate(composer, nav)) &&
                (innerWidth < 900 || article.right <= panel.left);
        }""")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_answers_share_composer_send_gate_and_confirm_in_transcript(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    page.set_viewport_size({"width": 390, "height": 800})
    server, transcript, incus = open_questions(
        page, make_server, tmp_path, monkeypatch, profile=profile
    )
    panel = page.get_by_role("complementary", name="Answer questions")
    panel.get_by_role("button", name="Expand answers", exact=True).click()
    expect(panel.get_by_role("radio", name=re.compile("Canary deployment"))).to_be_checked()
    expect(panel.get_by_role("button", name="Send answers")).to_have_count(0)
    panel.get_by_role("radio", name="b) All at once", exact=True).check()
    panel.get_by_role("textbox", name="Optional note").fill("after review")
    panel.get_by_role("button", name="Next step").click()
    panel.get_by_role("textbox", name="Answer", exact=True).fill("Operations")
    panel.get_by_role("button", name="Previous step").click()
    expect(panel.get_by_role("radio", name="b) All at once", exact=True)).to_be_checked()
    panel.get_by_role("button", name="Collapse answers", exact=True).click()
    panel.get_by_role("button", name="Expand answers", exact=True).click()
    expect(panel.get_by_role("textbox", name="Optional note")).to_have_value("after review")
    panel.get_by_role("button", name="Summary", exact=True).click()
    panel.get_by_role("button", name="Q1: Operations — Changed", exact=True).click()
    panel.get_by_role("textbox", name="Answer", exact=True).fill("Platform")
    panel.get_by_role("button", name="Next step").click()
    message = "Q12: b – after review\nQ1: Platform"  # noqa: RUF001 - Spec's literal separator.
    expect(panel.locator("pre")).to_have_text(message)
    composer = page.get_by_role("textbox", name="Message agent")
    composer.fill("Keep my free-text draft")
    panel.get_by_role("button", name="Send answers").click()
    expect(panel.get_by_role("status")).to_have_text("Waiting for transcript")
    expect(composer).to_be_disabled()
    expect(panel.get_by_role("button", name="Send answers")).to_be_disabled()
    assert incus.exec_with_input.call_count == 1
    assert incus.exec_with_input.call_args.args[2] == message
    with transcript.open("a") as stream:
        stream.write(record(3, "user", message))
    server.watches.open[-1].fire()
    expect(panel.get_by_role("status")).to_have_text("Delivered")
    expect(composer).to_be_enabled()
    expect(composer).to_have_value("Keep my free-text draft")
    if profile == "eink":
        page.get_by_role("button", name="1 new message ↓", exact=True).click()
    expect(page.locator(".entry.user").last).to_contain_text(message)
    composer.fill("From composer")
    page.get_by_role("button", name="Send message").click()
    expect(panel.get_by_role("button", name="Send answers")).to_be_disabled()
    wait_until(lambda: incus.exec_with_input.call_count == 2)


@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_panel_refusal_keeps_answers_and_latest_message_controls_scope(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    server, transcript, incus = open_questions(
        page, make_server, tmp_path, monkeypatch, profile=profile
    )
    panel = page.get_by_role("complementary", name="Answer questions")
    panel.get_by_role("button", name="Expand answers", exact=True).click()
    panel.get_by_role("button", name="Question 1", exact=True).click()
    panel.get_by_role("textbox", name="Answer", exact=True).fill("Keep this answer")
    panel.get_by_role("button", name="Summary", exact=True).click()
    original = incus.exec.side_effect

    def refuse(route: Route) -> None:
        def execute(name: str, command: list[str]) -> str:
            if "capture-pane" in " ".join(command):
                return "────────\n> occupied\n────────\n"
            return original(name, command)

        incus.exec.side_effect = execute
        route.continue_()

    page.route("**/messages", refuse)
    panel.get_by_role("button", name="Send answers").click()
    expect(panel.get_by_role("alert")).to_contain_text("Unsent terminal input")
    expect(panel.locator("pre")).to_contain_text("Q1: Keep this answer")
    assert incus.exec_with_input.call_count == 0
    with transcript.open("a") as stream:
        stream.write(record(3, "assistant", "A newer plain answer; no question set."))
    server.watches.open[-1].fire()
    if profile == "normal":
        expect(
            page.get_by_text("A newer plain answer; no question set.", exact=True)
        ).to_be_attached()
    expect(panel).to_have_count(0)
    expect(page.get_by_role("textbox", name="Message agent")).to_be_visible()


@pytest.mark.parametrize("latest", ["No questions in this answer.", "❓ **Q99** wrong format"])
def test_old_questions_never_resurface_when_latest_has_no_parseable_set(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    latest: str,
) -> None:
    open_questions(page, make_server, tmp_path, monkeypatch, latest=latest)
    expect(page.get_by_role("complementary", name="Answer questions")).to_have_count(0)
    expect(page.get_by_role("textbox", name="Message agent")).to_be_enabled()


def test_question_jump_handles_markdown_soft_breaks(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = (
        "Context before questions.\n❓ **Q12** - **Deploy**: Choose.\n"
        "a) Canary\n➡️ a: Safe\n❓ **Q1** - **Owner**: Who?"
    )
    open_questions(page, make_server, tmp_path, monkeypatch, latest=latest)
    panel = page.get_by_role("complementary", name="Answer questions")
    panel.get_by_role("button", name="Expand answers", exact=True).click()
    panel.get_by_role("button", name="Question 1", exact=True).click()
    panel.get_by_role("button", name="Show in conversation").click()
    expect(page.locator(".question-highlight")).to_have_text("❓ Q1 - Owner: Who?")
    expect(page.locator(".question-highlight")).to_be_in_viewport()
