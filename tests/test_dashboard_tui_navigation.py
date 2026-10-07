"""Navigation on Pilot: folding, column scrolling, menus, SSH gating, view state."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from jailbee.dashboard import columns as dcolumns
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import overlay as toverlay
from jailbee.dashboard.tui import session as tsession
from jailbee.db.view_prefs import FRONTEND_TUI, ViewState
from tests.dashboard_fixtures import WIDE, ci, frame_at, header, named_rows_group, wide_group
from tests.dashboard_pilot import (
    Resize,
    drive,
    keys,
    patch_pause,
    render_text,
    repo_menu_keys,
)

pytestmark = pytest.mark.usefixtures("no_real_branch_listing")

# The scroll markers the table draws at a clipped edge.
LEFT_MORE = chr(0x2039)
RIGHT_MORE = chr(0x203A)


# --- the inline editor ---------------------------------------------------------


def test_inline_editor_takes_a_non_ascii_character(mocker):
    # Textual decodes UTF-8 before the app sees a key, so a character split
    # across two reads cannot reach the app; the pure `edit_command` tests
    # cover the split-bytes case.
    run = drive(mocker, ["!", "é"])

    assert run.rc == 0
    assert isinstance(run.trace[2].overlay, toverlay.CommandState)
    assert run.trace[2].overlay.text == "é"


# --- SSH sessions --------------------------------------------------------------


@pytest.mark.parametrize(("key", "verb"), [("t", "tmux"), ("s", "shell")])
def test_ssh_dashboard_existing_attach_actions_work_without_exec(
    mocker, tmp_path, key, verb
) -> None:
    from jailbee.config.models_remote import RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0

    run = drive(
        mocker,
        ["j", key, "ctrl+c"],
        [group],
        remote=True,
        over_ssh=True,
        ssh_policy=RemoteSSHConfig(),
    )

    assert run.rc == 0
    child.assert_called_once_with(
        ["jailbee", verb, "alpha-x", "--force"], check=False, cwd=tmp_path
    )


# --- Space folds -----------------------------------------------------------------


def test_space_folds_then_unfolds_the_selected_repo(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    save = mocker.patch.object(tsession, "save_view_state")

    assert drive(mocker, ["space", "space"], [group]).rc == 0

    folded = [c.args[2].folded for c in save.call_args_list]
    assert folded == [frozenset({"alpha"}), frozenset()]


def test_space_on_a_container_row_folds_its_repo_and_selects_the_header(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    save = mocker.patch.object(tsession, "save_view_state")

    run = drive(mocker, ["j", "space"], [group])

    assert save.call_args.args[2].folded == frozenset({"alpha"})
    assert run.last.selected == dmodel.Row("repo", "alpha")


def test_space_with_nothing_selected_does_nothing(mocker):
    save = mocker.patch.object(tsession, "save_view_state")
    assert drive(mocker, ["space"]).rc == 0
    save.assert_not_called()


def test_run_space_only_persists_when_settings_overlay_is_open(mocker):
    save = mocker.patch.object(tsession, "save_view_state")
    assert drive(mocker, ["space", "S", "space"]).rc == 0
    save.assert_called_once()


@pytest.mark.parametrize("git_enabled", [True, False])
def test_run_renders_with_the_snapshots_git_enabled(mocker, git_enabled):
    """The TUI forwards the service's `git_enabled` (it never probes git itself)."""
    run = drive(mocker, [], git_enabled=git_enabled)
    assert run.trace
    assert all(view.git_enabled is git_enabled for view in run.trace)


# --- column scrolling ----------------------------------------------------------


def _offsets(run):
    return [view.column_offset for view in run.trace]


def test_run_arrows_scroll_and_clamp_overshoot(mocker, tmp_path):
    run = drive(
        mocker,
        ["right"] * 12 + ["left"],
        [wide_group(tmp_path)],
        view_state=ViewState(columns=WIDE),
        size=(44, 25),
    )
    offsets = _offsets(run)
    peak = max(offsets)
    assert peak > 0
    assert offsets[-2:] == [peak, peak - 1]


@pytest.mark.parametrize("width", [32, 33, 34])
def test_run_narrow_arrows_reach_final_column_through_returned_offsets(mocker, tmp_path, width):
    enabled = ("name", "mode", "state", "created", "network")
    run = drive(
        mocker,
        ["right"] * 6 + ["left"],
        [wide_group(tmp_path)],
        view_state=ViewState(columns=enabled),
        size=(width, 25),
    )
    assert _offsets(run) == [0, 1, 2, 3, 3, 3, 3, 2]
    rendered = [render_text(view, (width, 25)) for view in run.trace]
    assert "NET" in header(rendered[-2]).split()
    assert LEFT_MORE in header(rendered[-2]) and RIGHT_MORE not in header(rendered[-2])
    assert all(len(line) <= width for text in rendered for line in text.splitlines())


@pytest.mark.parametrize("initially_folded", [False, True])
def test_repo_menu_fold_refreshes_scroll_snapshot(mocker, tmp_path, initially_folded):
    mocker.patch.object(tsession, "save_view_state")
    group = wide_group(tmp_path)
    enabled = ("name", "mode", "state", "created", "network")
    run = drive(
        mocker,
        ["right"] * 6 + repo_menu_keys(group, "fold") + ["right"] * 3,
        [group],
        view_state=ViewState(
            columns=enabled,
            folded=frozenset({"alpha"}) if initially_folded else frozenset(),
        ),
        size=(34, 25),
    )
    before = run.trace[6]
    final = run.last
    assert (before.column_offset == 0) if initially_folded else (before.column_offset > 0)
    assert set(final.shown_columns) == (set(enabled) if initially_folded else {"name"})
    assert final.column_offset == (3 if initially_folded else 0)
    text = frame_at(
        [group], width=34, offset=final.column_offset, enabled=enabled, folded=final.folded
    )
    if initially_folded:
        assert "NET" in header(text).split()
    else:
        assert LEFT_MORE not in text and RIGHT_MORE not in text


def test_repo_menu_fold_reclamps_without_resetting_optimized_widths(mocker, tmp_path):
    mocker.patch.object(tsession, "save_view_state")
    group = wide_group(tmp_path)
    other = dataclasses.replace(group, prefix="beta")
    run = drive(
        mocker,
        ["o"] + ["right"] * 6 + repo_menu_keys(group, "fold"),
        [group, other],
        view_state=ViewState(columns=WIDE),
        size=(32, 25),
    )
    before, after = run.trace[7], run.last
    assert before.column_offset > 0
    assert after.column_offset == before.column_offset
    assert after.column_widths == before.column_widths
    assert after.column_widths is not None
    assert set(after.shown_columns) == set(WIDE)


def test_run_arrows_clamp_after_resize_before_stepping(mocker, tmp_path):
    group = wide_group(tmp_path)
    new_maximum = dcolumns.clamp_column_offset(
        [group],
        12,
        now=datetime(2026, 6, 8, tzinfo=UTC),
        enabled=WIDE,
        folded=frozenset(),
        column_widths=None,
        shown_columns=None,
        width=52,
    )

    run = drive(
        mocker,
        [*["right"] * 12, Resize(52, 25), "left"],
        [group],
        view_state=ViewState(columns=WIDE),
        size=(44, 25),
    )
    offsets = _offsets(run)
    assert 0 < new_maximum < max(offsets)
    assert offsets[-1] == new_maximum - 1


@pytest.mark.parametrize("reset_keys", [["o"], ["S", "space", "escape"]])
def test_run_reset_the_offset(mocker, tmp_path, reset_keys):
    mocker.patch.object(tsession, "save_view_state")
    run = drive(
        mocker,
        ["right", "right", *reset_keys],
        [wide_group(tmp_path)],
        view_state=ViewState(columns=WIDE),
        size=(44, 25),
    )
    offsets = _offsets(run)
    assert max(offsets) > 0 and offsets[-1] == 0


@pytest.mark.parametrize(
    "open_keys, overlay_type",
    [
        (["h"], str),
        (["enter"], tmenu.RepoMenuState),
        (["j", "enter"], tmenu.MenuState),
    ],
)
def test_run_arrows_are_ignored_while_an_overlay_is_open(mocker, tmp_path, open_keys, overlay_type):
    run = drive(
        mocker,
        ["right", *open_keys, "right", "left", "escape"],
        [wide_group(tmp_path)],
        view_state=ViewState(columns=WIDE),
        size=(44, 25),
    )
    offsets = _offsets(run)
    assert offsets[1] > 0
    assert set(offsets[1:]) == {offsets[1]}
    assert run.of_type(overlay_type)


def test_run_arrows_clamp_after_folding(mocker, tmp_path):
    mocker.patch.object(tsession, "save_view_state")
    run = drive(
        mocker,
        ["right"] * 12 + ["space", "left"],
        [wide_group(tmp_path)],
        view_state=ViewState(columns=WIDE),
        size=(44, 25),
    )
    offsets = _offsets(run)
    assert max(offsets) > 0
    assert offsets[-2:] == [0, 0]


# --- menus -------------------------------------------------------------------------


def test_run_enters_pr_submenu_and_dispatches_leaf(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha", pr_number=7)])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    run = drive(
        mocker,
        ["j", "enter", "down", "down", "down", "enter", "down", "enter"],
        groups=[group],
    )

    assert run.rc == 0
    assert any(
        isinstance(overlay, tmenu.MenuState) and overlay.active_group == "PR →"
        for overlay in run.overlays()
    )
    assert any(call.args[0] == ["jailbee", "pr", "alpha-x"] for call in child.call_args_list)


def test_run_menu_hotkeys_open_a_group_and_dispatch_its_leaf(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    assert drive(mocker, ["j", "enter", "g", "u"], groups=[group]).rc == 0

    assert any(
        call.args[0] == ["jailbee", "git", "push", "alpha-x"] for call in child.call_args_list
    )


def test_run_menu_capital_hotkey_destroys_only_through_lifecycle(mocker, tmp_path):
    """`D` inside `Lifecycle →` reaches destroy; a lowercase `d` there does nothing."""
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    drive(mocker, ["j", "enter", "l", "d"], groups=[group])
    assert not any("destroy" in call.args[0] for call in child.call_args_list)

    drive(mocker, ["j", "enter", "l", "D"], groups=[group])
    assert any(call.args[0][:2] == ["jailbee", "destroy"] for call in child.call_args_list)


def test_run_menu_unknown_key_leaves_the_menu_untouched(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, ["j", "enter", "z"], groups=[group])

    menus = [overlay for overlay in run.overlays() if overlay]
    assert menus and isinstance(menus[-1], tmenu.MenuState) and menus[-1].index == 0
    child.assert_not_called()


def test_run_escape_backs_out_but_q_closes_submenu(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha", pr_number=7)])
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(
        mocker,
        ["j", "enter", "down", "down", "down", "enter", "escape", "enter", "q"],
        groups=[group],
    )

    overlays = run.overlays()
    menus = run.of_type(tmenu.MenuState)
    assert [menu.active_group for menu in menus] == [
        None,
        None,
        None,
        None,
        "PR →",
        None,
        "PR →",
    ]
    assert menus[5].index == 3
    assert overlays[-1] is None
    child.assert_not_called()


def test_run_vanished_container_closes_submenu(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha", pr_number=7)])

    run = drive(
        mocker,
        [
            "j",
            "enter",
            "down",
            "down",
            "down",
            "enter",
            lambda _app: group.containers.clear(),
            "down",
        ],
        groups=[group],
    )

    assert run.rc == 0
    assert any(
        isinstance(overlay, tmenu.MenuState) and overlay.active_group == "PR →"
        for overlay in run.overlays()
    )
    assert any("menu closed" in str(notice) for notice in run.notices())


def test_ssh_disabled_policy_rejects_new_before_prompt_or_spawn(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(tsession.subprocess, "run")
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="disabled"))

    run = drive(
        mocker,
        ["n", *keys("feature"), "enter", "enter"],
        groups=[group],
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    prompt.assert_not_called()
    child.assert_not_called()
    assert any("disabled" in str(notice) for notice in run.notices())
    # rejected before the first question: no prompt was ever drawn
    assert not run.of_type(tsession.TextPrompt)


def test_ssh_allowlisted_new_prompts_then_spawns_final_argv(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    prompt = mocker.patch("typer.prompt")
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")
    patch_pause(mocker)
    policy = RemoteSSHConfig(commands=RemoteCommandPolicy(mode="allowlist", allow=["new"]))

    drive(
        mocker,
        ["n", *keys("feature"), "enter", "enter"],
        groups=[group],
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    prompt.assert_not_called()
    child.assert_called_once_with(
        ["jailbee", "new", "--background", "--", "feature", "main"], check=False, cwd=tmp_path
    )


def test_ssh_inline_shell_works_when_exec_entrypoint_is_disabled(mocker, tmp_path):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    policy = RemoteSSHConfig(
        exec=False, commands=RemoteCommandPolicy(mode="allowlist", allow=["shell"])
    )

    drive(
        mocker,
        ["j", "!", *keys("shell"), "enter"],
        groups=[group],
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    child.assert_called_once_with(["jailbee", "shell", "alpha-x"], cwd=tmp_path, check=False)


@pytest.mark.parametrize("change", ["policy", "eligibility"])
def test_open_menu_rechecks_policy_and_eligibility_before_dispatch(mocker, tmp_path, change):
    from jailbee.config.models_remote import RemoteCommandPolicy, RemoteSSHConfig

    container = ci("alpha-x", "alpha")
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [container])
    child = mocker.patch.object(tsession.subprocess, "run")
    policy = RemoteSSHConfig(
        commands=RemoteCommandPolicy(mode="allowlist", allow=["tmux", "shell"])
    )

    def change_underfoot(_app) -> None:
        if change == "policy":
            policy.commands.allow[:] = ["git merge"]
        else:
            container.state = "Stopped"

    run = drive(
        mocker,
        ["j", "enter", change_underfoot, "enter"],
        [group],
        remote=True,
        over_ssh=True,
        ssh_policy=policy,
    )

    assert not any(call.args[0][0] == "jailbee" for call in child.call_args_list)
    assert any(
        notice and ("not allowed" in notice or "no longer available" in notice)
        for notice in run.notices()
    )


# --- view state ----------------------------------------------------------------


def test_run_degrades_when_save_view_state_fails(mocker):
    """A DB write failure on the keypress path (Space in settings, Fold in a repo menu,
    the settings overlay toggle) must not crash the session.

    Fails if ``persist_view_state``'s try/except is removed and the exception
    is left to propagate (it would end the dashboard with a traceback).
    """
    save = mocker.patch.object(
        tsession, "save_view_state", side_effect=OSError("database is locked")
    )
    # "S" opens the settings overlay, Space toggles the field under the
    # cursor on the Fields tab — one of the three
    # persist_view_state call sites, reached with no live groups at all.
    run = drive(mocker, ["S", "space"])

    assert run.rc == 0  # the dashboard returned normally, no exception propagated
    save.assert_called_once()  # the write was attempted, and it failed


def test_run_persists_view_state_when_the_write_succeeds(mocker):
    """Sanity check for the harness itself: the same key sequence, without
    a failing ``save_view_state``, writes through normally."""
    save = mocker.patch.object(tsession, "save_view_state")
    run = drive(mocker, ["S", "space"])

    assert run.rc == 0
    save.assert_called_once()
    _engine, frontend, state = save.call_args.args
    assert frontend == FRONTEND_TUI
    assert isinstance(state, ViewState)


def test_run_visibility_tab_uses_raw_prefixes_and_persists_complete_state(mocker, tmp_path):
    from jailbee.dashboard.settings import SettingsState

    alpha = dmodel.RepoGroup("alpha", str(tmp_path / "a"), None, [ci("alpha-one", "alpha")])
    empty = dmodel.RepoGroup("empty", str(tmp_path / "e"), None, [])
    save = mocker.patch.object(tsession, "save_view_state")

    run = drive(
        mocker,
        ["S", "tab", "tab", "down", "space"],
        [alpha, empty],
        view_state=ViewState(("name",), frozenset({"vanished"})),
    )
    assert run.rc == 0

    visibility = next(
        overlay
        for overlay in run.overlays()
        if isinstance(overlay, SettingsState) and overlay.tab == "visibility"
    )
    assert visibility.visibility_repo_prefixes == ("alpha", "empty")
    state = save.call_args.args[2]
    assert state.columns == ("name",)
    assert state.folded == frozenset({"vanished"})
    assert state.hidden_repos == frozenset({"alpha"})


@pytest.mark.parametrize("initially_folded", [False, True])
def test_repo_menu_toggles_fold_and_persists_it(mocker, tmp_path, initially_folded):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    save = mocker.patch.object(tsession, "save_view_state")

    run = drive(
        mocker,
        repo_menu_keys(group, "fold"),
        groups=[group],
        view_state=ViewState(
            folded=frozenset({"alpha"}) if initially_folded else frozenset(),
            show_empty_repos=False,
            hidden_repos=frozenset({"other"}),
        ),
    )
    assert run.rc == 0

    menus = [overlay for overlay in run.overlays() if overlay]
    assert menus[0].actions[-1] == (("Unfold" if initially_folded else "Fold"), "fold")
    assert save.call_count == 1
    assert save.call_args.args[1] == FRONTEND_TUI
    assert save.call_args.args[2].folded == (
        frozenset() if initially_folded else frozenset({"alpha"})
    )
    assert save.call_args.args[2].show_empty_repos is False
    assert save.call_args.args[2].hidden_repos == frozenset({"other"})


def test_orphan_repo_menu_only_offers_folding(mocker):
    group = dmodel.RepoGroup("orphan", None, None, [ci("orphan-x", "orphan")])
    child = mocker.patch.object(tsession.subprocess, "run")
    mocker.patch.object(tsession, "save_view_state")

    run = drive(mocker, ["enter", "enter"], groups=[group])
    menus = [overlay for overlay in run.overlays() if overlay]
    assert menus[0].actions == [("Fold", "fold")]
    child.assert_not_called()


def test_new_from_a_container_row_leaves_the_cursor_there_after_esc(mocker, tmp_path):
    group = dmodel.RepoGroup(
        "alpha", str(tmp_path), None, [ci("alpha-x", "alpha"), ci("alpha-y", "alpha")]
    )
    mocker.patch.object(tsession, "new_container_base_default", return_value="main")

    run = drive(mocker, ["j", "j", "n", *keys("fe"), "escape"], [group])
    assert run.rc == 0

    prompt_frames = [v for v in run.trace if isinstance(v.overlay, tsession.TextPrompt)]
    assert prompt_frames
    assert {v.selected for v in prompt_frames} == {dmodel.Row("container", "alpha-y")}
    assert run.last.overlay is None
    assert run.last.selected == dmodel.Row("container", "alpha-y")


def test_v_toggles_the_details_panel_and_persists_it(mocker, tmp_path):
    saved = mocker.patch.object(tsession, "save_view_state")
    run = drive(mocker, ["v"], [named_rows_group(tmp_path, 2)], view_state=ViewState())
    assert run.trace[0].show_details is True
    assert run.last.show_details is False
    assert saved.call_args.args[2].show_details is False


def test_folding_keeps_a_stored_details_preference(mocker, tmp_path):
    """Every ViewState the session saves must carry show_details, or a fold
    silently turns a hidden panel back on."""
    saved = mocker.patch.object(tsession, "save_view_state")
    drive(
        mocker,
        ["space"],  # the cursor starts on the repo heading: Space folds it
        [named_rows_group(tmp_path, 2)],
        view_state=ViewState(show_details=False),
    )
    state = saved.call_args.args[2]
    assert state.folded == frozenset({"alpha"})
    assert state.show_details is False


@pytest.mark.parametrize("path", ["fold", "settings", "repo-menu"])
def test_every_saved_view_state_keeps_a_stored_details_preference(mocker, tmp_path, path):
    """The Space fold, the settings overlay and the repo menu's Fold each save
    a whole ViewState; none may turn a stored-hidden panel back on."""
    group = named_rows_group(tmp_path, 2)
    steps = {
        "fold": ["space"],
        "settings": ["S", "space"],
        "repo-menu": repo_menu_keys(group, "fold"),
    }[path]
    saved = mocker.patch.object(tsession, "save_view_state")
    drive(mocker, steps, [group], view_state=ViewState(show_details=False))
    assert saved.call_count >= 1
    assert saved.call_args.args[2].show_details is False


# --- optimized columns -------------------------------------------------------------


def test_run_nonempty_snapshot_survives_refresh_until_optimize(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])

    def gain_a_pr(_app) -> None:
        group.containers[0].pr_number = 42

    run = drive(
        mocker,
        [gain_a_pr, "r", "o"],
        [group],
        view_state=ViewState(columns=("name", "pr")),
    )
    shown = [view.shown_columns for view in run.trace]
    # the new PR column stays hidden through the refresh, until `o` optimizes
    assert shown[0] == shown[1] == shown[2] == ("name",)
    assert "pr" in shown[3]


def test_run_space_unfold_restores_nonempty_columns_without_reoptimizing(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-one", "alpha")])
    run = drive(
        mocker,
        ["o", "space"],
        [group],
        view_state=ViewState(columns=("name", "state", "network"), folded=frozenset({"alpha"})),
    )
    initial, optimized, unfolded = run.trace[:3]
    assert initial.shown_columns == optimized.shown_columns == ("name",)
    assert unfolded.shown_columns == ("name", "state", "network")
    assert unfolded.column_widths == optimized.column_widths
    output = render_text(unfolded)
    assert "ST" in output and "NET" in output
    assert "●" in output
