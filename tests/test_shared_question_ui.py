"""Shared questions reach dashboards through the state-client interface."""

import asyncio
from threading import Event

import pytest

from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.runner import RunResult
from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.prompt_channel import Prompt
from jailbee.state_service.protocol import ActionEntry, ActionQuestion, ActionQuestionResolved
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import make_app


def test_observer_sees_pending_question_and_resolution_closes_modal(mocker, tmp_path):
    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])
    client = app.session.client
    client._action_entries[42] = ActionEntry(
        42,
        ["destroy", "alpha"],
        str(tmp_path),
        "alpha",
        False,
        "other",
        "none",
        str(tmp_path),
        "alpha",
    )
    client._action_questions[42] = ActionQuestion(42, 7, Prompt(1, "text", "Destroy?"))

    async def scenario():
        async with app.run_test() as pilot:
            app.refresh_frame()
            assert len(app.screen_stack) == 1
            assert app.session.queue.get(42).pending_question.question == "Destroy?"
            await pilot.press("Q", "enter")
            assert isinstance(app.screen, pm.TextModal)
            client._action_questions.clear()
            client._question_resolutions.append(ActionQuestionResolved(42, 7, "other"))
            app.refresh_frame()
            await pilot.pause()
            assert not isinstance(app.screen, pm.TextModal)
            assert "answered in another dashboard" in app.session.notice

    asyncio.run(scenario())


@pytest.mark.parametrize("verb", ["destroy", "merge"])
def test_origin_receives_service_question_and_answers_child(mocker, tmp_path, verb):
    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    client = app.session.client
    answers = []
    finished = Event()

    def run(argv, *, on_prompt, **kwargs):
        answers.append(on_prompt(Prompt(1, "text", "Proceed?")))
        finished.set()
        return RunResult(status=0)

    client.action_runner = run

    async def scenario():
        async with app.run_test() as pilot:
            try:
                app.session.run_dashboard_command("alpha-a", "container", [verb, "alpha-a"])
                async with asyncio.timeout(5):
                    while not isinstance(app.screen, pm.TextModal):
                        await pilot.pause(0.01)
                assert client.action_questions()
                await pilot.press("y", "enter")
                assert await asyncio.to_thread(finished.wait, 5)
                assert answers == ["y"]
                assert client.action_requests[0].argv[:2] == [verb, "alpha-a"]
            finally:
                client.release_test_questions()

    asyncio.run(scenario())


def test_remote_question_is_read_only_without_abort(mocker, tmp_path):
    from textual.widgets import Button, Input

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])
    client = app.session.client
    client._action_entries[42] = ActionEntry(
        42,
        ["destroy", "alpha"],
        str(tmp_path),
        "alpha",
        False,
        "other",
        "none",
        str(tmp_path),
        "alpha",
    )
    client._action_questions[42] = ActionQuestion(
        42, 7, Prompt(1, "text", "Destroy?"), can_answer=False, reason="answer in a local dashboard"
    )

    async def scenario():
        async with app.run_test() as pilot:
            app.refresh_frame()
            await pilot.press("Q", "enter")
            assert "answer in a local dashboard" in str(
                app.screen.query_one("#readonly-hint").render()
            )
            assert not app.screen.query(Input)
            assert not app.screen.query("#abort-command")
            assert not any("Abort" in str(button.label) for button in app.screen.query(Button))
            client._action_questions.clear()
            client._question_resolutions.append(ActionQuestionResolved(42, 7, "other"))
            app.refresh_frame()
            await pilot.pause()
            assert not isinstance(app.screen, pm.ReadOnlyQuestion)

    asyncio.run(scenario())


def test_observer_critical_action_has_no_foreground_modal(mocker, tmp_path):
    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])
    client = app.session.client
    client._action_entries[42] = ActionEntry(
        42,
        ["merge", "alpha"],
        str(tmp_path),
        "alpha",
        True,
        "other",
        "none",
        str(tmp_path),
        "alpha",
    )
    client._action_questions[42] = ActionQuestion(42, 7, Prompt(1, "text", "Merge?"))

    async def scenario():
        async with app.run_test():
            app.refresh_frame()
            assert len(app.screen_stack) == 1
            assert app.session.queue.get(42).pending_question.question == "Merge?"
            assert not app.session.queue.get(42).foreground

    asyncio.run(scenario())


@pytest.mark.parametrize("covered", [False, True])
def test_resolution_closes_only_the_matching_question(mocker, tmp_path, covered):
    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])
    client = app.session.client
    client._action_entries[42] = ActionEntry(
        42,
        ["start", "alpha"],
        str(tmp_path),
        "alpha",
        False,
        "other",
        "none",
        str(tmp_path),
        "alpha",
    )
    client._action_questions[42] = ActionQuestion(42, 7, Prompt(1, "text", "First?"))

    async def scenario():
        async with app.run_test() as pilot:
            app.refresh_frame()
            await pilot.press("Q", "enter")
            assert isinstance(app.screen, pm.TextModal)
            if covered:
                app.show_output("Other", "Still open", failed=False)
                await pilot.pause()
                top = app.screen
            client._question_resolutions.append(ActionQuestionResolved(42, 7, "other"))
            client._action_questions[42] = ActionQuestion(42, 8, Prompt(2, "text", "Second?"))
            app.refresh_frame()
            await pilot.pause()
            if covered:
                assert app.screen is top
                assert len(app.screen_stack) == 2
                await pilot.press("escape")
            assert app.session.queue.get(42).pending_question.question == "Second?"
            app.show_question(42)
            await pilot.pause()
            assert isinstance(app.screen, pm.TextModal)
            assert app.screen.prompt.question == "Second?"
            assert "answered in another dashboard" in app.session.notice

    asyncio.run(scenario())


def test_disconnected_question_does_not_claim_answer_elsewhere(mocker, tmp_path):
    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])
    client = app.session.client
    client._action_entries[42] = ActionEntry(
        42, ["start"], str(tmp_path), "alpha", False, "other", "none", str(tmp_path), "alpha"
    )
    client._action_questions[42] = ActionQuestion(42, 7, Prompt(1, "text", "First?"))

    async def scenario():
        async with app.run_test() as pilot:
            app.refresh_frame()
            await pilot.press("Q", "enter")
            client._action_questions.clear()
            client._question_resolutions.append(ActionQuestionResolved(42, 7, None))
            app.refresh_frame()
            await pilot.pause()
            assert "answered in another dashboard" not in (app.session.notice or "")

    asyncio.run(scenario())
