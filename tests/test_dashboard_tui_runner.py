"""Non-attach actions run through the action runner; their questions are modals."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Input, OptionList

from jailbee.dashboard import model as dmodel
from jailbee.dashboard import runner as drunner
from jailbee.dashboard.tui import prompt_modals as pm
from jailbee.dashboard.tui import session as tsession
from jailbee.prompt_channel import Answer, Cancel, Prompt, WireOption
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import bare_session, keys, make_app
from tests.test_dashboard_runner import Spawns

pytestmark = pytest.mark.usefixtures("no_real_branch_listing")


def _group(tmp_path: Path) -> dmodel.RepoGroup:
    return dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])


# Generous on purpose: under ``pytest -n auto`` on a loaded host (load 40-70) one run of
# this file took minutes and a 5 s / 20 s ceiling timed out. These are condition waits,
# so a quick run never pays for the ceiling; it only bounds a real hang.
_WAIT_SECONDS = 60
_RUN_SECONDS = 180
_JOIN_SECONDS = 30


async def _until(pilot: Any, condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await pilot.pause(0.02)


async def _confirm_modal(pilot: Any, app: Any) -> None:
    # push_screen makes the screen current before mount finishes. Do not act
    # on it (or tear it down) until mount and its deferred focus have settled.
    await _until(
        pilot,
        lambda: isinstance(app.screen, pm.ConfirmModal) and app.screen.is_mounted,
        "the mounted modal",
    )
    await pilot.pause()


def test_quit_driver_waits_for_mounted_question_before_exiting(mocker, tmp_path):
    """Pin the real runner's current-but-still-mounting question before quit."""
    app = make_app(mocker, [_group(tmp_path)])
    app.threaded_actions = True
    app.session.client.action_runner = drunner.run_action
    spawn = Spawns([_prompt(default=False, destructive=True)])
    app.session.client.action_spawn = spawn

    async def main():
        mounting = asyncio.Event()
        release = asyncio.Event()
        exiting = asyncio.Event()
        mount = pm.ConfirmModal.on_mount

        async def held_mount(modal):
            mounting.set()
            await release.wait()
            mount(modal)

        mocker.patch.object(pm.ConfirmModal, "on_mount", held_mount)
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-x", "container", ["merge", "alpha-x"])
            await mounting.wait()

            async def quit_when_ready():
                await _confirm_modal(pilot, app)
                exiting.set()
                app.exit()

            quitting = asyncio.create_task(quit_when_ready())
            try:
                await asyncio.sleep(0)
                assert not exiting.is_set(), "quit must not tear down a mounting question"
                assert not quitting.done()
            finally:
                release.set()
                await quitting

    asyncio.run(asyncio.wait_for(main(), _RUN_SECONDS))
    assert not spawn.child.answers
    app.session.client.release_test_questions()
    spawn.child._thread.join(_JOIN_SECONDS)
    assert not spawn.child._thread.is_alive()


@pytest.mark.parametrize("kind", ["confirm", "choose_one", "choose_many", "text", "secret"])
def test_exit_while_prompt_is_mounting_does_not_report_an_exception(mocker, tmp_path, kind, caplog):
    app = make_app(mocker, [_group(tmp_path)])
    app.threaded_actions = True
    app.session.client.action_runner = drunner.run_action
    prompt = _prompt(
        kind=kind,
        default=False if kind == "confirm" else None,
        destructive=kind == "confirm",
        options=(WireOption("0", "first", "first", ""),) if "choose" in kind else (),
    )
    spawn = Spawns([prompt])
    app.session.client.action_spawn = spawn
    errors = mocker.spy(app, "_handle_exception")

    async def main():
        mounting = asyncio.Event()
        release = asyncio.Event()
        modal_type = type(pm.modal_for(prompt))
        mount = modal_type.on_mount
        held = None
        mounted = asyncio.Event()

        async def held_mount(modal):
            nonlocal held
            held = modal
            mounting.set()
            await release.wait()
            mount(modal)
            mounted.set()

        mocker.patch.object(modal_type, "on_mount", held_mount)
        async with app.run_test():
            app.session.run_dashboard_command("alpha-x", "container", ["start", "alpha-x"])
            await mounting.wait()
            try:
                app.exit()
                # Force pruning before releasing mount instead of relying on
                # Textual's shutdown scheduling to hit the race by chance.
                await held.remove_children()
            finally:
                release.set()

        assert mounted.is_set(), "the gated mount must finish, not just be cancelled"

    asyncio.run(asyncio.wait_for(main(), _RUN_SECONDS))
    errors.assert_not_called()
    assert not [record for record in caplog.records if record.levelno >= 40]
    assert not spawn.child.answers
    app.session.client.release_test_questions()
    spawn.child._thread.join(_JOIN_SECONDS)
    assert not spawn.child._thread.is_alive()


def _drive(
    mocker: Any,
    tmp_path: Path,
    spawn: Spawns,
    script: Callable[[Any, Any], Any],
) -> Any:
    """A real threaded runner over ``spawn``; ``script(app, pilot)`` plays the user."""
    app = make_app(mocker, [_group(tmp_path)])
    app.threaded_actions = True
    app.session.client.action_runner = drunner.run_action
    app.session.client.action_spawn = spawn

    async def main() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            for key in ["j", "!", *keys("merge"), "enter"]:  # the inline command line
                await pilot.press(key)
            await script(app, pilot)

    asyncio.run(asyncio.wait_for(main(), _RUN_SECONDS))
    return app


def test_until_tolerates_a_stalled_event_loop(mocker):
    """A starved host: each pause takes 1 s of wall time; 10 s must not time out."""
    clock = [0.0]
    mocker.patch.object(time, "monotonic", lambda: clock[0])

    class SlowPilot:
        async def pause(self, delay: float) -> None:
            clock[0] += 1.0

    asyncio.run(_until(SlowPilot(), lambda: clock[0] >= 10, "a slow thing"))
    with pytest.raises(AssertionError, match="timed out waiting for never"):
        asyncio.run(_until(SlowPilot(), lambda: False, "never"))


def _finished(app: Any) -> bool:
    """Whether the one action's queue entry has finished."""
    return any(entry.finished for entry in app.session.queue)


def _prompt(**kw: Any) -> Prompt:
    return Prompt(id=0, **{"kind": "confirm", "question": "Go?", **kw})


def test_prompt_during_action_is_a_modal_and_the_answer_reaches_the_child(mocker, tmp_path):
    spawn = Spawns([_prompt(default=True)])

    async def script(app, pilot):
        await _confirm_modal(pilot, app)
        assert app.session.terminal is app
        choices = app.screen.query_one(OptionList)
        assert [str(choices.get_option_at_index(i).prompt) for i in range(2)] == ["No", "Yes"]
        assert choices.highlighted == 1
        await pilot.press("up", "down", "enter")
        await _until(pilot, lambda: _finished(app), "the finished entry")
        assert isinstance(app.screen, pm.OutputScreen)  # merge prints: its output opens
        (entry,) = app.session.queue
        assert "alku ä" in entry.output  # what the child printed is in its queue entry
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)

    (answer,) = spawn.child.answers
    assert isinstance(answer, Answer)
    assert answer.value is True
    assert spawn.calls[0]["argv"][1:3] == ["-m", "jailbee"]
    assert spawn.calls[0]["argv"][-2:] == ["merge", "alpha-x"]


@pytest.mark.parametrize("kind", ["pr", "issue", "source"])
@pytest.mark.parametrize("transport", ["none", "ssh"])
def test_outbox_publish_uses_runner_modal_and_keeps_queue_result(mocker, tmp_path, kind, transport):
    from jailbee.dashboard.accounts import CliResult
    from tests.test_dashboard_outbox_flow import _listing, _proposal

    row = _proposal(f"{kind if kind != 'source' else 'pr'}/a.json")
    if kind == "source":
        row.update(state="awaiting-push", create_scope={"kind": "source", "manifest": "a.json"})
    mocker.patch(
        "jailbee.dashboard.accounts.run_cli_quiet", return_value=CliResult(True, "", _listing(row))
    )
    from jailbee.config.models_remote import RemoteSSHConfig

    app = make_app(
        mocker,
        [_group(tmp_path)],
        transport=transport,
        remote=transport != "none",
        ssh_policy=RemoteSSHConfig(),
    )
    app.threaded_actions = True

    def host_runner(argv, **kwargs):
        # The service resolves SSH policy on the host, not from the dashboard.
        if transport == "ssh":
            kwargs["policy"] = RemoteSSHConfig()
        return drunner.run_action(argv, **kwargs)

    app.session.client.action_runner = host_runner
    spawn = Spawns([_prompt(question="Publish proposal?", default=False)])
    app.session.client.action_spawn = spawn
    hand_off = mocker.patch.object(app, "hand_off", side_effect=AssertionError("terminal hand-off"))

    async def main():
        async with app.run_test() as pilot:
            await pilot.pause()
            app.session.overlay = app.session.open_outbox("alpha-x")
            app.refresh_frame()
            await _until(pilot, lambda: app.session.outbox.state.busy is None, "outbox listing")
            await pilot.press("p")
            await _until(
                pilot,
                lambda: any(e.pending_question for e in app.session.queue),
                "queued publication question",
            )
            assert not isinstance(app.screen, pm.ConfirmModal)
            await pilot.press("escape", "Q", "enter")
            await _until(pilot, lambda: isinstance(app.screen, pm.ConfirmModal), "publish modal")
            assert app.screen.prompt.question == "Publish proposal?"
            await pilot.press("y")
            await _until(pilot, lambda: _finished(app), "publication result")
            (entry,) = app.session.queue
            assert entry.state == "done" and "alku" in entry.output

    asyncio.run(asyncio.wait_for(main(), _RUN_SECONDS))
    hand_off.assert_not_called()
    assert spawn.child.answers[0].value is True
    argv = spawn.calls[0]["argv"]
    assert "--source-manifest" in argv if kind == "source" else "apply" in argv


@pytest.mark.parametrize(("index", "argv"), [(0, ["stop", "--all"]), (1, ["resume"])])
def test_global_lifecycle_runs_without_a_repo_and_uses_prompt_channel(mocker, index, argv):
    from tests.dashboard_pilot import FakeStateClient, Pick, _apply

    client = FakeStateClient([])
    client.resumable_repos = ["alpha"]
    app = make_app(mocker, [], client=client)
    app.threaded_actions = True
    app.session.client.action_runner = drunner.run_action
    spawn = Spawns([_prompt(question="Proceed on the host?", default=False)])
    app.session.client.action_spawn = spawn
    hand_off = mocker.patch.object(app, "hand_off", side_effect=AssertionError("terminal hand-off"))

    async def main():
        async with app.run_test() as pilot:
            await pilot.press("f10")
            await pilot.pause()
            await _apply(pilot, app, Pick(index))
            await _until(
                pilot,
                lambda: isinstance(app.screen, pm.ConfirmModal),
                "immediate host confirmation",
            )
            (entry,) = app.session.queue
            assert entry.pending_question is not None
            assert entry.state == "awaiting"
            assert app.screen.prompt.question == "Proceed on the host?"
            await pilot.press("y")
            await _until(pilot, lambda: _finished(app), "host lifecycle result")
            (entry,) = app.session.queue
            assert entry.target == "Host"
            assert entry.command == "jailbee " + " ".join(argv)
            assert entry.state == "done" and "alku" in entry.output

    asyncio.run(asyncio.wait_for(main(), _RUN_SECONDS))
    hand_off.assert_not_called()
    assert spawn.child.answers[0].value is True
    assert spawn.calls[0]["argv"][-len(argv) :] == argv
    assert spawn.calls[0]["cwd"] == Path.home()


def test_single_choice_answer_reaches_the_child(mocker, tmp_path):
    options = (WireOption("0", "a", "a", ""), WireOption("1", "b", "b", ""))
    spawn = Spawns([_prompt(kind="choose_one", question="which", options=options)])

    async def script(app, pilot):
        await _until(pilot, lambda: isinstance(app.screen, pm.ChooseOneModal), "the list")
        await pilot.press("down", "enter")
        await _until(pilot, lambda: _finished(app), "the finished entry")
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)
    assert spawn.child.answers[0].value == "1"


def test_cancelling_a_modal_is_a_cancel_for_the_child(mocker, tmp_path):
    spawn = Spawns([_prompt()])

    async def script(app, pilot):
        await _confirm_modal(pilot, app)
        await pilot.press("ctrl+c")
        await _until(pilot, lambda: _finished(app), "the finished entry")
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)
    assert isinstance(spawn.child.answers[0], Cancel)


@pytest.mark.parametrize("kind", ["confirm", "choose_one", "choose_many", "text", "secret"])
@pytest.mark.parametrize("control", ["later", "abort-command"])
def test_contextual_question_actions_are_keyboard_accessible(mocker, tmp_path, kind, control):
    prompt = _prompt(
        kind=kind,
        options=(WireOption("0", "first", "first", ""),) if "choose" in kind else (),
    )
    spawn = Spawns([prompt])
    app = make_app(mocker, [_group(tmp_path)])
    app.session.client.action_runner = drunner.run_action
    app.session.client.action_spawn = spawn
    app.threaded_actions = True

    async def main():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-x", "container", ["start", "alpha-x"])
            await _until(
                pilot,
                lambda: isinstance(app.screen, pm.PromptModal) and app.screen.is_mounted,
                "contextual question",
            )
            await pilot.pause()
            if kind == "confirm":
                await pilot.press("right")
                assert app.screen.query_one(OptionList).highlighted == 1
                await pilot.press("left")
                assert app.screen.query_one(OptionList).highlighted == 0
            await pilot.press("tab")
            assert isinstance(app.focused, Button)
            assert app.focused.id == "later"
            await pilot.press("shift+tab")
            assert not isinstance(app.focused, Button)
            await pilot.press("tab")
            assert isinstance(app.focused, Button)
            assert app.focused.id == "later"
            if control == "abort-command":
                await pilot.press("tab")
                assert app.focused.id == control
            await pilot.press("enter")
            if control == "later":
                await _until(pilot, lambda: not app.modal_open, "question deferred")
                assert not spawn.child.answers
                (entry,) = app.session.queue
                assert entry.pending_question is not None
                assert entry.pending_question.question == prompt.question
                assert entry.pending_question.kind == kind
                assert entry.state == "awaiting"
                await pilot.press("Q", "enter")
                await _until(
                    pilot, lambda: isinstance(app.screen, pm.PromptModal), "reopened question"
                )
                await pilot.press("ctrl+c")
            await _until(pilot, lambda: _finished(app), "aborted command")

    asyncio.run(asyncio.wait_for(main(), 20))
    assert isinstance(spawn.child.answers[0], Cancel)


def test_destructive_question_is_styled_and_defaults_to_no(mocker, tmp_path):
    spawn = Spawns([_prompt(question="Delete alpha-x?", default=False, destructive=True)])
    seen: dict[str, Any] = {}

    async def script(app, pilot):
        await _confirm_modal(pilot, app)
        seen["styled"] = app.screen.has_class("destructive")
        seen["focus"] = app.focused
        assert isinstance(app.focused, OptionList)
        seen["highlighted"] = app.focused.highlighted
        await pilot.press("enter")  # Enter on the preset highlight
        await _until(pilot, lambda: _finished(app), "the finished entry")
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)
    assert seen["styled"]
    assert isinstance(seen["focus"], OptionList)
    assert seen["highlighted"] == 0
    assert spawn.child.answers[0].value is False


def test_secret_is_masked_in_the_dashboard(mocker, tmp_path):
    spawn = Spawns([_prompt(kind="secret", question="token")])
    seen: dict[str, Any] = {}

    async def script(app, pilot):
        await _until(pilot, lambda: isinstance(app.screen, pm.TextModal), "the input")
        seen["masked"] = app.screen.query_one(Input).password
        await pilot.press(*"s3cret", "enter")
        await _until(pilot, lambda: _finished(app), "the finished entry")
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)
    assert seen["masked"] is True
    assert spawn.child.answers[0].value == "s3cret"


def test_teardown_with_contextual_question_does_not_log_callback_error(mocker, tmp_path, caplog):
    spawn = Spawns([_prompt()])
    app = make_app(mocker, [_group(tmp_path)])
    app.threaded_actions = True
    app.session.client.action_runner = drunner.run_action
    app.session.client.action_spawn = spawn

    async def main():
        async with app.run_test() as pilot:
            app.session.run_dashboard_command("alpha-x", "container", ["start", "alpha-x"])
            await _confirm_modal(pilot, app)
        # run_test tears down the screens before resolving the pending answer.
        await asyncio.sleep(0)

    asyncio.run(asyncio.wait_for(main(), 20))
    assert not [record for record in caplog.records if record.levelno >= 40]


def test_quitting_with_a_question_pending_detaches_and_the_app_exits(mocker, tmp_path):
    spawn = Spawns([_prompt()])

    async def script(app, pilot):
        await _confirm_modal(pilot, app)
        app.exit(0)

    app = _drive(mocker, tmp_path, spawn, script)  # wait_for: a hang fails the test
    assert not app.quit_requested
    assert not spawn.child.answers
    app.session.client.release_test_questions()
    spawn.child._thread.join(_JOIN_SECONDS)
    assert not spawn.child._thread.is_alive()


def test_refused_action_shows_the_refusal_and_starts_nothing(mocker, tmp_path):
    spawn = Spawns()
    mocker.patch.object(
        drunner,
        "check_dashboard_command",
        side_effect=drunner.RouteError("merge: not allowed"),
    )
    seen: dict[str, Any] = {}

    async def script(app, pilot):
        await _until(pilot, lambda: app.session.notice is not None, "the notice")
        seen["notice"] = app.session.notice
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)
    assert not spawn.calls
    assert "not allowed" in seen["notice"]


def test_source_update_exception_remains_in_queue_detail_and_log(mocker, tmp_path):
    from jailbee.dashboard.model import RepoTarget

    session, terminal = bare_session(mocker, [_group(tmp_path)])

    def run(runs, done, progress=None):
        def failing(argv, *, on_output, **kwargs):
            on_output("manifest own.json\n")
            raise RuntimeError("source update rejected: permission denied [remote]")

        done(drunner.run_actions(runs, run=failing))

    terminal.run_actions = run
    session.run_action(
        RepoTarget(tmp_path, None),
        ["pr", "push", "alpha-x", "--source-manifest", "own.json"],
        style="output",
        label="pr push alpha-x",
    )
    (entry,) = session.queue
    assert entry.state == "failed"
    assert "permission denied [remote]" in entry.output
    assert "permission denied [remote]" in " ".join(session.queue_entry_picker(entry.id).detail)
    assert "permission denied [remote]" in session.notice
    from jailbee.dashboard.tui.session import PickerEntry

    session._queue_chosen(
        session.queue_entry_picker(entry.id), PickerEntry("Show output", "output", "o")
    )
    assert "permission denied [remote]" in terminal.shown[-1][1]


def test_attach_verbs_still_hand_over_the_terminal(mocker, tmp_path):
    mocker.patch.object(tsession.subprocess, "run").return_value.returncode = 0
    session, terminal = bare_session(mocker, [_group(tmp_path)])

    session.dispatch("alpha-x", "shell")
    assert len(terminal.handed) == 1
    assert not getattr(terminal, "ran", [])

    session.dispatch("alpha-x", "stop")
    assert len(terminal.handed) == 1  # a plain action does not take the terminal
    assert [r.argv[0] for r in terminal.ran] == ["stop"]


def test_remote_dashboard_action_audit_record_carries_the_ssh_client(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteSSHConfig

    records: list[dict[str, Any]] = []
    mocker.patch.object(drunner, "jsonl_audit", return_value=records.append)
    app = make_app(
        mocker,
        [_group(tmp_path)],
        remote=True,
        transport="ssh",
        ssh_policy=RemoteSSHConfig(),
        remote_client="SHA256:abc",
    )
    app.threaded_actions = True
    app.session.client.action_runner = drunner.run_action
    app.session.client.action_spawn = Spawns()

    async def main() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            for key in ["j", "!", *keys("merge"), "enter"]:
                await pilot.press(key)
            await _until(pilot, lambda: bool(records), "the audit record")

    asyncio.run(asyncio.wait_for(main(), _RUN_SECONDS))

    assert records[0]["transport"] == "ssh"
    assert records[0]["client"] == "SHA256:abc"
