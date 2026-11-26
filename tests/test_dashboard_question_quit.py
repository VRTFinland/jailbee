"""Dashboard detach leaves service-hosted questions available to another dashboard."""

import asyncio
from threading import Event

import pytest

from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.runner import RunResult
from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.prompt_channel import Prompt
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import burst, make_app


def test_quit_detaches_without_answering_and_another_dashboard_can_answer(mocker, tmp_path):
    groups = [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])]
    app = make_app(mocker, groups)
    client = app.session.client
    app.threaded_actions = True
    answers = []
    done = Event()
    release = Event()

    def run(argv, *, on_prompt, **kwargs):
        assert release.wait(5)
        answers.append(on_prompt(Prompt(1, "confirm", "Proceed?")))
        done.set()
        return RunResult(status=0)

    client.action_runner = run

    async def scenario():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
            await pilot.press("down")
            release.set()
            async with asyncio.timeout(5):
                while not any(e.pending_question for e in app.session.queue):
                    app.refresh_frame()
                    await pilot.pause(0.01)
            await pilot.press("q")
            assert app.quit_requested
            assert not answers
            assert client.action_questions()
        assert not done.is_set()
        second = make_app(mocker, groups, client=client)
        async with second.run_test() as pilot:
            second.refresh_frame()
            await pilot.press("Q", "enter")
            assert isinstance(second.screen, pm.ConfirmModal)
            await pilot.press("y")
            assert await asyncio.to_thread(done.wait, 5)
            assert answers == [True]

    try:
        asyncio.run(scenario())
    finally:
        client.release_test_questions()


@pytest.mark.parametrize("keys", ["nyy", "nnn", "yyy", "yny"])
def test_shared_confirmation_keeps_first_answer_in_unpaced_burst(mocker, tmp_path, keys):
    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    client = app.session.client
    answers = []
    done = Event()
    release = Event()

    def run(argv, *, on_prompt, **kwargs):
        assert release.wait(5)
        answers.append(on_prompt(Prompt(1, "confirm", "Proceed?")))
        done.set()
        return RunResult(status=0)

    client.action_runner = run

    async def scenario():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
            await pilot.press("down")
            release.set()
            async with asyncio.timeout(5):
                while not any(e.pending_question for e in app.session.queue):
                    app.refresh_frame()
                    await pilot.pause(0.01)
            await pilot.press("Q", "enter")
            assert isinstance(app.screen, pm.ConfirmModal)
            burst(*[(key, key) for key in keys])(app)
            assert await asyncio.to_thread(done.wait, 5)
            await pilot.pause()
            assert answers == [keys[0] == "y"]

    try:
        asyncio.run(scenario())
    finally:
        client.release_test_questions()


def test_quit_without_questions_does_not_confirm(mocker, tmp_path):
    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("q")
            assert app.quit_requested
            assert not isinstance(app.screen, pm.ConfirmModal)

    asyncio.run(scenario())
