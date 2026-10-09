"""Input order: every key and paste is handled before the next one is read (V4)."""

from __future__ import annotations

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.overlays import TextPrompt
from jailbee.dashboard.settings import SettingsState
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.overlay import NativeState
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import Paste, burst, drive, patch_pause


def _group(tmp_path):  # type: ignore[no-untyped-def]
    return dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [ci("alpha-one", "alpha"), ci("alpha-two", "alpha")]
    )


def _typed(text: str) -> tuple[tuple[str, str], ...]:
    """Letters as (key, character) pairs; punctuation has other key names, so none here."""
    assert all(ch.isalnum() for ch in text), text
    return tuple((ch, ch) for ch in text)


def _texts(run):  # type: ignore[no-untyped-def]
    return [n.text for n in run.natives if n is not None and n.kind in ("command", "prompt")]


def test_keys_after_the_command_runs_reach_the_table(mocker, tmp_path):
    """`!ls⏎j` in one read: `ls` runs, then `j` moves the table, not the dead line."""
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    run = drive(
        mocker,
        [burst(("exclamation_mark", "!"), *_typed("ls"), ("enter", "\r"), ("j", "j"))],
        [_group(tmp_path)],
    )
    child.assert_called_once()
    assert child.call_args.args[0][:2] == ["jailbee", "ls"]
    assert run.last.overlay is None
    assert run.last.selected == dmodel.Row("container", "alpha-one")


def test_answers_typed_ahead_land_in_the_question_they_answer(mocker, tmp_path):
    """`n feat⏎ dev` in one read: the branch question gets `feat`, the base question `dev`."""
    run = drive(
        mocker,
        [
            "j",
            burst(
                ("n", "n"),
                *_typed("feat"),
                ("enter", "\r"),
                ("ctrl+u", "\x15"),
                *_typed("dev"),
            ),
        ],
        [_group(tmp_path)],
    )
    prompt = run.of_type(TextPrompt)[-1]
    assert prompt.purpose == "new-base" and prompt.carry == ("feat",)
    assert _texts(run)[-1] == "dev"


def test_a_paste_in_the_same_read_as_bang_lands_in_the_line(mocker, tmp_path):
    run = drive(
        mocker,
        [burst(("exclamation_mark", "!"), Paste("ls -l"), ("x", "x"))],
        [_group(tmp_path)],
    )
    assert _texts(run)[-1] == "ls -lx"


def test_editing_keys_apply_in_order_with_typed_text(mocker, tmp_path):
    """Ctrl-A between letters in one read moves the cursor before the next letter."""
    run = drive(
        mocker,
        [burst(("exclamation_mark", "!"), *_typed("ab"), ("ctrl+a", "\x01"), ("x", "x"))],
        [_group(tmp_path)],
    )
    assert _texts(run)[-1] == "xab"


def test_ctrl_c_in_a_burst_cancels_only_the_line(mocker, tmp_path):
    run = drive(
        mocker,
        [burst(("exclamation_mark", "!"), ("l", "l"), ("ctrl+c", "\x03"), ("j", "j"))],
        [_group(tmp_path)],
    )
    assert run.last.overlay is None
    assert run.last.selected == dmodel.Row("container", "alpha-one")
    # `drive` stops as soon as the app quits; a step after the burst means it did not.
    assert run.steps_taken > 1


def test_a_quick_key_that_hands_off_keeps_the_keys_after_it(mocker, tmp_path):
    """`t` runs tmux in the terminal; `j` typed with it moves the table once it returns."""
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    run = drive(mocker, ["j", burst(("t", "t"), ("j", "j"))], [_group(tmp_path)])
    child.assert_called_once()
    assert run.last.selected == dmodel.Row("container", "alpha-two")


def test_menu_keys_typed_ahead_reach_the_menu(mocker, tmp_path):
    run = drive(mocker, ["j", burst(("enter", "\r"), ("j", "j"))], [_group(tmp_path)])
    assert run.natives[-1] is not None and run.natives[-1].cursor == 1


def test_a_hotkey_typed_ahead_opens_its_level(mocker, tmp_path):
    """`⏎g` in one read: the menu opens and `g` opens Git → inside it (V3b parked item 1)."""
    run = drive(mocker, ["j", burst(("enter", "\r"), ("g", "g"))], [_group(tmp_path)])
    assert run.natives[-1] is not None and run.natives[-1].level == "Git →"


def test_keys_after_closing_the_menu_reach_the_table(mocker, tmp_path):
    run = drive(
        mocker,
        [
            "j",
            burst(("enter", "\r"), ("g", "g"), ("escape", "\x1b"), ("escape", "\x1b"), ("j", "j")),
        ],
        [_group(tmp_path)],
    )
    assert run.last.overlay is None
    assert run.last.selected == dmodel.Row("container", "alpha-two")


def test_q_typed_ahead_closes_the_menu_then_the_table_moves(mocker, tmp_path):
    run = drive(mocker, ["j", burst(("enter", "\r"), ("q", "q"), ("j", "j"))], [_group(tmp_path)])
    assert run.last.overlay is None
    assert run.last.selected == dmodel.Row("container", "alpha-two")


def test_space_typed_ahead_toggles_in_settings(mocker, tmp_path):
    """Row 0 of Fields is the locked `name`, so the typed-ahead `space` goes to the Repos tab."""
    mocker.patch.object(tsession, "save_view_state")
    run = drive(mocker, [burst(("S", "S"), ("tab", "\t"), ("space", " "))], [_group(tmp_path)])
    settings = run.of_type(SettingsState)[-1]
    assert settings.folded  # the first repo row was toggled by the space


def test_a_box_that_lost_the_focus_still_gets_its_keys(mocker, tmp_path):
    """Bindings are found through the focus chain; a key re-focuses the box first."""
    run = drive(
        mocker,
        ["j", "enter", lambda app: app.screen.set_focus(None), "j"],
        [_group(tmp_path)],
    )
    assert run.natives[-1] == NativeState("menu", 1, level=None)


def test_a_leaf_hotkey_typed_ahead_runs_before_the_next_key(mocker, tmp_path):
    """`⏎t j` in one read: the menu's `t` runs tmux and closes it, then `j` moves the table."""
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    run = drive(mocker, ["j", burst(("enter", "\r"), ("t", "t"), ("j", "j"))], [_group(tmp_path)])
    child.assert_called_once()
    assert run.last.overlay is None
    assert run.last.selected == dmodel.Row("container", "alpha-two")
