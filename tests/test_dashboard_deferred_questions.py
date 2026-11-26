"""Deferred prompt-channel questions through the dashboard's public UI."""

from __future__ import annotations

import asyncio
from threading import Event

import pytest

from jailbee.dashboard import runner as drunner
from jailbee.dashboard.model import RepoGroup
from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.prompt_channel import Prompt
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import make_app


@pytest.mark.parametrize(
    "answer_keys, expected",
    [(["m", "a", "i", "n", "enter"], "main"), (["escape"], None), (["ctrl+c"], None)],
)
def test_background_question_waits_in_queue_until_selected(mocker, tmp_path, answer_keys, expected):
    asked = Event()
    release = Event()
    answers = []

    def run(argv, *, on_prompt, **kwargs):
        assert release.wait(5)
        asked.set()
        answers.append(on_prompt(Prompt(1, "text", "Branch name?")))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    app.action_runner = run

    async def scenario():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
            await pilot.press("down")
            release.set()
            async with asyncio.timeout(5):
                while not asked.is_set() or not any(
                    e.state == "awaiting" for e in app.session.queue
                ):
                    await pilot.pause(0.01)
            await pilot.pause()
            assert len(app.screen_stack) == 1
            entry = next(iter(app.session.queue))
            assert entry.pending_question.question == "Branch name?"
            assert app.session.view().queue.pending == 1
            assert app.session.view().queue.failed == 0
            assert "jailbee start alpha-a" in app.session.notice
            assert "Q" in app.session.notice
            assert answers == []
            await pilot.press("Q")
            from tests.dashboard_pilot import Pick, _apply

            await pilot.pause()
            await _apply(pilot, app, Pick(0))
            assert isinstance(app.screen, pm.TextModal)
            await pilot.press(*answer_keys)
            async with asyncio.timeout(5):
                while not answers:
                    await pilot.pause(0.01)
            assert answers == [expected]
            entry = next(iter(app.session.queue))
            assert entry.state != "awaiting"
            assert entry.pending_question is None

    asyncio.run(scenario())


@pytest.mark.parametrize("first", [0, 1])
def test_two_pending_questions_are_answered_independently(mocker, tmp_path, first):
    answers = {}
    release = Event()

    def run(argv, *, on_prompt, **kwargs):
        assert release.wait(5)
        answers[argv[0]] = on_prompt(Prompt(1, "text", f"Name for {argv[0]}?"))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    app.action_runner = run

    async def scenario():
        async with app.run_test() as pilot:
            for verb in ["start", "stop"]:
                app.session.run_dashboard_command("alpha-a", "container", [verb, "alpha-a"])
            await pilot.press("down")
            release.set()
            async with asyncio.timeout(5):
                while sum(e.pending_question is not None for e in app.session.queue) != 2:
                    await pilot.pause(0.01)
            assert len(app.screen_stack) == 1
            for row, verb, value in [
                (first, ["stop", "start"][first], "one"),
                (1 - first, ["start", "stop"][first], "two"),
            ]:
                await pilot.press("Q") if app.session.overlay is None else None
                await pilot.press("home", *(["down"] * row), "enter")
                assert isinstance(app.screen, pm.TextModal)
                await pilot.press(*value, "enter")
                async with asyncio.timeout(5):
                    while verb not in answers:
                        await pilot.pause(0.01)
                assert answers[verb] == value
            assert not any(e.pending_question is not None for e in app.session.queue)

    asyncio.run(scenario())


def test_only_the_critical_action_whose_modal_is_foreground_opens_its_question(mocker, tmp_path):
    answers = {}

    def run(argv, *, on_prompt, **kwargs):
        answers[argv[0]] = on_prompt(Prompt(1, "text", f"Name for {argv[0]}?"))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    app.action_runner = run

    async def scenario():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])
            async with asyncio.timeout(5):
                while not isinstance(app.screen, pm.TextModal):
                    await pilot.pause(0.01)
            await pilot.press(*"first", "enter")
            async with asyncio.timeout(5):
                while "merge" not in answers:
                    await pilot.pause(0.01)
            assert isinstance(app.screen, pm.OutputScreen)
            # A second critical entry is foreground-marked, but its output modal
            # is queued behind the first action: its question must not interrupt.
            app.session.run_dashboard_command("alpha-a", "container", ["git", "pull", "alpha-a"])
            async with asyncio.timeout(5):
                while not any(e.pending_question is not None for e in app.session.queue):
                    await pilot.pause(0.01)
            await pilot.pause()
            assert isinstance(app.screen, pm.OutputScreen)
            assert answers == {"merge": "first"}

    asyncio.run(scenario())


def test_critical_question_still_opens_after_another_live_output_was_closed(mocker, tmp_path):
    release_question = Event()
    answers = []

    def run(argv, *, on_prompt, **kwargs):
        assert release_question.wait(10)
        answers.append(on_prompt(Prompt(1, "text", "Critical question?")))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    app.action_runner = run

    async def scenario():
        async with app.run_test() as pilot:
            try:
                app.session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])
                await pilot.pause()
                critical_screen = app.screen
                app.show_output("Another action", "other output", failed=False, live=True)
                await pilot.pause()
                await pilot.press("escape")
                assert app.screen is critical_screen
                release_question.set()
                async with asyncio.timeout(5):
                    while not any(e.pending_question for e in app.session.queue):
                        await pilot.pause(0.01)
                await pilot.pause()
                assert isinstance(app.screen, pm.TextModal)
                await pilot.press(*"yes", "enter")
                async with asyncio.timeout(5):
                    while not answers:
                        await pilot.pause(0.01)
                assert answers == ["yes"]
            finally:
                release_question.set()

    asyncio.run(scenario())


def test_dead_child_clears_pending_question(mocker, tmp_path):
    import threading

    from jailbee.prompt_channel import CommandEnd

    close_child = Event()

    class Child:
        stdout = None

        def __init__(self, sock):
            self.thread = threading.Thread(target=self.run, args=(sock,))
            self.thread.start()

        def run(self, sock):
            end = CommandEnd(sock)
            # Send a prompt then close without waiting for its answer.
            end._ch.handshake()
            end._ch.send(Prompt(1, "text", "Waiting?").to_json())
            close_child.wait(5)
            end.close()

        def wait(self):
            self.thread.join(5)
            return 1

        def kill(self):
            close_child.set()

    def spawn(argv, *, cwd, env, prompt_sock):
        return Child(prompt_sock)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    app.action_runner = drunner.run_action
    app.action_spawn = spawn

    async def scenario():
        async with app.run_test() as pilot:
            try:
                app.session.run_dashboard_command("alpha-a", "container", ["start", "alpha-a"])
                async with asyncio.timeout(5):
                    while not any(e.pending_question for e in app.session.queue):
                        await pilot.pause(0.01)
                close_child.set()
                async with asyncio.timeout(5):
                    while not any(e.finished for e in app.session.queue):
                        await pilot.pause(0.01)
                entry = next(iter(app.session.queue))
                assert entry.state == "failed"
                assert entry.pending_question is None
            finally:
                close_child.set()
                app.exit()

    asyncio.run(scenario())


def test_critical_question_waits_when_another_screen_is_on_top(mocker, tmp_path):
    release = Event()

    def run(argv, *, on_prompt, **kwargs):
        assert release.wait(10)
        on_prompt(Prompt(1, "text", "Critical question?"))
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.action_runner = run
    app.threaded_actions = True

    async def scenario():
        async with app.run_test() as pilot:
            try:
                app.session.run_dashboard_command("alpha-a", "container", ["merge", "alpha-a"])
                await pilot.pause()
                app.show_output("Other", "Output", failed=False, live=True)
                await pilot.pause()
                other_screen = app.screen
                release.set()
                async with asyncio.timeout(5):
                    while not any(e.pending_question for e in app.session.queue):
                        await pilot.pause(0.01)
                assert app.screen is other_screen
            finally:
                release.set()
                app.exit()

    asyncio.run(scenario())


def test_exit_cancels_all_deferred_commands(mocker, tmp_path):
    answers = []
    finished = Event()

    def run(argv, *, on_prompt, **kwargs):
        answers.append(on_prompt(Prompt(1, "text", "Waiting?")))
        if len(answers) == 2:
            finished.set()
        return drunner.RunResult(status=0)

    app = make_app(mocker, [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])])
    app.threaded_actions = True
    app.action_runner = run

    async def scenario():
        async with app.run_test() as pilot:
            for verb in ["start", "stop"]:
                app.session.run_dashboard_command("alpha-a", "container", [verb, "alpha-a"])
            async with asyncio.timeout(5):
                while sum(e.pending_question is not None for e in app.session.queue) != 2:
                    await pilot.pause(0.01)
            app.exit()
        assert await asyncio.to_thread(finished.wait, 5)
        assert answers == [None, None]

    asyncio.run(scenario())


def test_batch_prompt_carries_entry_identity(tmp_path):
    seen = []

    class Progress:
        def entry_id(self, index):
            return [42, 77][index]

        def output(self, index, chunk):
            pass

        def question(self, index, asking):
            pass

    def run(argv, *, on_prompt, **kwargs):
        on_prompt(Prompt(1, "text", argv[0]))
        return drunner.RunResult(status=0)

    drunner.run_actions(
        [drunner.ActionRun([name], tmp_path) for name in ("first", "second")],
        run=run,
        progress=Progress(),
        on_entry_prompt=lambda entry_id, prompt: seen.append((entry_id, prompt.question)),
    )
    assert seen == [(42, "first"), (77, "second")]


def test_queue_cursor_stays_on_selected_question(mocker, tmp_path):
    from tests.dashboard_pilot import bare_session

    session, terminal = bare_session(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])
    first = session.queue.start("first", "alpha")
    session.queue.start("second", "alpha")
    session.queue.set_question(first, Prompt(1, "text", "Name?"))
    session.open_queue()
    picker = session.overlay
    session.picker_chosen(picker.entries[1])
    assert terminal.questions == [first]
    assert session.overlay.start_index == 1


def test_importing_question_core_does_not_load_textual():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import jailbee.dashboard.notifications; "
            "import jailbee.dashboard.tui.session; assert 'textual' not in sys.modules",
        ],
        check=True,
    )


def test_question_arrival_updates_queue_and_notice(mocker, tmp_path):
    from tests.dashboard_pilot import bare_session

    session, _ = bare_session(mocker, [RepoGroup("alpha", str(tmp_path), None, [])])
    entry_id = session.queue.start("jailbee start alpha", "alpha")
    prompt = Prompt(1, "text", "Name?")
    assert not session.question_arrived(entry_id, prompt, on_top=False, queue_key="N")
    assert session.queue.get(entry_id).pending_question == prompt
    assert "N" in session.notice
