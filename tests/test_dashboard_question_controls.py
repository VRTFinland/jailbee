"""Question controls for running dashboard commands."""

import asyncio
import threading

import pytest

from jailbee.dashboard import runner
from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.prompt_channel import Prompt
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import make_app


async def mounted_question(app, pilot, kind):
    async with asyncio.timeout(5):
        while not (isinstance(app.screen, kind) and app.screen.is_mounted):
            await pilot.pause(0.01)
    await pilot.pause()


@pytest.mark.parametrize("control", ["escape", "button"])
def test_escape_defers_a_question_and_it_can_be_reopened(mocker, tmp_path, control):
    answers = []

    def run(argv, *, on_prompt, **kwargs):
        answers.append(on_prompt(Prompt(1, "text", "Branch?")))
        return runner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    app.action_runner = run

    async def scenario():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])
            await mounted_question(app, pilot, pm.TextModal)
            if control == "button":
                await pilot.click("#later")
            else:
                await pilot.press("escape")
            async with asyncio.timeout(5):
                while not (isinstance(app.screen, pm.OutputScreen) and app.screen.is_mounted):
                    await pilot.pause(0.01)
            await pilot.pause()
            assert answers == []
            assert next(iter(app.session.queue)).state == "awaiting"
            await pilot.press("escape")  # close the foreground output
            async with asyncio.timeout(5):
                while app.modal_open:
                    await pilot.pause(0.01)
            await pilot.press("Q")
            async with asyncio.timeout(5):
                while app.session.overlay is None or app.session.overlay.purpose != "queue":
                    await pilot.pause(0.01)
            await pilot.pause()
            await pilot.press("enter")
            await mounted_question(app, pilot, pm.TextModal)
            await pilot.press(*"main", "enter")
            async with asyncio.timeout(5):
                while not answers:
                    await pilot.pause(0.01)
            assert answers == ["main"]

    asyncio.run(scenario())


@pytest.mark.parametrize("covered", [False, True])
def test_disconnect_closes_the_displayed_question(mocker, tmp_path, covered):
    from jailbee.prompt_channel import CommandEnd

    close = threading.Event()

    class Child:
        stdout = None

        def __init__(self, sock):
            self.thread = threading.Thread(target=self.run, args=(sock,))
            self.thread.start()

        def run(self, sock):
            end = CommandEnd(sock)
            end._ch.handshake()
            end._ch.send(Prompt(1, "text", "Waiting?").to_json())
            close.wait(5)
            end.close()

        def wait(self):
            self.thread.join(5)
            return 1

        def kill(self):
            close.set()

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    app.action_runner = runner.run_action
    app.action_spawn = lambda argv, *, cwd, env, prompt_sock: Child(prompt_sock)

    async def scenario():
        async with app.run_test() as pilot:
            try:
                app.session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])
                await mounted_question(app, pilot, pm.TextModal)
                question = app.screen
                if covered:
                    app.show_output("Other action", "Still reading", failed=False)
                    await mounted_question(app, pilot, pm.OutputScreen)
                    covering = app.screen
                close.set()
                async with asyncio.timeout(5):
                    while (
                        question in app.screen_stack or not next(iter(app.session.queue)).finished
                    ):
                        await pilot.pause(0.01)
                if covered:
                    assert app.screen is covering
                    await pilot.press("escape")
                    assert question not in app.screen_stack
                assert next(iter(app.session.queue)).pending_question is None
            finally:
                close.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("control", ["button", "ctrl+c"])
def test_abort_command_cancels_the_waiting_command(mocker, tmp_path, control):
    answers = []

    def run(argv, *, on_prompt, **kwargs):
        answers.append(on_prompt(Prompt(1, "confirm", "Continue?")))
        return runner.RunResult(status=1)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    app.action_runner = run

    async def scenario():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])
            await mounted_question(app, pilot, pm.ConfirmModal)
            if control == "button":
                await pilot.click("#abort-command")
            else:
                await pilot.press("ctrl+c")
            async with asyncio.timeout(5):
                while not next(iter(app.session.queue)).finished:
                    await pilot.pause(0.01)
            assert answers == [None]
            assert next(iter(app.session.queue)).state == "failed"

    asyncio.run(scenario())
