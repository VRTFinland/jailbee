"""Quit behavior for commands waiting for dashboard answers."""

import asyncio

from jailbee.dashboard import runner as drunner
from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.prompt_channel import Prompt
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import make_app


def test_quit_confirms_and_cancels_pending_command(mocker, tmp_path):
    answers = []

    def run(argv, *, on_prompt, **kwargs):
        answers.append(on_prompt(Prompt(1, "text", "Name?")))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.action_runner = run
    app.threaded_actions = True

    async def scenario():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
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


def test_quit_without_questions_does_not_confirm(mocker, tmp_path):
    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.press("q")
            assert app.quit_requested
            assert not isinstance(app.screen, pm.ConfirmModal)

    asyncio.run(scenario())


def test_question_during_handoff_is_deferred(mocker, tmp_path):
    from tests.dashboard_pilot import bare_session

    session, terminal = bare_session(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])
    entry_id = session.queue.start("start alpha", "alpha", critical=True)
    session._modal_entry = entry_id
    before = session.queue.attention_items()
    session.begin_hand_off()
    assert not session.question_arrived(
        entry_id, Prompt(1, "text", "Name?"), on_top=True, queue_key="Q"
    )
    session.end_hand_off(before)
    assert session.queue.get(entry_id).pending_question is not None
    assert session.overlay is not None
    assert not getattr(terminal, "questions", [])
