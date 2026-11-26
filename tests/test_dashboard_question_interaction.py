"""Questions follow the action until the user moves on."""

import asyncio
from threading import Event

from jailbee.dashboard import runner as drunner
from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.prompt_channel import Prompt
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import make_app


def test_question_opens_after_dispatch_without_interaction(mocker, tmp_path):
    def run(argv, *, on_prompt, **kwargs):
        on_prompt(Prompt(1, "text", "Name?"))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.action_runner = run
    app.threaded_actions = True

    async def scenario():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
            async with asyncio.timeout(5):
                while not isinstance(app.screen, pm.TextModal):
                    await pilot.pause(0.01)

    asyncio.run(scenario())


def test_key_after_dispatch_defers_question(mocker, tmp_path):
    release = Event()

    def run(argv, *, on_prompt, **kwargs):
        assert release.wait(5)
        on_prompt(Prompt(1, "text", "Name?"))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.action_runner = run
    app.threaded_actions = True

    async def scenario():
        async with app.run_test() as pilot:
            try:
                app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
                await pilot.press("down")
                release.set()
                async with asyncio.timeout(5):
                    while not any(e.pending_question for e in app.session.queue):
                        await pilot.pause(0.01)
                assert len(app.screen_stack) == 1
            finally:
                release.set()

    asyncio.run(scenario())


def test_followup_opens_after_answer_and_timer_does_not_disarm(mocker, tmp_path):
    release = Event()
    answers = []

    def run(argv, *, on_prompt, **kwargs):
        assert release.wait(5)
        answers.append(on_prompt(Prompt(1, "text", "First?")))
        answers.append(on_prompt(Prompt(2, "text", "Second?")))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.action_runner = run
    app.threaded_actions = True

    async def scenario():
        async with app.run_test() as pilot:
            try:
                app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
                app._timer_tick()
                release.set()
                async with asyncio.timeout(5):
                    while not isinstance(app.screen, pm.TextModal):
                        await pilot.pause(0.01)
                await pilot.press("a", "enter")
                async with asyncio.timeout(5):
                    while not (isinstance(app.screen, pm.TextModal) and answers):
                        await pilot.pause(0.01)
                await pilot.press("b", "enter")
                async with asyncio.timeout(5):
                    while len(answers) < 2:
                        await pilot.pause(0.01)
                assert answers == ["a", "b"]
            finally:
                release.set()

    asyncio.run(scenario())


def test_routing_inputs_are_independent():
    from jailbee.dashboard.notifications import should_show_question

    assert not should_show_question(critical_foreground=False)
    assert should_show_question(critical_foreground=True)
    assert should_show_question(critical_foreground=False, since_dispatch=True)
    assert should_show_question(critical_foreground=False, since_answer=True)


def test_click_on_dashboard_disarms_action(mocker, tmp_path):
    release = Event()

    def run(argv, *, on_prompt, **kwargs):
        assert release.wait(5)
        on_prompt(Prompt(1, "text", "Name?"))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.action_runner = run
    app.threaded_actions = True

    async def scenario():
        async with app.run_test() as pilot:
            try:
                app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
                await pilot.click("#frame", offset=(2, 2))
                release.set()
                async with asyncio.timeout(5):
                    while not any(e.pending_question for e in app.session.queue):
                        await pilot.pause(0.01)
                assert not app.modal_open
            finally:
                release.set()

    asyncio.run(scenario())
