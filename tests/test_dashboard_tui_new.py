"""New-container flows on Pilot: the inline prompts, retarget, SIGINT, jobs, vanishing repos."""

from __future__ import annotations

import pytest

from jailbee.dashboard import dispatch as ddispatch
from jailbee.dashboard import menus as dmenus
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.jobs import JobRunner
from jailbee.dashboard.tui import keys as tkeys
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import session as tsession
from tests.dashboard_fixtures import ci, fake_branches, retarget_group
from tests.dashboard_pilot import drive, keys, patch_in, patch_pause

pytestmark = pytest.mark.usefixtures("no_real_branch_listing")

# repo header -> Enter opens the repo menu -> Down to "New from PR..." -> Enter
_NEW_PR_KEYS = ["enter", "down", "enter", *keys("123"), "enter"]


def _paste(text: str):  # type: ignore[no-untyped-def]
    """A step delivering ``text`` as one input, as a terminal paste arrives."""
    return lambda app: app.session.handle_input(text.encode())


def _prompts(run):  # type: ignore[no-untyped-def]
    return run.of_type(tsession.TextPrompt)


def test_run_new_from_empty_repo_header_dispatches_to_repo_root(mocker, tmp_path):
    group = dmodel.RepoGroup("empty", str(tmp_path), None, [])
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    patch_pause(mocker)

    run = drive(mocker, ["n", *keys("feature"), "enter", "enter"], [group])  # base prefilled "main"
    assert run.rc == 0

    prompt.assert_not_called()  # the terminal is never handed over for a question
    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--", "feature", "main"], check=False, cwd=tmp_path
    )


def test_run_new_prompt_is_drawn_in_the_frame_and_keeps_the_table(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")

    run = drive(mocker, ["n", *keys("fe")], [group])

    prompts = _prompts(run)
    assert prompts[-1].label == "New branch"
    assert prompts[-1].text == "fe"
    # the table is still drawn behind the prompt, the cursor where `n` was pressed
    last = next(view for view in reversed(run.trace) if view.overlay is prompts[-1])
    assert last.groups == [group]
    assert last.selected == dmodel.Row("repo", "alpha")


@pytest.mark.parametrize(
    "answers",
    [
        pytest.param(["n", "escape"], id="branch-step-escape"),
        pytest.param(["n", *keys("feature"), "enter", "escape"], id="base-step-escape"),
        # Ctrl-C answers the prompt only
        pytest.param(["n", *keys("feature"), "enter", "ctrl+c"], id="base-step-ctrl-c"),
    ],
)
def test_run_new_escape_at_either_step_spawns_nothing_and_keeps_the_dashboard(
    mocker, tmp_path, answers
):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")

    run = drive(mocker, [*answers, "h", "escape"], [group])

    assert run.rc == 0
    child.assert_not_called()
    # after cancelling, the dashboard still handled a later key (help opened)
    assert "help" in run.overlays()
    assert any("Cancelled" in str(notice) for notice in run.notices())


def test_run_new_blank_branch_is_rejected_inline_not_dispatched(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")

    run = drive(mocker, ["n", *keys("  "), "enter"], [group])

    child.assert_not_called()
    assert any(p.error == "New branch cannot be empty" for p in _prompts(run))


def test_run_new_trims_answers_and_rejects_a_blank_base_inline(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    patch_pause(mocker)

    steps = [
        "n",
        *keys("  feature  "),
        "enter",
        *["backspace"] * 4,  # wipe "main"
        *keys("  "),
        "enter",  # a whitespace-only base: rejected inline
        *["backspace"] * 2,
        *keys(" dev "),
        "enter",
    ]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    assert any(p.error == "Base branch cannot be empty" for p in _prompts(run))
    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--", "feature", "dev"], check=False, cwd=tmp_path
    )


def test_run_retarget_asks_inline_and_runs_the_cli_with_the_base(mocker, tmp_path):
    group = retarget_group(tmp_path)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    mocker.patch.object(tsession, "host_branches", side_effect=fake_branches)

    run = drive(mocker, ["j", "enter", "g", "b", *keys("dev"), "tab", "enter"], [group])
    assert run.rc == 0

    prompts = _prompts(run)
    assert prompts[0].purpose == "container-retarget"
    assert prompts[0].suggestions == ("main", "develop")  # current base left out
    assert prompts[0].require_suggestion is True
    assert "feat/a" in prompts[0].title
    child.assert_called_once()
    assert child.call_args.args[0] == ["jailbee", "git", "retarget", "--", "alpha-x", "develop"]


def test_run_retarget_refuses_an_unknown_branch_inline(mocker, tmp_path):
    group = retarget_group(tmp_path)
    child = mocker.patch.object(tsession.subprocess, "run")
    mocker.patch.object(tsession, "host_branches", side_effect=fake_branches)

    run = drive(mocker, ["j", "enter", "g", "b", *keys("nope"), "enter"], [group])

    child.assert_not_called()
    assert any(p.error == "'nope' is not one of the listed branches" for p in _prompts(run))


def test_run_retarget_prompt_is_gated_by_the_dispatch_prechecks(mocker, tmp_path):
    """No prompt opens when the pre-checks (SSH policy, availability) refuse the verb."""
    group = retarget_group(tmp_path)
    child = mocker.patch.object(tsession.subprocess, "run")
    mocker.patch.object(tsession, "host_branches", side_effect=fake_branches)
    patch_in(
        mocker,
        "check_dashboard_command",
        ddispatch,
        dmenus,
        tkeys,
        tsession,
        side_effect=tsession.RouteError("refused by policy"),
    )

    run = drive(mocker, ["j", "enter", "g", "b", "escape"], [group])

    assert _prompts(run) == []
    child.assert_not_called()
    assert any("refused by policy" in str(notice) for notice in run.notices())


def test_run_new_base_prompt_offers_the_host_branches(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession.subprocess, "run")
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    mocker.patch.object(tsession, "host_branches", side_effect=fake_branches)

    run = drive(mocker, ["n", *keys("feature"), "enter"], [group])

    base = [p for p in _prompts(run) if p.purpose == "new-base"]
    assert base and base[-1].suggestions == ("main", "feat/a", "develop")
    assert base[-1].require_suggestion is False
    assert base[-1].text == "main"


@pytest.mark.parametrize(
    "before",
    [
        pytest.param(["n"], id="branch-step"),
        pytest.param(["n", *keys("feature"), "enter"], id="base-step"),
        pytest.param(["!", *keys("ls")], id="command-line"),
    ],
)
def test_run_sigint_at_a_text_input_cancels_it_and_keeps_the_dashboard(mocker, tmp_path, before):
    """Collapsed SIGINT: Textual delivers Ctrl-C as one key, never as a signal."""
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")

    run = drive(mocker, [*before, "ctrl+c", "h", "escape"], [group])

    assert run.rc == 0
    child.assert_not_called()
    assert run.trace[len(before) + 1].overlay is None  # the Ctrl-C closed the input
    # the dashboard outlived the Ctrl-C and took "h"
    assert run.trace[len(before) + 2].overlay == "help"


def test_run_sigint_with_no_overlay_still_quits(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, ["ctrl+c", "h"], [group])

    assert run.rc == 0
    child.assert_not_called()
    assert run.steps_taken == 1  # nothing was read after the interrupt


def test_run_new_from_pr_prompts_for_a_number_and_dispatches(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    assert drive(mocker, _NEW_PR_KEYS, [group]).rc == 0

    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--pr", "123"], check=False, cwd=tmp_path
    )


def test_new_container_runs_detached_without_taking_the_terminal(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    wait = patch_pause(mocker)

    assert drive(mocker, _NEW_PR_KEYS, [group]).rc == 0

    child.assert_called_once()
    wait.assert_not_called()  # `foreground` always ends in the "press Enter" stop


def test_new_container_that_wants_an_answer_is_rerun_in_the_foreground(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    detached = mocker.Mock(returncode=2, stderr="error: ... no terminal to ask on. Re-run")
    attended = mocker.Mock(returncode=0, stderr=None)
    child = mocker.patch.object(tsession.subprocess, "run", side_effect=[detached, attended])
    wait = patch_pause(mocker)

    assert drive(mocker, _NEW_PR_KEYS, [group]).rc == 0

    argv = ["jailbee", "new", "--background", "--pr", "123"]
    assert child.call_args_list == [
        mocker.call(argv, check=False, cwd=tmp_path),
        mocker.call(argv, check=False, cwd=tmp_path),
    ]
    wait.assert_called_once()  # the second run is the attended one


def test_new_container_real_failure_is_noticed_not_rerun(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(
        tsession.subprocess,
        "run",
        return_value=mocker.Mock(returncode=1, stderr="warning\nerror: git fetch failed\n"),
    )
    wait = patch_pause(mocker)

    run = drive(mocker, _NEW_PR_KEYS, [group])

    assert run.rc == 0
    child.assert_called_once()
    wait.assert_not_called()
    assert any(
        "jailbee new failed: error: git fetch failed" in str(notice) for notice in run.notices()
    )


class _PendingJobs(JobRunner):
    """A runner whose job never finishes, to look at the in-flight state."""

    def start(self, key, label, argv, cwd, on_done):  # type: ignore[no-untyped-def]  # test double
        if key in self._labels:
            raise ValueError(key)
        self._labels[key] = label


def test_running_job_is_shown_and_not_started_twice(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    typed = ["n", *keys("work"), "enter", "enter", "n", *keys("work"), "enter", "enter"]
    patch_pause(mocker)
    mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, typed, [group], jobs=_PendingJobs)

    notices = [str(notice) for notice in run.notices()]
    assert any("creating work…" in n for n in notices)
    assert any("already being created" in n for n in notices)


def test_run_new_prompt_whose_repo_vanishes_dispatches_nothing(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    live: list[dmodel.RepoGroup] = [group]
    child = mocker.patch.object(tsession.subprocess, "run")
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    patch_pause(mocker)

    # branch, then the repo vanishes, then Enter and Enter to confirm the base
    steps = ["n", *keys("feature"), lambda _app: live.clear(), "enter", "enter"]
    run = drive(mocker, steps, live)
    assert run.rc == 0

    child.assert_not_called()
    # the tick after the vanish closes the prompt, so no submit is ever reached
    notices = [str(notice) for notice in run.notices()]
    assert any("prompt closed" in n for n in notices)


def test_run_new_prompt_whose_repo_loses_its_directory_is_refused_at_submit(mocker, tmp_path):
    """The submit-time lookup alone: the tick still lists the repo, only its directory is gone."""
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    patch_pause(mocker)

    def lose_directory(_app):
        group.repo_root = None  # still listed (the tick keeps the prompt), but not runnable

    # branch, Enter opens the base prompt, the directory goes, Enter submits
    steps = ["n", *keys("feature"), "enter", lose_directory, "enter"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    child.assert_not_called()
    prompts = run.of_type(tsession.TextPrompt)
    assert len(prompts) >= 2  # the branch prompt, then the base prompt that was submitted
    notices = [str(notice) for notice in run.notices()]
    assert not any("prompt closed" in n for n in notices)
    assert "'alpha' is no longer listed" in notices


def test_run_open_prompt_closes_when_its_repo_vanishes_before_any_submit(mocker, tmp_path):
    """The tick-time guard alone: the repo goes while the user is still typing."""
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    live: list[dmodel.RepoGroup] = [group]
    child = mocker.patch.object(tsession.subprocess, "run")
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")

    # no Enter: nothing is ever submitted; the repo goes right after "f" was typed
    run = drive(mocker, ["n", "f", lambda _app: live.clear()], live)
    assert run.rc == 0

    child.assert_not_called()
    overlays = run.overlays()
    prompts = [o for o in overlays if isinstance(o, tsession.TextPrompt)]
    assert prompts[-1].text == "f"
    closed_at = overlays.index(prompts[-1]) + 1
    assert overlays[closed_at] is None
    assert "'alpha' is gone — prompt closed" in str(run.trace[closed_at].notice)


def test_run_empty_repo_header_menu_creates_container(mocker, tmp_path):
    group = dmodel.RepoGroup("empty", str(tmp_path), None, [])
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    patch_pause(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0

    # Enter opens the repo menu, Enter picks "New container...", then the two answers
    steps = ["enter", "enter", *keys("feature"), "enter", "enter"]
    assert drive(mocker, steps, [group]).rc == 0

    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--", "feature", "main"], check=False, cwd=tmp_path
    )


def test_run_cannot_create_from_a_row_hidden_by_visibility_settings(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = ["down", "S", "tab", "tab", "down", "space", "escape", "n"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    prompt.assert_not_called()
    child.assert_not_called()
    assert any("Select a repo" in str(notice) for notice in run.notices())


def test_open_menu_closes_when_its_container_becomes_hidden(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    hiding = False
    filter_groups = tsession.visible_repo_groups

    def hide_after_menu_opens(groups, *, show_empty_repos, hidden_repos):
        active_hidden = hidden_repos | ({"alpha"} if hiding else set())
        return filter_groups(
            groups, show_empty_repos=show_empty_repos, hidden_repos=frozenset(active_hidden)
        )

    mocker.patch.object(tsession, "visible_repo_groups", side_effect=hide_after_menu_opens)

    def hide_selected_repo(_app) -> None:  # type: ignore[no-untyped-def]
        nonlocal hiding
        hiding = True

    # the menu is open; its repo gets hidden before the next key
    run = drive(mocker, ["down", "enter", hide_selected_repo, "x", "enter"], [group])
    assert run.rc == 0

    overlays = run.overlays()
    menu_frames = [overlay for overlay in overlays if isinstance(overlay, tmenu.MenuState)]
    assert menu_frames  # the selected container really did have an open action menu
    closed_at = overlays.index(None, overlays.index(menu_frames[-1]) + 1)
    assert not any(isinstance(overlay, tmenu.MenuState) for overlay in overlays[closed_at:])
    assert any("menu closed" in str(notice) for notice in run.notices())
    child.assert_not_called()


def test_settings_key_switches_from_another_overlay_instead_of_closing(mocker):
    """F2/S must mirror ``h``'s own toggle: pressing it while another
    overlay (the action menu, help) is open switches to settings, not just
    closes whatever was open.

    There is no live group in this harness (the fake state client serves
    ``[]``), and Space is only handled by the settings overlay. That makes
    ``save_view_state`` firing after ``h`` then ``S`` then `Space` a
    discriminating signal that ``S`` actually opened the settings overlay,
    whose Space handling calls ``persist_view_state``, rather than merely
    closing help and leaving the bare table to reject the keypress silently.
    Fails if `"settings"` goes back to being grouped
    with `("cancel", "quit")`, which only closes whatever overlay is open.
    """
    save = mocker.patch.object(tsession, "save_view_state")
    run = drive(mocker, ["h", "S", "space"])

    assert run.rc == 0
    save.assert_called_once()


def test_run_dispatches_n_to_start_new_container(mocker):
    """Drive the `n` key through the session's real dispatch, not just
    `parse_key`/the binding shape in isolation -- a typo in that arm would be
    caught by nothing else.

    The fake state client serves no containers, so nothing is selected and
    `start_new_container` takes its notice path ("Select a repo or a container
    first") without prompting or spawning anything.
    """
    run = drive(mocker, ["n"])

    assert run.rc == 0
    assert "Select a repo or a container first" in run.notices()


def test_repo_menu_new_runs_the_existing_creation_flow(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession, "new_container_base_default", return_value="develop")
    patch_pause(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0

    # the base field is prefilled with "develop": erase it and type another base
    steps = [
        "enter",
        "enter",
        *keys("feature"),
        "enter",
        *["backspace"] * 7,
        *keys("main"),
        "enter",
    ]
    assert drive(mocker, steps, [group]).rc == 0

    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--", "feature", "main"], check=False, cwd=tmp_path
    )


def test_repo_menu_new_from_pr_runs_review_creation_in_repo(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    patch_pause(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0

    steps = ["enter", "j", "enter", *keys("123"), "enter"]
    assert drive(mocker, steps, [group]).rc == 0

    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--pr", "123"], check=False, cwd=tmp_path
    )


_NOT_A_PR = "PR number must be a positive whole number"


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ("0", _NOT_A_PR),
        ("-2", _NOT_A_PR),
        ("abc", _NOT_A_PR),
        ("--yes", _NOT_A_PR),
        ("  ", "PR number cannot be empty"),
        pytest.param("9" * 5000, _NOT_A_PR, id="oversized"),
    ],
)
def test_repo_menu_new_from_pr_rejects_nonpositive_or_non_numeric_input(
    mocker, tmp_path, answer, error
):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    patch_pause(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    # the answer arrives as one input (a paste), so the oversized case stays one frame
    run = drive(mocker, ["enter", "j", "enter", _paste(answer), "enter"], [group])
    assert run.rc == 0

    child.assert_not_called()
    prompts = _prompts(run)
    assert prompts[-1].purpose == "new-pr"
    assert prompts[-1].text == answer
    assert prompts[-1].error == error


def test_new_container_reports_a_vanished_repo_root_instead_of_crashing(mocker, tmp_path):
    """The identical failure as the dispatch one, reached through a different
    keypress: once both inline answers are in, `run_new_container`'s own
    `subprocess.run(new_container_argv(...), cwd=repo.cwd())` raises the same
    uncaught `OSError` if the repo root disappeared between a refresh and the
    final Enter. Exercises `_report_vanished_repo`'s other call site (shared
    with `dispatch`) rather than assuming the fix generalizes.
    """
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    # Patches the same `subprocess.run` `new_container_base_default` calls
    # through `git.get_current_branch` -- that call already tolerates OSError
    # and returns None (so the base field opens empty), so this only affects
    # `run_new_container`'s subprocess.run.
    mocker.patch.object(tsession.subprocess, "run", side_effect=OSError("gone"))

    # The repo header row is selected by default (no navigation needed): "n"
    # opens the branch prompt, then the base prompt, and the final Enter runs
    # `jailbee new` through `run_new_container`, not `dispatch`.
    steps = ["n", *keys("work"), "enter", *keys("main"), "enter"]
    run = drive(mocker, steps, [group])

    assert run.rc == 0  # the OSError did not propagate
    assert any(notice is not None and str(tmp_path) in notice for notice in run.notices())
