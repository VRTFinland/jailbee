"""DashboardApp on Pilot: keys, Ctrl-C, hand-off, title, repaint, startup."""

from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from textual import events
from textual._xterm_parser import (
    XTermParser,
)  # private: the only way to see what a terminal's bytes become

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.overlays import Picker, PickerEntry, TextPrompt
from jailbee.dashboard.tui import app as tapp
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.keys import KEY_BINDINGS, parse_key
from jailbee.dashboard.tui.menu_state import MenuState
from jailbee.dashboard.tui.overlay import CommandState
from jailbee.state_service import StateServiceUnavailable
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import drive, patch_pause, start_session

# What a terminal sends for each bound key, in every encoding the old byte table knew.
_TERMINAL_BYTES = (
    "\x1b[A",
    "\x1bOA",
    "\x1b[B",
    "\x1bOB",
    "\x1b[C",
    "\x1b[D",
    "\x1b[1;2A",
    "\x1b[1;2B",
    "\x1b[5~",
    "\x1b[6~",
    "\r",
    "\n",
    "\x1b",
    " ",
    "\t",
    "\x1bOQ",
    "\x1b[12~",
    "\x03",
    "\x12",
    "\x1b[15~",
    "?",
    "!",
    *"neEAOvMShjkqQ<>I+-=",
)


def _key_names(data: str) -> list[str]:
    parser = XTermParser()
    events_ = [*parser.feed(data), *parser.feed("")]
    return [e.key for e in events_ if isinstance(e, events.Key)]


def test_every_bound_key_is_what_a_terminal_produces():
    tokens = {parse_key(name) for data in _TERMINAL_BYTES for name in _key_names(data)}
    assert {b.token for b in KEY_BINDINGS} <= tokens


def test_every_declared_key_name_is_one_a_terminal_produces():
    produced = {name for data in _TERMINAL_BYTES for name in _key_names(data)}
    declared = {key for b in KEY_BINDINGS for key in b.keys}
    assert declared - produced <= {"ctrl+m"}  # Enter's other alias: a terminal sends \r as "enter"


@pytest.mark.parametrize(("key", "token"), [("ctrl+j", "enter"), ("ctrl+m", "enter")])
def test_enter_keeps_its_control_aliases(key, token):
    assert parse_key(key) == token


def test_unmapped_keys_are_dropped():
    assert parse_key("ctrl+x") == parse_key("home") == parse_key("é") == ""


def test_keys_move_the_selection_and_open_a_menu(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    run = drive(mocker, ["j", "enter"], [group])
    assert run.trace[1].selected == dmodel.Row("container", "alpha-one")
    assert isinstance(run.trace[2].overlay, MenuState)
    assert run.rc == 0


@pytest.mark.parametrize(
    ("opening", "overlay_type"),
    [
        (["n"], TextPrompt),
        (["!"], CommandState),
    ],
)
def test_ctrl_c_in_a_text_input_cancels_only_the_input(mocker, tmp_path, opening, overlay_type):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    run = drive(mocker, [*opening, "ctrl+c", "h"], [group])
    assert isinstance(run.trace[len(opening)].overlay, overlay_type)
    assert run.trace[len(opening) + 1].overlay is None
    assert run.trace[len(opening) + 2].overlay == "help"  # still running after Ctrl-C


def test_ctrl_c_at_a_picker_cancels_the_picker(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    picker = Picker("repo-apply", "Apply", (PickerEntry("a", "a"),), target="alpha")
    run = drive(
        mocker,
        [lambda app: setattr(app.session, "overlay", picker), "ctrl+c", "h"],
        [group],
    )
    assert run.trace[1].overlay == picker
    assert run.trace[2].overlay is None
    assert run.trace[2].notice == "Cancelled"
    assert run.trace[3].overlay == "help"


@pytest.mark.parametrize(
    ("opening", "overlay_check"),
    [
        (["j", "enter"], lambda overlay: isinstance(overlay, MenuState)),
        (["h"], lambda overlay: overlay == "help"),
    ],
    ids=["menu", "help"],
)
def test_ctrl_c_at_a_menu_or_help_quits_at_once(mocker, tmp_path, opening, overlay_check):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    run = drive(mocker, [*opening, "ctrl+c", "h"], [group])
    assert overlay_check(run.trace[len(opening)].overlay)
    assert run.steps_taken == len(opening) + 1  # the Ctrl-C ended it: no `h` after, no padding
    assert run.rc == 0


def test_ctrl_c_with_nothing_open_quits_at_once(mocker):
    run = drive(mocker, ["ctrl+c", "h"], [])
    assert run.steps_taken == 1
    assert run.rc == 0


def test_textual_own_quit_binding_is_not_inherited(mocker):
    """Ctrl-Q is a priority Textual binding that would end the app behind the session's back."""
    run = drive(mocker, ["ctrl+q", "h"], [])
    assert run.trace[2].overlay == "help"
    assert run.steps_taken == 3  # the two keys, then the Ctrl-C that quits


def test_hand_off_order_marks_the_client_inactive_around_the_child(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    patch_pause(mocker)
    seen: list[list[tuple]] = []

    def child(*_a, **_k):
        seen.append(list(client.events))
        return mocker.Mock(returncode=0)

    mocker.patch.object(tsession.subprocess, "run", side_effect=child)
    _, client = start_session(mocker, [group])
    drive(mocker, ["j", "t"], [group], client=client)
    assert seen
    assert seen[0][-1] == ("active", False)
    after = client.events[len(seen[0]) :]
    assert after[:2] == [("active", True), ("refresh",)]


def test_a_raising_child_still_reactivates_the_client_and_rewrites_the_title(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    writes = mocker.patch.object(tapp.DashboardApp, "_write_terminal")
    seen: dict[str, object] = {}

    def explode() -> int:
        raise RuntimeError("child blew up")

    def hand_off_the_failing_child(app: tapp.DashboardApp) -> None:
        before = len(client.events)
        with pytest.raises(RuntimeError, match="child blew up"):
            app.hand_off(explode)
        seen["events"] = client.events[before:]
        seen["painted"] = app._painted

    _, client = start_session(mocker, [group])
    drive(mocker, [hand_off_the_failing_child], [group], client=client)
    assert seen["events"] == [("active", False), ("active", True), ("refresh",)]
    assert seen["painted"] is None  # the next frame repaints the screen the child drew over
    # first frame, then again on the frame after the failed hand-off
    assert [c.args[0] for c in writes.call_args_list] == ["\x1b]2;🐝 alpha\x07"] * 2


def test_hand_off_suspends_textual_when_the_driver_can(mocker, tmp_path):
    """Headless Pilot cannot suspend; the real driver can, and must be used."""
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    patch_pause(mocker)
    order: list[str] = []
    mocker.patch.object(
        tsession.subprocess,
        "run",
        side_effect=lambda *_a, **_k: order.append("child") or mocker.Mock(returncode=0),
    )

    @contextmanager
    def fake_suspend(_self):
        order.append("suspend")
        yield
        order.append("resume")

    mocker.patch.object(tapp.DashboardApp, "suspend", fake_suspend)
    mocker.patch.object(tapp, "_can_suspend", return_value=True)
    drive(mocker, ["j", "t"], [group])
    assert order == ["suspend", "child", "resume"]


def test_the_title_is_written_on_change_and_again_after_a_hand_off(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    patch_pause(mocker)
    mocker.patch.object(tsession.subprocess, "run", return_value=mocker.Mock(returncode=0))
    writes = mocker.patch.object(tapp.DashboardApp, "_write_terminal")
    # "j" twice: the second finds no row below, so the title does not change.
    drive(mocker, ["j", "j", "t"], [group])
    container_title = tsession.terminal_title([group], dmodel.Row("container", "alpha-one"))
    assert [c.args[0] for c in writes.call_args_list] == [
        "\x1b]2;🐝 alpha\x07",
        f"\x1b]2;{container_title}\x07",
        f"\x1b]2;{container_title}\x07",  # rewritten after the `t` hand-off
    ]


def test_an_unchanged_view_is_not_repainted(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    # A fixed clock: the title's seconds would otherwise change the view.
    mocker.patch.object(tsession, "_now", return_value=datetime(2026, 10, 7, 12, tzinfo=UTC))
    from jailbee.dashboard.tui import widgets

    paint = mocker.spy(widgets.DashboardFrame, "show")
    counts: list[int] = []
    run = drive(
        mocker,
        [
            lambda _app: counts.append(paint.call_count),
            lambda _app: None,
            lambda _app: counts.append(paint.call_count),
        ],
        [group],
    )
    assert counts[0] >= 1
    assert counts[1] == counts[0]  # two idle ticks repainted nothing
    assert run.rc == 0


def test_startup_waits_for_the_first_snapshot_before_textual_starts(mocker):
    _, client = start_session(mocker, [])
    order: list[str] = []
    wait = client.wait_first_snapshot
    client.wait_first_snapshot = lambda t: (order.append("wait"), wait(t))[1]
    mocker.patch.object(
        tapp.DashboardApp, "run", side_effect=lambda **_kw: order.append("app") or 0
    )
    assert tapp.run(mocker.Mock(), None) == 0
    assert order == ["wait", "app"]
    assert client.closed


def test_a_crashed_app_exits_non_zero(mocker):
    """Textual ends an unhandled handler exception with return code 1 and no value."""
    _, client = start_session(mocker, [])

    def crash(self, **_kw):
        self._return_code = 1

    mocker.patch.object(tapp.DashboardApp, "run", autospec=True, side_effect=crash)
    assert tapp.run(mocker.Mock(), None) == 1
    assert client.closed


def test_ctrl_c_out_of_textual_exits_cleanly(mocker):
    _, client = start_session(mocker, [])
    mocker.patch.object(tapp.DashboardApp, "run", side_effect=KeyboardInterrupt)
    assert tapp.run(mocker.Mock(), None) == 0
    assert client.closed


def test_a_failing_app_constructor_still_closes_the_state_client(mocker):
    _, client = start_session(mocker, [])
    mocker.patch.object(tapp.DashboardApp, "__init__", side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        tapp.run(mocker.Mock(), None)
    assert client.closed


def test_a_redirected_stderr_is_refused_before_any_screen(mocker):
    mocker.patch.object(tsession.sys, "stdin", mocker.Mock(isatty=lambda: True))
    mocker.patch.object(tsession.sys, "stdout", mocker.Mock(isatty=lambda: True))
    mocker.patch.object(tsession.sys, "stderr", mocker.Mock(isatty=lambda: False))
    client = mocker.patch.object(tsession, "open_state_client")
    app_run = mocker.patch.object(tapp.DashboardApp, "run")
    assert tapp.run(mocker.Mock(), None) == 1
    client.assert_not_called()
    app_run.assert_not_called()


def test_a_failed_first_gather_never_starts_textual(mocker):
    _, client = start_session(mocker, [], fail=StateServiceUnavailable("no server"))
    app_run = mocker.patch.object(tapp.DashboardApp, "run")
    assert tapp.run(mocker.Mock(), None) == 1
    app_run.assert_not_called()
    assert client.closed


def test_the_app_module_is_the_only_one_importing_textual():
    code = (
        "import sys, jailbee.cli, jailbee.dashboard.tui.session, jailbee.dashboard.tui.frame, "
        "jailbee.dashboard.tui.fleet, jailbee.dashboard.tui.layout, "
        "jailbee.dashboard.tui.keys, jailbee.dashboard.tui.overlay, "
        "jailbee.dashboard.overlays; sys.exit('textual' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def test_outbox_delete_keeps_keys_live_and_finishes_after_close(mocker, tmp_path):
    blocked_verb = "drop"
    import asyncio
    from threading import Event, get_ident

    from jailbee.dashboard.jobs import JobRunner
    from tests.dashboard_fixtures import cfg_group
    from tests.dashboard_pilot import container_menu_keys, make_app, screen_text
    from tests.test_dashboard_outbox import _action, _container, _listing, _proposal

    entered, release = Event(), Event()
    calls = []
    notifications = []
    ui_thread = get_ident()
    blocking = False
    listing = _listing(_container(_proposal(actions=[_action(0)])))

    def quiet(argv, **kwargs):
        calls.append((argv[1], get_ident()))
        if blocking and argv[1] == blocked_verb:
            entered.set()
            assert release.wait(5), "test did not release the outbox runner"
        return tsession.da.CliResult(True, "done", listing)

    mocker.patch.object(tsession.da, "run_cli_quiet", side_effect=quiet)
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    app = make_app(mocker, [group], jobs=JobRunner)

    async def settle(pilot, controller):
        async with asyncio.timeout(5):
            while controller.state.busy:
                app.session.jobs.poll()
                app.refresh_frame()
                await pilot.pause(0.001)
        await pilot.pause()

    async def scenario():
        nonlocal blocking
        async with app.run_test(size=(120, 35)) as pilot:
            try:
                for key in container_menu_keys(group, "outbox browse"):
                    await pilot.press(key)
                controller = app.session.outbox
                assert controller is not None
                await settle(pilot, controller)
                changed = controller.on_change

                def record(state):
                    notifications.append(get_ident())
                    changed(state)

                controller.on_change = record
                calls.clear()
                blocking = True
                await pilot.press("d", "down", "enter")
                assert await asyncio.to_thread(entered.wait, 2), "delete command never started"
                assert controller.state.busy == "Deleting 1/1… · Esc leaves; deletion continues"
                assert "Deleting 1/1" in screen_text(app)
                preview = controller.state.preview
                await pilot.press("v")
                assert controller.state.preview != preview
                assert not release.is_set()  # the key was processed while the child was blocked
                await pilot.press("r", "p")
                assert controller.state.busy == "Deleting 1/1… · Esc leaves; deletion continues"
                await pilot.press("escape")
                assert not isinstance(app.session.overlay, tsession.OutboxState)
                release.set()
                await settle(pilot, controller)
                assert not isinstance(app.session.overlay, tsession.OutboxState)
                assert app.session.notice == "Deleted 1 from the outbox."
                assert ("refresh",) in app.session.client.events
            finally:
                release.set()

    asyncio.run(scenario())
    assert [verb for verb, _ in calls] == ["drop", "ls"]
    assert all(thread != ui_thread for _, thread in calls)
    assert notifications and set(notifications) == {ui_thread}


@pytest.mark.parametrize("fail_middle", [False, True])
@pytest.mark.parametrize("queue_extra", [False, True])
def test_outbox_batch_delete_progress_rows_and_deferred_refresh(
    mocker, tmp_path, fail_middle, queue_extra
):
    import asyncio
    from threading import Event

    from jailbee.dashboard.jobs import JobRunner
    from tests.dashboard_fixtures import cfg_group
    from tests.dashboard_pilot import container_menu_keys, make_app, screen_text
    from tests.test_dashboard_outbox import _container, _listing, _proposal

    proposals = [_proposal(f"pr/{letter}.json") for letter in "abcd"]
    entered = [Event() for _ in range(4)]
    release = [Event() for _ in range(4)]
    calls = []
    drops = 0

    def quiet(argv, **kwargs):
        nonlocal drops
        calls.append(list(argv))
        if argv[1] == "drop":
            index = drops
            drops += 1
            entered[index].set()
            assert release[index].wait(10), "test did not release the drop"
            if fail_middle and index == 1:
                return tsession.da.CliResult(False, "locked")
            proposals[:] = [p for p in proposals if p["id"] != argv[3]]
            return tsession.da.CliResult(True, "done", "")
        return tsession.da.CliResult(True, "done", _listing(_container(*proposals)))

    mocker.patch.object(tsession.da, "run_cli_quiet", side_effect=quiet)
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    app = make_app(mocker, [group], jobs=JobRunner)

    async def wait_for(pilot, event):
        async with asyncio.timeout(5):
            while not event.is_set():
                app.session.jobs.poll()
                app.refresh_frame()
                await pilot.pause(0.001)
        await pilot.pause()

    async def scenario():
        async with app.run_test(size=(140, 35)) as pilot:
            try:
                for key in container_menu_keys(group, "outbox browse"):
                    await pilot.press(key)
                controller = app.session.outbox
                await _settle_outbox(app, pilot, controller)
                sent_loader = mocker.Mock(return_value=())
                controller.sent_loader = sent_loader
                permissions = mocker.spy(controller.runner, "permitted")
                calls.clear()
                await pilot.press("space", "down", "space", "down", "space", "d", "down", "enter")
                await wait_for(pilot, entered[0])
                assert "Deleting 1/3" in screen_text(app)
                assert "Esc leaves; deletion continues" in screen_text(app)
                await pilot.press("down", "space", "r", "r", "enter")
                assert ("alpha-x", "pr/d.json", None, None) in controller.state.marks
                assert "pr/d.json" in str(controller.state.expanded)
                assert [a[1] for a in calls] == ["drop"]
                if queue_extra:
                    # Unmark d and ask about a again: the same proposal is reserved.
                    await pilot.press("space", "home", "d")
                    assert controller.state.status == ("pr/a.json: already in the delete queue",)
                    await pilot.pause()
                    assert "already in the delete queue" in screen_text(app)
                    await pilot.press("end", "left", "space", "d", "down", "enter")
                    assert controller.state.question is None
                    assert "Deleting 1/4" in screen_text(app)
                release[0].set()
                await wait_for(pilot, entered[1])
                assert f"Deleting 2/{4 if queue_extra else 3}" in screen_text(app)
                sent_loader.assert_not_called()
                permissions.assert_not_called()
                assert [p.id for c in controller.state.containers for p in c.proposals] == [
                    "pr/b.json",
                    "pr/c.json",
                    "pr/d.json",
                ]
                release[1].set()
                await wait_for(pilot, entered[2])
                assert f"Deleting 3/{4 if queue_extra else 3}" in screen_text(app)
                if queue_extra:
                    release[2].set()
                    await wait_for(pilot, entered[3])
                    assert "Deleting 4/4" in screen_text(app)
                await pilot.press("escape")
                assert not isinstance(app.session.overlay, tsession.OutboxState)
                release[3 if queue_extra else 2].set()
                await _settle_outbox(app, pilot, controller)
                assert [a[1] for a in calls] == ["drop"] * (4 if queue_extra else 3) + ["ls"], calls
                sent_loader.assert_called_once_with()
                assert permissions.call_count > 0
                assert (
                    ("alpha-x", "pr/d.json", None, None) in controller.state.marks
                ) is not queue_extra
                remaining = [p.id for c in controller.state.containers for p in c.proposals]
                assert remaining == (["pr/b.json"] if fail_middle else []) + (
                    [] if queue_extra else ["pr/d.json"]
                )
                deleted = 3 + int(queue_extra) - int(fail_middle)
                summary = (
                    f"1 not deleted (pr/b.json: locked); deleted {deleted}"
                    if fail_middle
                    else f"Deleted {deleted} from the outbox."
                )
                assert controller.state.status == (summary,)
                assert app.session.notice == summary
                assert summary in screen_text(app)
            finally:
                for event in release:
                    event.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("separate_batches", [False, True])
@pytest.mark.parametrize("first_fails", [False, True])
@pytest.mark.parametrize("refresh_fails", [False, True])
def test_finished_proposal_can_be_deleted_again_while_another_drop_runs(
    mocker, tmp_path, separate_batches, first_fails, refresh_fails
):
    import asyncio
    from threading import Event

    from jailbee.dashboard.jobs import JobRunner
    from tests.dashboard_fixtures import cfg_group
    from tests.dashboard_pilot import container_menu_keys, make_app
    from tests.test_dashboard_outbox import _action, _container, _listing, _proposal

    proposals = [
        _proposal("pr/a.json", actions=[_action(0, body="first"), _action(1, body="keep")]),
        _proposal("pr/b.json"),
    ]
    entered, release = Event(), Event()
    first_entered, first_release = Event(), Event()
    drops = []

    def quiet(argv, **kwargs):
        if argv[1] == "drop":
            drops.append(argv[3])
            if len(drops) == 1:
                first_entered.set()
                assert first_release.wait(10), "test did not release a"
                if first_fails:
                    return tsession.da.CliResult(False, "locked")
                proposals[0]["actions"].pop(0)
                proposals[0]["actions"][0]["index"] = 0
                proposals[0]["actions"][0]["delete_scope"] = [0]
                proposals[0]["revision"] = "r2"
                return tsession.da.CliResult(True, "done", "")
            if argv[3] == "pr/a.json":
                if argv[argv.index("--revision") + 1] != ("r1" if first_fails else "r2"):
                    return tsession.da.CliResult(False, "proposal changed; refresh required")
            if argv[3] == "pr/b.json":
                entered.set()
                assert release.wait(10), "test did not release b"
            proposals[:] = [p for p in proposals if p["id"] != argv[3]]
            return tsession.da.CliResult(True, "done", "")
        if refresh_fails and len(drops) == 1 and not first_fails:
            return tsession.da.CliResult(False, "listing unavailable")
        return tsession.da.CliResult(True, "done", _listing(_container(*proposals)))

    mocker.patch.object(tsession.da, "run_cli_quiet", side_effect=quiet)
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    app = make_app(mocker, [group], jobs=JobRunner)

    async def scenario():
        async with app.run_test(size=(140, 35)) as pilot:
            try:
                for key in container_menu_keys(group, "outbox browse"):
                    await pilot.press(key)
                controller = app.session.outbox
                await _settle_outbox(app, pilot, controller)
                if first_fails:
                    select = ()
                else:
                    select = ("enter", "down")  # a's first action
                if separate_batches:
                    await pilot.press(*select, "d", "down", "enter")
                    assert await asyncio.to_thread(first_entered.wait, 2)
                    await pilot.press("end", "d", "down", "enter")
                else:
                    await pilot.press(*select, "space", "end", "space", "d", "down", "enter")
                    assert await asyncio.to_thread(first_entered.wait, 2)
                if not first_fails:
                    await pilot.press("home", "down", "down", "space")
                first_release.set()
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        app.session.jobs.poll()
                        app.refresh_frame()
                        await pilot.pause(0.001)
                if not first_fails and not refresh_fails:
                    assert controller.state.marks == frozenset({("alpha-x", "pr/a.json", 0, None)})
                    await pilot.press("home", "down", "space")  # unmark the same remaining action
                elif not first_fails:
                    assert not controller.state.marks  # cannot retain marks without a fresh revision
                await pilot.press("home", "p")
                assert controller.state.status == (
                    "publish must wait for the delete queue to finish",
                )
                await pilot.press("d")
                if refresh_fails and not first_fails:
                    assert controller.state.question is None
                    assert controller.state.status == ("pr/a.json: refresh required after deletion",)
                    release.set()
                    await _settle_outbox(app, pilot, controller)
                    await pilot.press("d")
                assert controller.state.question is not None
                await pilot.press("down", "enter")
                assert controller.state.question is None
                release.set()
                await _settle_outbox(app, pilot, controller)
                assert drops == ["pr/a.json", "pr/b.json", "pr/a.json"]
                assert not any(c.proposals for c in controller.state.containers)
            finally:
                first_release.set()
                release.set()

    asyncio.run(scenario())


def test_delete_progress_counts_children_skipped_by_a_changed_scope(mocker, tmp_path):
    import asyncio

    from tests.dashboard_pilot import container_menu_keys, screen_text
    from tests.test_dashboard_outbox import _action, _container, _listing, _proposal

    shown = _proposal(actions=[_action(0), _action(1), _action(2)])
    changed = _proposal(revision="r2", actions=[_action(0, body="changed"), _action(1)])
    other = _proposal("pr/b.json")
    app, group, entered, release, calls, gate = _blocking_outbox_app(
        mocker,
        tmp_path,
        [_listing(_container(shown, other)), _listing(_container(changed, other))],
        "drop",
    )

    quiet = tsession.da.run_cli_quiet.side_effect

    def block_b(argv, **kwargs):
        if argv[1] == "drop" and argv[3] == "pr/b.json":
            gate["on"] = True
        return quiet(argv, **kwargs)

    tsession.da.run_cli_quiet.side_effect = block_b

    async def scenario():
        async with app.run_test(size=(140, 35)) as pilot:
            try:
                for key in container_menu_keys(group, "outbox browse"):
                    await pilot.press(key)
                controller = app.session.outbox
                await _settle_outbox(app, pilot, controller)
                # Expand a; mark its three children, then b, not the parent.
                await pilot.press(
                    "enter",
                    "down",
                    "space",
                    "down",
                    "space",
                    "down",
                    "space",
                    "down",
                    "space",
                    "d",
                    "down",
                    "enter",
                )
                async with asyncio.timeout(5):
                    while len([a for a in calls if a[1] == "drop"]) < 2:
                        app.session.jobs.poll()
                        app.refresh_frame()
                        await pilot.pause(0.001)
                assert await asyncio.to_thread(entered.wait, 2)
                assert "Deleting 4/4" in screen_text(app)
                release.set()
                await _settle_outbox(app, pilot, controller)
                assert controller.state.status == (
                    "2 not deleted (pr/a.json: changed; refreshed); deleted 2",
                )
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("confirm_during_reload", [False, True])
def test_delete_question_and_queue_survive_the_final_reload(
    mocker, tmp_path, confirm_during_reload
):
    import asyncio

    from tests.dashboard_pilot import container_menu_keys
    from tests.test_dashboard_outbox import _container, _listing, _proposal

    a, b = _proposal(), _proposal("pr/b.json")
    app, group, entered, release, calls, gate = _blocking_outbox_app(
        mocker,
        tmp_path,
        [_listing(_container(a, b)), _listing(_container(b)), _listing(_container())],
        "ls",
    )

    async def scenario():
        async with app.run_test(size=(140, 35)) as pilot:
            try:
                for key in container_menu_keys(group, "outbox browse"):
                    await pilot.press(key)
                controller = app.session.outbox
                await _settle_outbox(app, pilot, controller)
                calls.clear()
                gate["on"] = True
                await pilot.press("d", "down", "enter")
                async with asyncio.timeout(5):
                    while not entered.is_set():
                        app.session.jobs.poll()
                        app.refresh_frame()
                        await pilot.pause(0.001)
                await pilot.pause()
                await pilot.press("d")  # a is gone; b is the highlighted proposal
                assert controller.state.question is not None
                if confirm_during_reload:
                    await pilot.press("down", "enter")
                release.set()
                await _settle_outbox(app, pilot, controller)
                if not confirm_during_reload:
                    assert controller.state.question is not None
                    assert controller.state.question.lines[0].startswith("pr/b.json")
                    await pilot.press("down", "enter")
                    await _settle_outbox(app, pilot, controller)
                assert [a[1] for a in calls] == ["drop", "ls", "drop", "ls"]
                assert [a[3] for a in calls if a[1] == "drop"] == ["pr/a.json", "pr/b.json"]
                assert not any(c.proposals for c in controller.state.containers)
            finally:
                release.set()

    asyncio.run(scenario())


def _blocking_outbox_app(mocker, tmp_path, listings, blocked_verb):
    """The dashboard with the real JobRunner; ``run_cli_quiet`` blocks on ``blocked_verb``.

    ``listings`` is consumed per `outbox ls` (the last one repeats); returns
    (app, group, entered, release, calls, gate); ``gate`` arms the one block.
    """
    from threading import Event

    from jailbee.dashboard.jobs import JobRunner
    from tests.dashboard_fixtures import cfg_group
    from tests.dashboard_pilot import make_app

    entered, release = Event(), Event()
    calls: list[list[str]] = []
    gate = {"on": False}

    def quiet(argv, **kwargs):
        calls.append(list(argv))
        if gate["on"] and argv[1] == blocked_verb:
            gate["on"] = False
            entered.set()
            assert release.wait(5), "test did not release the outbox runner"
        if argv[1] == "ls":
            return tsession.da.CliResult(
                True, "done", listings[0] if len(listings) == 1 else listings.pop(0)
            )
        return tsession.da.CliResult(True, "done", "")

    mocker.patch.object(tsession.da, "run_cli_quiet", side_effect=quiet)
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    app = make_app(mocker, [group], jobs=JobRunner)
    return app, group, entered, release, calls, gate


async def _settle_outbox(app, pilot, controller):
    import asyncio

    async with asyncio.timeout(5):
        while controller.state.busy:
            app.session.jobs.poll()
            app.refresh_frame()
            await pilot.pause(0.001)
    await pilot.pause()


def test_closing_the_box_during_pr_preflight_runs_no_terminal_hand_off(mocker, tmp_path):
    import asyncio

    from tests.dashboard_pilot import container_menu_keys
    from tests.test_dashboard_outbox import _container, _listing, _proposal

    awaiting = _proposal("pr/d.json", state="awaiting-pr", create_scope={"kind": "repo"})
    app, group, entered, release, _calls, gate = _blocking_outbox_app(
        mocker, tmp_path, [_listing(_container(awaiting))], "ls"
    )
    handed = mocker.patch.object(app.session, "run_dashboard_command", return_value=0)

    async def scenario():
        async with app.run_test(size=(120, 35)) as pilot:
            try:
                for key in container_menu_keys(group, "outbox browse"):
                    await pilot.press(key)
                controller = app.session.outbox
                assert controller is not None
                await _settle_outbox(app, pilot, controller)
                gate["on"] = True
                await pilot.press("p")
                assert await asyncio.to_thread(entered.wait, 2), "the preflight never started"
                await pilot.press("escape")
                assert not isinstance(app.session.overlay, tsession.OutboxState)
                release.set()
                await _settle_outbox(app, pilot, controller)
                assert not isinstance(app.session.overlay, tsession.OutboxState)
                assert app.session.notice == "pr push not run: outbox box closed"
            finally:
                release.set()

    asyncio.run(scenario())
    handed.assert_not_called()


def test_pr_preflight_with_the_box_open_runs_the_terminal_hand_off_once(mocker, tmp_path):
    import asyncio

    from tests.dashboard_pilot import container_menu_keys
    from tests.test_dashboard_outbox import _container, _listing, _proposal

    awaiting = _proposal("pr/d.json", state="awaiting-pr", create_scope={"kind": "repo"})
    app, group, entered, release, _calls, gate = _blocking_outbox_app(
        mocker, tmp_path, [_listing(_container(awaiting))], "ls"
    )
    handed = mocker.patch.object(app.session, "run_dashboard_command", return_value=0)

    async def scenario():
        async with app.run_test(size=(120, 35)) as pilot:
            try:
                for key in container_menu_keys(group, "outbox browse"):
                    await pilot.press(key)
                controller = app.session.outbox
                assert controller is not None
                await _settle_outbox(app, pilot, controller)
                gate["on"] = True
                await pilot.press("p")
                assert await asyncio.to_thread(entered.wait, 2), "the preflight never started"
                release.set()
                await _settle_outbox(app, pilot, controller)
                assert app.session.notice != "pr push not run: outbox box closed"
            finally:
                release.set()

    asyncio.run(scenario())
    handed.assert_called_once()


def test_reopen_after_a_background_delete_shows_no_stale_marks(mocker, tmp_path):
    import asyncio

    from tests.dashboard_pilot import container_menu_keys
    from tests.test_dashboard_outbox import _container, _listing, _proposal

    both = _listing(_container(_proposal("pr/a.json"), _proposal("issue/b.json")))
    rest = _listing(_container(_proposal("issue/b.json")))
    # Opening, the delete's pre-drop check, then everything after the drop.
    app, group, entered, release, calls, gate = _blocking_outbox_app(
        mocker, tmp_path, [both, both, rest], "drop"
    )

    async def scenario():
        async with app.run_test(size=(120, 35)) as pilot:
            try:
                for key in container_menu_keys(group, "outbox browse"):
                    await pilot.press(key)
                controller = app.session.outbox
                assert controller is not None
                await _settle_outbox(app, pilot, controller)
                gate["on"] = True
                await pilot.press("space", "d", "down", "enter")
                assert await asyncio.to_thread(entered.wait, 2), "the drop never started"
                await pilot.press("escape")  # closed with pr/a.json still marked
                release.set()
                await _settle_outbox(app, pilot, controller)
                assert isinstance(app.session.overlay, MenuState)  # back at the container menu
                app.session.menu_chosen("outbox browse", None, 0)
                reopened = app.session.outbox
                assert reopened is not None and reopened is not controller
                await _settle_outbox(app, pilot, reopened)
                return reopened.state
            finally:
                release.set()

    state = asyncio.run(scenario())
    assert [p.id for c in state.containers for p in c.proposals] == ["issue/b.json"]
    assert state.marks == frozenset()
    assert sum(argv[:2] == ["outbox", "drop"] for argv in calls) == 1


def test_outbox_box_intent_reaches_session(mocker, tmp_path):
    from tests.dashboard_fixtures import cfg_group
    from tests.dashboard_pilot import container_menu_keys
    from tests.test_dashboard_outbox import _action, _container, _listing, _proposal

    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    mocker.patch.object(
        tsession.da,
        "run_cli_quiet",
        return_value=tsession.da.CliResult(
            True, "done", _listing(_container(_proposal(actions=[_action(0)])))
        ),
    )
    intent = mocker.spy(tsession.DashboardSession, "outbox_intent")

    run = drive(mocker, [*container_menu_keys(group, "outbox browse"), "d"], [group])

    assert intent.call_args_list[0].args[1:3] == ("delete", ("alpha-x", "pr/a.json", None, None))
    asked = [o.question for o in run.of_type(tsession.OutboxState) if o.question is not None]
    assert asked and asked[0].purpose == "delete"
