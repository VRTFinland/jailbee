"""Non-attach actions run through the action runner; their questions are modals."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Input

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


async def _until(pilot: Any, condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await pilot.pause(0.02)


def _drive(
    mocker: Any,
    tmp_path: Path,
    spawn: Spawns,
    script: Callable[[Any, Any], Any],
) -> Any:
    """A real threaded runner over ``spawn``; ``script(app, pilot)`` plays the user."""
    app = make_app(mocker, [_group(tmp_path)])
    app.threaded_actions = True
    app.action_runner = drunner.run_action
    app.action_spawn = spawn

    async def main() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            for key in ["j", "!", *keys("merge"), "enter"]:  # the inline command line
                await pilot.press(key)
            await script(app, pilot)

    asyncio.run(asyncio.wait_for(main(), 20))
    return app


def _prompt(**kw: Any) -> Prompt:
    return Prompt(id=0, **{"kind": "confirm", "question": "Go?", **kw})


def test_prompt_during_action_is_a_modal_and_the_answer_reaches_the_child(mocker, tmp_path):
    spawn = Spawns([_prompt(default=True)])

    async def script(app, pilot):
        await _until(pilot, lambda: isinstance(app.screen, pm.ConfirmModal), "the modal")
        assert app.session.terminal is app
        await pilot.press("y")
        await _until(pilot, lambda: isinstance(app.screen, pm.OutputScreen), "the output")
        assert "alku ä" in app.screen.text  # what the child printed is shown by the dashboard
        await pilot.press("enter")
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)

    (answer,) = spawn.child.answers
    assert isinstance(answer, Answer)
    assert answer.value is True
    assert spawn.calls[0]["argv"][1:3] == ["-m", "jailbee"]
    assert spawn.calls[0]["argv"][-2:] == ["merge", "alpha-x"]


def test_single_choice_answer_reaches_the_child(mocker, tmp_path):
    options = (WireOption("0", "a", "a", ""), WireOption("1", "b", "b", ""))
    spawn = Spawns([_prompt(kind="choose_one", question="which", options=options)])

    async def script(app, pilot):
        await _until(pilot, lambda: isinstance(app.screen, pm.ChooseOneModal), "the list")
        await pilot.press("down", "enter")
        await _until(pilot, lambda: isinstance(app.screen, pm.OutputScreen), "the output")
        await pilot.press("enter")
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)
    assert spawn.child.answers[0].value == "1"


def test_cancelling_a_modal_is_a_cancel_for_the_child(mocker, tmp_path):
    spawn = Spawns([_prompt()])

    async def script(app, pilot):
        await _until(pilot, lambda: isinstance(app.screen, pm.ConfirmModal), "the modal")
        await pilot.press("escape")
        await _until(pilot, lambda: isinstance(app.screen, pm.OutputScreen), "the output")
        await pilot.press("enter")
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)
    assert isinstance(spawn.child.answers[0], Cancel)


def test_destructive_question_is_styled_and_defaults_to_no(mocker, tmp_path):
    spawn = Spawns([_prompt(question="Delete alpha-x?", default=False, destructive=True)])
    seen: dict[str, Any] = {}

    async def script(app, pilot):
        await _until(pilot, lambda: isinstance(app.screen, pm.ConfirmModal), "the modal")
        seen["styled"] = app.screen.has_class("destructive")
        seen["focus"] = app.focused
        await pilot.press("enter")  # Enter on the preset focus
        await _until(pilot, lambda: isinstance(app.screen, pm.OutputScreen), "the output")
        await pilot.press("enter")
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)
    assert seen["styled"]
    assert isinstance(seen["focus"], Button)
    assert seen["focus"].id == "no"
    assert spawn.child.answers[0].value is False


def test_secret_is_masked_in_the_dashboard(mocker, tmp_path):
    spawn = Spawns([_prompt(kind="secret", question="token")])
    seen: dict[str, Any] = {}

    async def script(app, pilot):
        await _until(pilot, lambda: isinstance(app.screen, pm.TextModal), "the input")
        seen["masked"] = app.screen.query_one(Input).password
        await pilot.press(*"s3cret", "enter")
        await _until(pilot, lambda: isinstance(app.screen, pm.OutputScreen), "the output")
        await pilot.press("enter")
        await pilot.press("ctrl+c")

    _drive(mocker, tmp_path, spawn, script)
    assert seen["masked"] is True
    assert spawn.child.answers[0].value == "s3cret"


def test_quitting_with_a_question_pending_cancels_it_and_the_app_exits(mocker, tmp_path):
    spawn = Spawns([_prompt()])

    async def script(app, pilot):
        await _until(pilot, lambda: isinstance(app.screen, pm.ConfirmModal), "the modal")
        app._after("quit")  # what the quit key does

    app = _drive(mocker, tmp_path, spawn, script)  # wait_for: a hang fails the test
    assert app.quit_requested
    assert isinstance(spawn.child.answers[0], Cancel)  # the child was told, so it exited
    spawn.child._thread.join(5)
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
    app.action_runner = drunner.run_action
    app.action_spawn = Spawns()

    async def main() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            for key in ["j", "!", *keys("merge"), "enter"]:
                await pilot.press(key)
            await _until(pilot, lambda: bool(records), "the audit record")

    asyncio.run(asyncio.wait_for(main(), 20))

    assert records[0]["transport"] == "ssh"
    assert records[0]["client"] == "SHA256:abc"
