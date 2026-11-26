"""Quit behavior for commands waiting for dashboard answers."""

import asyncio
from threading import Event

import pytest

from jailbee.dashboard import runner as drunner
from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.prompt_channel import Prompt
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import burst, make_app


def test_quit_confirms_and_cancels_pending_command(mocker, tmp_path):
    answers = []
    release = Event()

    def run(argv, *, on_prompt, **kwargs):
        assert release.wait(5)
        answers.append(on_prompt(Prompt(1, "text", "Name?")))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.action_runner = run
    app.threaded_actions = True

    async def scenario():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
            await pilot.press("down")
            release.set()
            async with asyncio.timeout(5):
                while not any(e.pending_question for e in app.session.queue):
                    await pilot.pause(0.01)
            await pilot.press("q")
            assert isinstance(app.screen, pm.ConfirmModal)
            assert "1 command" in app.screen.prompt.question
            await pilot.press("n")
            assert not answers
            assert any(e.pending_question for e in app.session.queue)
            await pilot.press("ctrl+c")
            assert isinstance(app.screen, pm.ConfirmModal)
            await pilot.press("y")
            async with asyncio.timeout(5):
                while not answers:
                    await asyncio.sleep(0.01)
            assert answers == [None]

    asyncio.run(scenario())


@pytest.mark.parametrize("keys", ["nqy", "nnn", "yyy", "yny"])
def test_quit_confirmation_survives_unpaced_answer_bursts(mocker, tmp_path, keys):
    answers = []
    release = Event()

    def run(argv, *, on_prompt, **kwargs):
        assert release.wait(5)
        answers.append(on_prompt(Prompt(1, "text", "Name?")))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.action_runner = run
    app.threaded_actions = True

    async def scenario():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
            await pilot.press("down")
            release.set()
            async with asyncio.timeout(5):
                while not any(e.pending_question for e in app.session.queue):
                    await pilot.pause(0.01)
            await pilot.press("q")
            assert isinstance(app.screen, pm.ConfirmModal)
            # Pilot.press waits between keys; one terminal read does not.
            burst(*[(key, key) for key in keys])(app)
            await pilot.pause()
            # Keys already routed to the old confirmation may be discarded, but
            # must neither answer twice nor remove another screen.
            if not app.quit_requested:
                assert not answers
                assert any(e.pending_question for e in app.session.queue)
                if not isinstance(app.screen, pm.ConfirmModal):
                    await pilot.press("q")
                await pilot.press("y")
            async with asyncio.timeout(5):
                while not answers:
                    await asyncio.sleep(0.01)
            assert app.quit_requested
            assert answers == [None]

    asyncio.run(scenario())


def test_quit_without_questions_does_not_confirm(mocker, tmp_path):
    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("q")
            assert app.quit_requested
            assert not isinstance(app.screen, pm.ConfirmModal)

    asyncio.run(scenario())
