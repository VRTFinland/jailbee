"""Questions follow the action until the user moves on."""

import asyncio
from threading import Event

import pytest

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


def test_question_arriving_during_blocking_handoff_opens_queue_not_modal(mocker, tmp_path):
    release = Event()
    arriving = Event()
    ready = Event()
    answers = []

    def run(argv, *, on_prompt, **kwargs):
        ready.set()
        assert release.wait(5)
        answers.append(on_prompt(Prompt(1, "text", "Name?")))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.action_runner = run
    app.threaded_actions = True

    call_from_thread = app.call_from_thread

    def relay(*args, **kwargs):
        if app.session.handed_off:
            arriving.set()
        return call_from_thread(*args, **kwargs)

    mocker.patch.object(app, "call_from_thread", side_effect=relay)

    def terminal_command():
        release.set()
        assert arriving.wait(5)
        return 0

    async def scenario():
        async with app.run_test() as pilot:
            try:
                app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
                assert await asyncio.to_thread(ready.wait, 5)
                assert app.hand_off(terminal_command) == 0
                async with asyncio.timeout(5):
                    while not any(e.pending_question for e in app.session.queue):
                        await pilot.pause(0.01)
                assert not app.modal_open
                assert app.session.overlay is not None
                assert app.session.overlay.purpose == "queue"
                await pilot.press("enter")
                async with asyncio.timeout(5):
                    while not isinstance(app.screen, pm.TextModal):
                        await pilot.pause(0.01)
                await pilot.press("x", "enter")
                async with asyncio.timeout(5):
                    while not answers:
                        await pilot.pause(0.01)
                assert answers == ["x"]
            finally:
                release.set()
                app.exit()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"dispatch_interaction": None}, False),
        ({"dispatch_interaction": 6}, False),
        ({"interaction": 8}, False),
        ({"dispatch_interaction": 6, "answer_interaction": 7}, True),
        ({"dispatch_interaction": None, "answer_interaction": 6}, False),
        ({"overlay": "queue"}, True),
        ({"overlay": "other"}, False),
        ({"on_top": False}, False),
        ({"suspended": True}, False),
        ({"critical": True, "foreground": True, "dispatch_interaction": None}, True),
        ({"critical": True, "foreground": True, "overlay": "other"}, True),
        ({"critical": True, "foreground": True, "on_top": False}, False),
        ({"critical": True, "foreground": True, "suspended": True}, False),
        ({"critical": False, "foreground": True}, False),
        ({"critical": True, "foreground": False, "dispatch_interaction": None}, False),
        ({"interaction": 0, "dispatch_interaction": 0}, True),
    ],
)
def test_routing_uses_visibility_suspension_and_interaction_epochs(changes, expected):
    from jailbee.dashboard.notifications import should_show_question

    inputs = dict(
        critical=False,
        foreground=False,
        on_top=True,
        overlay="none",
        dispatch_interaction=7,
        answer_interaction=None,
        interaction=7,
        suspended=False,
    )
    assert should_show_question(**(inputs | changes)) is expected


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
