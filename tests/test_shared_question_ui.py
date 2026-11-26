"""Shared questions reach dashboards through the state-client interface."""

import asyncio

from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.prompt_channel import Prompt
from jailbee.state_service.protocol import ActionEntry, ActionQuestion, ActionQuestionResolved
from tests.dashboard_pilot import make_app


def test_observer_sees_pending_question_and_resolution_closes_modal(mocker, tmp_path):
    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])
    client = app.session.client
    client._action_entries[42] = ActionEntry(42, ["destroy", "alpha"], str(tmp_path), "alpha", False, "other", "none", str(tmp_path), "alpha")
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
