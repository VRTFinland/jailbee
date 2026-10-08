"""Native text boxes: the ``!`` command line and the prompt (V3b)."""

from __future__ import annotations

from textual import events

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.overlay import CommandState, NativeState, overlay_key
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import Paste, backgrounds, bare_session, drive, keys, patch_pause


def _group(tmp_path):  # type: ignore[no-untyped-def]
    return dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])


def _batch(*pairs: tuple[str, str]):  # type: ignore[no-untyped-def]
    """Keys posted in one go, as one terminal read delivers them (Pilot waits between presses)."""

    def step(app):  # type: ignore[no-untyped-def]
        for key, character in pairs:
            app.post_message(events.Key(key, character))

    return step


def _texts(run):  # type: ignore[no-untyped-def]
    return [n.text for n in run.natives if n is not None and n.kind in ("command", "prompt")]


def test_bang_opens_a_focused_command_box(mocker, tmp_path):
    focused: list[str] = []
    run = drive(
        mocker, ["!", lambda app: focused.append(type(app.focused).__name__)], [_group(tmp_path)]
    )
    assert isinstance(run.trace[1].overlay, CommandState)
    assert overlay_key(CommandState()) == ("command",)
    assert run.natives[1] == NativeState("command", None, text="", matches=())
    assert focused == ["OverlayInput"]


def test_dashboard_keys_are_text_in_the_command_line(mocker, tmp_path):
    run = drive(mocker, ["!", *keys("qhS?"), "ctrl+c", "j"], [_group(tmp_path)])
    assert _texts(run)[-1] == "qhS?"
    assert run.trace[-2].overlay is None  # Ctrl-C closed only the line
    assert run.last.selected == dmodel.Row("container", "alpha-one")  # the table answers again


def test_tab_cycles_the_listed_completions_and_an_edit_restarts(mocker, tmp_path):
    mocker.patch.object(
        tsession.DashboardSession, "command_candidates", return_value=("shell", "shutdown")
    )
    run = drive(mocker, ["!", "s", "tab", "tab", "tab", "x"], [_group(tmp_path)])
    states = [n for n in run.natives if n is not None]
    assert [(n.text, n.cursor) for n in states[2:6]] == [
        ("shell", 0),
        ("shutdown", 1),
        ("shell", 0),
        ("shellx", None),
    ]
    assert states[2].matches == ("shell", "shutdown")


def test_tab_without_candidates_keeps_the_text_and_the_focus(mocker, tmp_path):
    mocker.patch.object(tsession.DashboardSession, "command_candidates", return_value=())
    focused: list[str] = []
    run = drive(
        mocker,
        ["!", "z", "tab", lambda app: focused.append(type(app.focused).__name__)],
        [_group(tmp_path)],
    )
    assert _texts(run)[-1] == "z"
    assert focused == ["OverlayInput"]


def test_keys_typed_before_the_command_box_is_focused_land_in_it(mocker, tmp_path):
    run = drive(
        mocker,
        [
            _batch(
                ("exclamation_mark", "!"), ("l", "l"), ("s", "s"), ("backspace", "\x7f"), ("x", "x")
            )
        ],
        [_group(tmp_path)],
    )
    assert _texts(run)[-1] == "lx"


def test_type_ahead_enter_runs_the_command(mocker, tmp_path):
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    drive(
        mocker,
        [_batch(("exclamation_mark", "!"), ("l", "l"), ("s", "s"), ("enter", "\r"))],
        [_group(tmp_path)],
    )
    child.assert_called_once()
    assert child.call_args.args[0][:2] == ["jailbee", "ls"]


def test_a_multi_line_paste_is_joined_and_a_control_chunk_dropped(mocker, tmp_path):
    run = drive(mocker, ["!", Paste("ls\n-l\n"), Paste("a\tb")], [_group(tmp_path)])
    assert _texts(run)[-1] == "ls-l"


def test_the_command_box_paints_no_background(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")  # else Textual strips every colour and the scan proves nothing
    seen: list[set[str]] = []
    drive(mocker, ["!", "l", lambda app: seen.append(backgrounds(app))], [_group(tmp_path)])
    assert seen[0] and seen[0] <= {"default"}


def test_command_submitted_closes_the_line_and_runs_the_text(mocker, tmp_path):
    session, _ = bare_session(mocker, [_group(tmp_path)])
    run_command = mocker.patch.object(session, "run_command")
    session.overlay = CommandState()
    session.command_submitted("ls -l")
    assert session.overlay is None
    run_command.assert_called_once_with("ls -l")


def test_over_ssh_without_a_policy_nothing_is_offered(mocker, tmp_path):
    session, _ = bare_session(mocker, [_group(tmp_path)], over_ssh=True)
    assert session.command_candidates("sh") == ()
