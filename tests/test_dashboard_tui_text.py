"""Native text boxes: the ``!`` command line and the prompt (V3b)."""

from __future__ import annotations

from textual import events
from textual.widgets import Input

from jailbee.dashboard import model as dmodel
from jailbee.dashboard.overlays import TextPrompt, validate_answer
from jailbee.dashboard.tui import session as tsession
from jailbee.dashboard.tui.overlay import CommandState, NativeState, overlay_key
from tests.dashboard_fixtures import ci, fake_branches, retarget_group
from tests.dashboard_pilot import (
    Paste,
    Pick,
    backgrounds,
    bare_session,
    box_text,
    drive,
    keys,
    patch_pause,
)


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


RETARGET = ["j", "enter", "g", "b"]  # container menu → Git → Retarget… (the choice prompt)


def _prompt_states(run):  # type: ignore[no-untyped-def]
    return [n for n in run.natives if n is not None and n.kind == "prompt"]


def _retarget(mocker, tmp_path, steps, **kw):  # type: ignore[no-untyped-def]
    mocker.patch.object(tsession, "host_branches", side_effect=fake_branches)
    return drive(mocker, [*RETARGET, *steps], [retarget_group(tmp_path)], **kw)


def test_n_opens_a_focused_prompt(mocker, tmp_path):
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    focused: list[str] = []
    run = drive(
        mocker, ["n", lambda app: focused.append(type(app.focused).__name__)], [_group(tmp_path)]
    )
    assert isinstance(run.trace[1].overlay, TextPrompt)
    assert _prompt_states(run)[0] == NativeState("prompt", None, text="", matches=())
    assert focused == ["OverlayInput"]


def test_a_prefilled_answer_is_extended_not_replaced(mocker, tmp_path):
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    mocker.patch.object(tsession, "host_branches", return_value=("main", "dev"))
    run = drive(mocker, ["n", *keys("feat"), "enter", "x"], [_group(tmp_path)])
    assert run.trace[-2].overlay.purpose == "new-base"  # the last frame is the harness closing it
    assert _prompt_states(run)[-1].text == "mainx"


def test_down_up_and_enter_on_the_matches(mocker, tmp_path):
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    run = _retarget(mocker, tmp_path, ["down", "down", "down", "up", "enter"])
    assert [n.cursor for n in _prompt_states(run)[1:5]] == [0, 1, 1, 0]
    assert child.call_args.args[0] == ["jailbee", "git", "retarget", "--", "alpha-x", "main"]


def test_up_from_the_first_match_leaves_the_list_and_enter_takes_the_text(mocker, tmp_path):
    run = _retarget(mocker, tmp_path, ["down", "up", *keys("nope"), "enter"])
    states = _prompt_states(run)
    assert states[2].cursor is None
    assert states[-1].error == "'nope' is not one of the listed branches"


def test_tab_completes_the_first_match_or_the_highlighted_one(mocker, tmp_path):
    run = _retarget(mocker, tmp_path, ["tab", "ctrl+u", "down", "down", "tab"])
    texts = [n.text for n in _prompt_states(run)]
    assert texts[1] == "main" and texts[-1] == "develop"
    assert _prompt_states(run)[-1].cursor is None  # completing drops the highlight


def test_typing_after_arrowing_drops_the_highlight(mocker, tmp_path):
    run = _retarget(mocker, tmp_path, ["down", "d"])
    assert _prompt_states(run)[-1].cursor is None
    assert _prompt_states(run)[-1].matches == ("develop",)


def test_ticks_keep_the_typed_text_highlight_and_error(mocker, tmp_path):
    def tick_five(app):  # type: ignore[no-untyped-def]
        for _ in range(5):
            app.refresh_frame()

    # "e" lists only "develop"; Enter refuses it (not an exact name); ↓ highlights it.
    run = _retarget(mocker, tmp_path, ["e", "enter", "down", tick_five])
    before, after = _prompt_states(run)[-2], _prompt_states(run)[-1]
    assert after == before
    assert (after.text, after.cursor) == ("e", 0)
    assert after.error == "'e' is not one of the listed branches"


def test_an_error_shows_until_the_next_edit_and_again_on_the_next_submit(mocker, tmp_path):
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    run = drive(mocker, ["n", "enter", "a", "backspace", "enter"], [_group(tmp_path)])
    errors = [n.error for n in _prompt_states(run)]
    assert errors[1:] == ["New branch cannot be empty", None, None, "New branch cannot be empty"]


def test_a_refused_highlighted_match_keeps_its_error(mocker, tmp_path):
    mocker.patch.object(tsession, "validate_answer", return_value="refused")
    run = _retarget(mocker, tmp_path, ["down", "enter"])
    last = _prompt_states(run)[-1]
    assert (last.text, last.error) == ("main", "refused")


def test_a_click_on_a_match_submits_it(mocker, tmp_path):
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    _retarget(mocker, tmp_path, [Pick(1)])
    assert child.call_args.args[0] == ["jailbee", "git", "retarget", "--", "alpha-x", "develop"]


def test_ctrl_c_cancels_only_the_prompt(mocker, tmp_path):
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    run = drive(mocker, ["n", *keys("q"), "ctrl+c", "j"], [_group(tmp_path)])
    assert run.trace[-2].overlay is None and run.trace[-2].notice == "Cancelled"
    assert run.last.selected == dmodel.Row("container", "alpha-one")


def test_ctrl_h_and_backspace_delete(mocker, tmp_path):
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    run = drive(mocker, ["n", "a", "b", "ctrl+h", "c", "backspace"], [_group(tmp_path)])
    assert _prompt_states(run)[-1].text == "a"


def test_the_choice_prompt_paints_no_background_but_the_hover(mocker, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR")
    seen: list[set[str]] = []
    _retarget(mocker, tmp_path, ["down", lambda app: seen.append(backgrounds(app))])
    assert seen[0] and seen[0] <= {"default"}


def test_validate_answer_reads_the_given_text():
    prompt = TextPrompt("new-pr", "t", "PR number")
    assert validate_answer(prompt, "  ") == "PR number cannot be empty"
    assert validate_answer(prompt, "0") == "PR number must be a positive whole number"
    assert validate_answer(prompt, " 12 ") is None


_BRANCHES = ("main", "feat/maint", "release/main-fix", "develop")


def _choice(text: str = "", **kw) -> TextPrompt:  # type: ignore[no-untyped-def]
    return TextPrompt(
        "new-base", "New container", "Base branch", initial=text, suggestions=_BRANCHES, **kw
    )


def test_the_box_lists_the_matches_under_the_input():
    out = "\n".join(box_text(_choice("ma")))
    assert "New container" in out and "Base branch" in out
    assert "> ma" in out
    assert "feat/maint" in out and "main" in out
    assert "develop" not in out


def test_the_box_keeps_markup_in_branch_names_literal():
    spec = TextPrompt("new-base", "t", "Base branch", suggestions=("feat/[wip]",))
    assert "feat/[wip]" in "\n".join(box_text(spec))


def test_the_box_says_when_nothing_matches():
    assert "(no matching branch)" in "\n".join(box_text(_choice("zzz")))


def test_a_prompt_without_suggestions_lists_nothing():
    out = "\n".join(box_text(TextPrompt("new-branch", "t", "New branch", initial="ab")))
    assert "> ab" in out and "no matching" not in out


def test_backspace_also_drops_the_highlight(mocker, tmp_path):
    run = _retarget(mocker, tmp_path, ["d", "down", "backspace"])
    states = _prompt_states(run)
    assert states[-2].cursor == 0 and states[-1].cursor is None


def test_a_choice_prompt_takes_free_text_unless_a_listed_name_is_required(mocker, tmp_path):
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    mocker.patch.object(tsession, "host_branches", return_value=("main", "dev"))
    run = drive(mocker, ["n", *keys("feat"), "enter", *keys("zz"), "enter"], [_group(tmp_path)])
    assert run.trace[-1].overlay is None  # "mainzz" was accepted: new-base takes free text


def test_a_prompt_without_suggestions_ignores_arrows_and_tab(mocker, tmp_path):
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    run = drive(mocker, ["n", "x", "down", "up", "tab", "x"], [_group(tmp_path)])
    assert _prompt_states(run)[-1].text == "xx"


def test_an_edit_that_keeps_the_matches_still_drops_the_highlight(mocker, tmp_path):
    # A space leaves the filter blank, so the list is not rebuilt (which would reset it).
    run = _retarget(mocker, tmp_path, ["down", "space"])
    last = _prompt_states(run)[-1]
    assert (last.text, last.cursor, last.matches) == (" ", None, ("main", "develop"))


def test_a_late_change_event_for_the_refused_text_keeps_the_error(mocker, tmp_path):
    def late_changed(app):  # type: ignore[no-untyped-def]
        box = app.frame.native_box
        box.post_message(Input.Changed(box.input, box.input.value, None))

    run = _retarget(mocker, tmp_path, ["e", "enter", late_changed])
    last = _prompt_states(run)[-1]
    assert (last.text, last.error) == ("e", "'e' is not one of the listed branches")
