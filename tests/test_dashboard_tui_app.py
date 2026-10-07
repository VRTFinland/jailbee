"""DashboardApp on Pilot: keys, Ctrl-C, hand-off, title, repaint, startup."""

from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.overlays import Picker, PickerEntry, TextPrompt
from jailbee.dashboard.tui import app as tapp
from jailbee.dashboard.tui import key_adapter
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.keys import KEY_BINDINGS, parse_key
from jailbee.dashboard.tui.menu_state import MenuState
from jailbee.dashboard.tui.overlay import CommandState
from jailbee.state_service import StateServiceUnavailable
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import drive, patch_pause, start_session

# Textual key name → what a terminal sent for it.
_TEXTUAL_KEYS = {
    "up": None,
    "down": None,
    "left": None,
    "right": None,
    "enter": None,
    "escape": "\x1b",
    "tab": "\t",
    "backspace": None,
    "ctrl+c": None,
    "f2": None,
    "space": " ",
}


def test_every_bound_key_is_reachable_from_textual():
    produced = {key_adapter.legacy_bytes(k, c) for k, c in _TEXTUAL_KEYS.items()}
    produced |= {
        key_adapter.legacy_bytes(ch, ch)
        for b in KEY_BINDINGS
        for k in b.keys
        if len(k) == 1 and chr(k[0]).isprintable()
        for ch in [k.decode()]
    }
    tokens = {parse_key(data) for data in produced if data is not None}
    # EOF (b"") has no Textual key by design (plan refinement 4).
    assert {b.token for b in KEY_BINDINGS} <= tokens


@pytest.mark.parametrize(
    ("key", "expected"),
    [("ctrl+h", b"\x7f"), ("ctrl+j", b"\r"), ("ctrl+m", b"\r")],
)
def test_control_aliases_of_backspace_and_enter_are_kept(key, expected):
    assert key_adapter.legacy_bytes(key, None) == expected


def test_ctrl_h_deletes_a_character_in_a_prompt(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    run = drive(mocker, ["n", "a", "b", "ctrl+h", "ctrl+c"], [group])
    assert run.trace[4].overlay.text == "a"


def test_unmapped_and_non_printable_keys_are_dropped():
    assert key_adapter.legacy_bytes("ctrl+x", None) is None
    assert key_adapter.legacy_bytes("home", None) is None
    assert key_adapter.legacy_bytes("é", "é") == "é".encode()


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
    paint = mocker.patch.object(tapp, "render_view", wraps=tapp.render_view)
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
        "jailbee.dashboard.tui.key_adapter; sys.exit('textual' in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
