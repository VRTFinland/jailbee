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
from tests.conftest import make_cfg
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
    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    monkeypatch.setattr("jailbee.web.chat.load_repo_config", lambda path: cfg)
    incus = Mock()
    incus.list_containers.return_value = [{"name": "work-1", "status": "Running"}]
    busy = False

    def execute(name: str, command: list[str]) -> str:
        cmd = " ".join(command)
        if "python3" in cmd:
            return "42 10 123\n10 1 22\n"
        if "list-panes" in cmd:
            return "%1 10\n"
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
    page.evaluate("profile => localStorage.setItem('jailbee.displayProfile', profile)", profile)
    page.goto(f"{server.url}/#/repos/work/sessions/claude/{SESSION}")
    page.reload()
    field = page.get_by_role("textbox", name="Message agent")
    expect(field).to_be_enabled()
    wait_until(lambda: len(server.watches.open) == 1)
    message = "**bold**\nsecond line"
    field.fill(message)
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_role("status")).to_have_text("Waiting for transcript")
    assert incus.exec_with_input.call_args.args[2] == message
    with transcript.open("a") as stream:
        stream.write(record(1, "user", message))
    server.watches.open[-1].fire()
    expect(page.get_by_role("status")).to_have_text("Delivered")
    expect(field).to_have_value("")
    field.fill("Keep me")

    def refuse_on_post(route: Route) -> None:
        nonlocal busy
        busy = True
        route.continue_()

    page.route("**/messages", refuse_on_post)
    page.get_by_role("button", name="Send message").click()
    expect(page.get_by_text("Unsent terminal input or unrecognised prompt")).to_be_visible()
    expect(field).to_have_value("Keep me")
    composer = page.locator(".agent-composer")
    box = composer.bounding_box()
    assert box is not None and box["y"] >= 0
    if profile == "eink":
        box = composer.bounding_box()
        nav = page.locator(".page-nav").bounding_box()
        assert box is not None and nav is not None
        assert box["y"] + box["height"] <= nav["y"]


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
    expect(page.get_by_role("textbox", name="Message agent")).to_be_visible()
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    page.wait_for_function("""() => {
        const entry = document.querySelector('.entry.assistant:last-child').getBoundingClientRect();
        const composer = document.querySelector('.agent-composer').getBoundingClientRect();
        return entry.bottom <= composer.top + 2;
    }""")


def test_composer_explains_disabled_permission(
    page: Page,
    make_server: Callable[..., E2EServer],
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    project = home / "projects" / "-work-repo"
    project.mkdir(parents=True)
    (project / f"{SESSION}.jsonl").write_text(record(0, "user", TITLE))
    homes = {"work": [("claude", home)]}
    server = make_server(agent_homes=homes.get)
    server.push(snapshot_of())
    log_in_with_pin(page, server.url, server.pin.current)
    page.goto(f"{server.url}/#/repos/work/sessions/claude/{SESSION}")
    expect(page.get_by_role("textbox", name="Message agent")).to_be_disabled()
    expect(page.get_by_text("Agent chat is disabled")).to_be_visible()
