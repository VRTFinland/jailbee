"""Send messages through the real web route and a mocked Incus boundary."""

from __future__ import annotations

import json
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
from tests_e2e.test_session_reading import SESSION, TITLE, open_transcript, record


@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_composer_delivery_and_refusal(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    home = tmp_path / "home"
    project = home / "projects" / "-work-repo"
    project.mkdir(parents=True)
    transcript = project / f"{SESSION}.jsonl"
    transcript.write_text(record(0, "user", TITLE))
    live = tmp_path / "live" / "sessions"
    live.mkdir(parents=True)
    (live / "session.json").write_text(
        json.dumps(
            {
                "pid": 42,
                "procStart": 123,
                "status": "idle",
                "sessionId": SESSION,
            }
        )
    )
    adapter = get_adapter("claude")
    monkeypatch.setattr(type(adapter), "session_home", lambda self, cfg, name: live.parent)
    cfg = Config.model_validate(resolve_agents_raw({"agents": {"claude": {"enabled": True}}}))
    monkeypatch.setattr("jailbee.web.chat.load_repo_config", lambda path: cfg)
    incus = Mock()
    incus.list_containers.return_value = [{"name": "work-1", "status": "Running"}]
    busy = False

    def execute(name: str, command: list[str]) -> str:
        cmd = " ".join(command)
        if "python3" in cmd:
            return "42 10 123 S 42 42 5\n10 1 22 S 10 42 5\n"
        if "list-panes" in cmd:
            return "" if busy else "%1 10\n"
        if "capture-pane" in cmd:
            return "────────\n> occupied\n────────\n" if busy else "────────\n> \n────────\n"
        return ""

    incus.exec.side_effect = execute
    homes = {"work": [("claude", home)]}
    server = make_server(agent_chat=True, incus_factory=lambda: incus, agent_homes=homes.get)
    summary = AgentSummary("claude", "busy", None, None, 1, session_ids=(SESSION,))
    server.push(snapshot_of(repo_group("work", container("work-1", agent_status=(summary,)))))
    page.set_viewport_size({"width": 390, "height": 800})
    log_in_with_pin(page, server.url, server.pin.current)
    page.evaluate(
        """profile => {
        localStorage.setItem('jailbee.displayProfile', profile);
        localStorage.setItem('jailbee.composerExpanded', '1');
    }""",
        profile,
    )
    page.goto(f"{server.url}/#/repos/work/sessions/claude/{SESSION}")
    page.reload()
    field = page.get_by_role("textbox", name="Message agent")
    expect(field).to_be_enabled()
    wait_until(lambda: len(server.watches.open) == 1)
    message = "  **bold**\nsecond\tline  "
    field.fill("first")
    field.press("Shift+Enter")
    expect(field).to_have_value("first\n")
    field.fill("firstsecond")
    field.evaluate("el => el.setSelectionRange(5, 5)")
    page.get_by_role("button", name="Insert newline", exact=True).click()
    expect(field).to_have_value("first\nsecond")
    expect(field).to_be_focused()
    assert field.get_attribute("enterkeyhint") == "send"
    field.fill(message)
    composer_box = page.locator(".agent-composer").bounding_box()
    field.press("Enter")
    expect(page.get_by_role("status")).to_have_text("Waiting for transcript")
    assert page.locator(".agent-composer").bounding_box() == composer_box
    assert incus.exec_with_input.call_args.args[2] == message
    with transcript.open("a") as stream:
        stream.write(record(1, "user", message.replace("\t", "    ").strip()))
    server.watches.open[-1].fire()
    expect(page.get_by_role("status")).to_have_text("Delivered")
    expect(field).to_have_value("")

    # Collapsed controls retain the same reserved geometry for every status.
    page.get_by_role("button", name="Hide composer").click()
    collapsed_box = page.locator(".agent-composer").bounding_box()
    assert collapsed_box is not None
    page.get_by_role("button", name="Message agent", exact=True).click()
    # A dialog opening after availability was checked refuses without any terminal write.
    writes = incus.exec_with_input.call_count
    calls = len(incus.exec.call_args_list)
    (live / "session.json").write_text(
        json.dumps(
            {
                "pid": 42,
                "procStart": 123,
                "status": "waiting",
                "waitingFor": "permission",
                "sessionId": SESSION,
            }
        )
    )
    field.fill("dialog response")
    field.press("Enter")
    expect(page.get_by_role("status")).to_have_text(
        "The agent is waiting at a dialog in the terminal; answer it there first."
    )
    expect(field).to_have_value("dialog response")
    assert incus.exec_with_input.call_count == writes
    assert not any(
        "paste-buffer" in str(call) or "send-keys" in str(call)
        for call in incus.exec.call_args_list[calls:]
    )
    expect(field).to_be_enabled()
    page.get_by_role("button", name="Hide composer").click()
    assert page.locator(".agent-composer").bounding_box() == collapsed_box
    page.get_by_role("button", name="Message agent", exact=True).click()
    (live / "session.json").write_text(
        json.dumps(
            {
                "pid": 42,
                "procStart": 123,
                "status": "idle",
                "sessionId": SESSION,
            }
        )
    )

    # Slash commands may produce only metadata; reloading must release the gate.
    field.fill("/compact")
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_role("status")).to_have_text("Waiting for transcript")
    expect(field).to_be_disabled()
    page.reload()
    expect(field).to_be_enabled()
    field.fill("Keep me")

    def refuse_on_post(route: Route) -> None:
        nonlocal busy
        busy = True
        route.continue_()

    page.route("**/messages", refuse_on_post)
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_text("No pane", exact=True)).to_be_visible()
    expect(field).to_have_value("Keep me")
    composer = page.locator(".agent-composer")
    box = composer.bounding_box()
    assert box is not None and box["y"] >= 0
    if profile == "eink":
        box = composer.bounding_box()
        nav = page.locator(".page-nav").bounding_box()
        assert box is not None and nav is not None
        assert box["y"] + box["height"] <= nav["y"] or box["x"] + box["width"] <= nav["x"]


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_composer_choice_and_focus(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    width: int,
    profile: str,
) -> None:
    home = tmp_path / "home"
    project = home / "projects" / "-work-repo"
    project.mkdir(parents=True)
    (project / f"{SESSION}.jsonl").write_text(record(0, "user", TITLE))
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
    incus.exec.side_effect = lambda name, command: (
        "42 10 123 S 42 42 5\n10 1 22 S 10 42 5\n" if "python3" in command else "%1 10\n"
    )
    homes = {"work": [("claude", home)]}
    server = make_server(agent_chat=True, incus_factory=lambda: incus, agent_homes=homes.get)
    summary = AgentSummary("claude", "busy", None, None, 1, session_ids=(SESSION,))
    server.push(snapshot_of(repo_group("work", container("work-1", agent_status=(summary,)))))
    page.set_viewport_size({"width": width, "height": 800})
    log_in_with_pin(page, server.url, server.pin.current)
    page.goto(f"{server.url}/#/repos/work/sessions/claude/{SESSION}")
    page.evaluate(
        """profile => {
        localStorage.setItem('jailbee.displayProfile', profile);
        localStorage.removeItem('jailbee.composerExpanded');
    }""",
        profile,
    )
    page.reload()
    field = page.get_by_role("textbox", name="Message agent")
    if width == 390:
        expect(field).to_have_count(0)
        page.get_by_role("button", name="Message agent", exact=True).click()
        expect(field).to_be_focused()
    else:
        expect(field).to_be_visible()
    field.fill("Retained draft")
    page.get_by_role("button", name="Hide composer").click()
    expect(field).to_have_count(0)
    page.get_by_role("button", name="Message agent", exact=True).click()
    expect(field).to_have_value("Retained draft")
    page.get_by_role("button", name="Hide composer").click()
    page.reload()
    expect(field).to_have_count(0)
    page.get_by_role("button", name="Message agent", exact=True).click()
    expect(field).to_be_focused()


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_reader_end_remains_above_composer(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    width: int,
    profile: str,
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    open_transcript(page, make_server, tmp_path)
    page.evaluate("profile => localStorage.setItem('jailbee.displayProfile', profile)", profile)
    page.reload()
    if width == 390:
        page.get_by_role("button", name="Message agent", exact=True).click()
    expect(page.get_by_role("textbox", name="Message agent")).to_be_visible()
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    page.wait_for_function("""() => {
        const entry = document.querySelector('.entry.assistant:last-child').getBoundingClientRect();
        const composer = document.querySelector('.agent-composer').getBoundingClientRect();
        return entry.bottom <= composer.top + 2;
    }""")


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("profile", ["normal", "eink"])
def test_composer_explains_disabled_permission(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
    width: int,
    profile: str,
) -> None:
    home = tmp_path / "home"
    project = home / "projects" / "-work-repo"
    project.mkdir(parents=True)
    transcript = project / f"{SESSION}.jsonl"
    question = "❓ **Q1** - **Deploy**: Choose.\na) Canary\n➡️ a: Safe"
    transcript.write_text(record(0, "user", TITLE) + record(1, "assistant", question))
    homes = {"work": [("claude", home)]}
    server = make_server(agent_chat=False, agent_homes=homes.get)
    server.push(snapshot_of())
    page.set_viewport_size({"width": width, "height": 800})
    log_in_with_pin(page, server.url, server.pin.current)
    page.evaluate(
        """profile => {
        localStorage.setItem('jailbee.displayProfile', profile);
        localStorage.setItem('jailbee.einkFollow', '1');
    }""",
        profile,
    )
    page.goto(f"{server.url}/#/repos/work/sessions/claude/{SESSION}")
    expect(page.get_by_text("Agent chat disabled", exact=True)).to_have_count(0)
    expect(page.get_by_text("Deploy", exact=False).first).to_be_visible()
    expect(page.get_by_role("textbox", name="Message agent")).to_have_count(0)
    expect(page.get_by_role("button", name="Answer questions")).to_have_count(0)
    wait_until(lambda: len(server.watches.open) == 1)
    with transcript.open("a") as stream:
        stream.write(record(2, "assistant", "❓ **Q2** - **Release**: Continue?\na) Yes"))
    server.watches.open[-1].fire()
    expect(page.get_by_text("Release", exact=False).first).to_be_visible()
    expect(page.get_by_text("Agent chat disabled", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Answer questions")).to_have_count(0)
