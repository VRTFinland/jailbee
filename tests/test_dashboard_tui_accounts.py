"""Credential groups and the Accounts panel on Pilot: pickers, prompts, panel questions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import session as tsession
from tests.dashboard_fixtures import (
    ACCOUNT_LS,
    TEAM_ROWS,
    alpha_group,
    ci,
    fake_account_cli,
    fake_accounts_cli,
    groups_listing,
)
from tests.dashboard_pilot import (
    OPEN_REPO_GROUP_PICKER,
    drive,
    keys,
    open_container_group_picker,
)

# --- Credential group… in the repo menu --------------------------------------


def test_repo_credential_group_flow_sets_the_chosen_group(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    cli = fake_account_cli(mocker, listing=groups_listing(TEAM_ROWS))
    child = mocker.patch.object(tsession.subprocess, "run")

    # the picker opens on its first entry, the only group: "team"
    run = drive(mocker, [*OPEN_REPO_GROUP_PICKER, "enter"], [group])
    assert run.rc == 0

    assert cli.call_args_list == [
        mocker.call(tsession.da.group_ls_argv(), cwd=tmp_path),
        mocker.call(["account", "group", "set", "team"], cwd=tmp_path),
    ]
    child.assert_not_called()  # quiet: the terminal was never handed over
    shown = [i for i, v in enumerate(run.trace) if isinstance(v.overlay, tsession.Picker)]
    assert shown, "the group picker was never drawn"
    picker = run.trace[shown[0]].overlay
    assert (picker.purpose, picker.target, picker.title) == (
        "repo-group",
        "alpha",
        "Credential group — alpha",
    )
    assert run.trace[shown[0]].selected == dmodel.Row("repo", "alpha")
    assert run.last.overlay is None
    assert run.last.notice == "Set."


@pytest.mark.parametrize("cancel", ["escape", "ctrl+c"], ids=["esc", "ctrl-c"])
def test_credential_group_picker_offers_none_and_host_default_even_with_no_groups(
    mocker, tmp_path, cancel
):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    cli = fake_account_cli(mocker, listing=groups_listing("[]"))

    run = drive(mocker, [*OPEN_REPO_GROUP_PICKER, cancel], [group])
    assert run.rc == 0

    pickers = run.of_type(tsession.Picker)
    assert [e.label for e in pickers[0].entries] == [
        "none (this repo keeps its own login)",
        "Use the host default",
        "New group…",
    ]
    assert cli.call_count == 1  # the listing; the cancel ran nothing
    # a frame after the cancel: the picker closed, the dashboard did not
    assert run.last.overlay is None


def test_ctrl_c_at_the_group_picker_cancels_it_and_the_dashboard_keeps_running(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    cli = fake_account_cli(mocker, listing=groups_listing("[]"))

    # cancel the picker, then Enter on the repo header must open its menu again
    run = drive(mocker, [*OPEN_REPO_GROUP_PICKER, "ctrl+c", "enter"], [group])
    assert run.rc == 0

    picker_at = max(i for i, v in enumerate(run.trace) if isinstance(v.overlay, tsession.Picker))
    cancelled = run.trace[picker_at + 1]
    assert cancelled.overlay is None
    assert cancelled.notice == "Cancelled"
    assert isinstance(run.trace[picker_at + 2].overlay, tmenu.RepoMenuState)
    assert cli.call_count == 1


def test_ctrl_c_without_an_overlay_still_quits(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])

    run = drive(mocker, ["ctrl+c"], [group])

    assert run.rc == 0
    assert run.steps_taken == 1  # the first Ctrl-C ended the session


def test_credential_group_picker_lists_each_group_once_in_order(mocker, tmp_path):
    rows = (
        '[{"agent": "claude", "group": "team", "account": "a", "state": "live",'
        ' "repos": [], "containers": []},'
        ' {"agent": "codex", "group": "team", "account": null, "state": "empty",'
        ' "repos": [], "containers": []},'
        ' {"agent": "claude", "group": "solo", "account": null, "state": "empty",'
        ' "repos": [], "containers": []},'
        ' {"agent": "claude", "group": null, "account": "b", "state": "parked",'
        ' "repos": [], "containers": []}]'
    )
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    fake_account_cli(mocker, listing=groups_listing(rows))

    run = drive(mocker, [*OPEN_REPO_GROUP_PICKER, "escape"], [group])
    assert run.rc == 0

    entries = run.of_type(tsession.Picker)[0].entries
    assert [(e.label, e.value) for e in entries[:2]] == [("solo", "solo"), ("team", "team")]
    assert len(entries) == 5


def test_credential_group_picker_hides_a_legacy_group_named_none(mocker, tmp_path):
    """`none` spells "no group"; a legacy group of that name must not be offered twice."""
    rows = (
        '[{"agent": "claude", "group": "none", "account": null, "state": "empty",'
        ' "repos": [], "containers": []},'
        ' {"agent": "claude", "group": "team", "account": null, "state": "empty",'
        ' "repos": [], "containers": []}]'
    )
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    fake_account_cli(mocker, listing=groups_listing(rows))

    run = drive(mocker, [*OPEN_REPO_GROUP_PICKER, "escape"], [group])
    assert run.rc == 0

    entries = run.of_type(tsession.Picker)[0].entries
    assert [(e.label, e.value) for e in entries] == [
        ("team", "team"),
        ("none (this repo keeps its own login)", "none"),
        ("Use the host default", "__unset__"),
        ("New group…", "__new__"),
    ]


@pytest.mark.parametrize(
    ("downs", "argv"),
    [
        (0, ["account", "group", "set", "none"]),
        (1, ["account", "group", "unset"]),
    ],
    ids=["none", "host-default"],
)
def test_credential_group_picker_non_group_choices_run_their_command(mocker, tmp_path, downs, argv):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    cli = fake_account_cli(mocker, listing=groups_listing("[]"))

    run = drive(mocker, [*OPEN_REPO_GROUP_PICKER, *["j"] * downs, "enter"], [group])
    assert run.rc == 0

    assert cli.call_args_list[-1] == mocker.call(argv, cwd=tmp_path)
    assert cli.call_count == 2


@pytest.mark.parametrize(
    ("listing", "reason"),
    [
        (tsession.da.CliResult(False, "error: boom"), "boom"),
        (tsession.da.CliResult(True, "done", "not json"), "unexpected output"),
    ],
    ids=["command-failed", "garbled-output"],
)
def test_credential_group_listing_failure_is_a_notice_not_a_traceback(
    mocker, tmp_path, listing, reason
):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    cli = fake_account_cli(mocker, listing=listing)

    run = drive(mocker, OPEN_REPO_GROUP_PICKER, [group])
    assert run.rc == 0

    assert cli.call_count == 1
    assert not run.of_type(tsession.Picker)
    assert run.last.overlay is None
    assert "could not list credential groups" in run.last.notice
    assert reason in run.last.notice


def test_new_group_name_prompt_esc_runs_nothing(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    cli = fake_account_cli(mocker, listing=groups_listing(TEAM_ROWS))

    # team, none, host default, New group…
    steps = [*OPEN_REPO_GROUP_PICKER, *["j"] * 3, "enter", *keys("fresh"), "escape"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    prompts = run.of_type(tsession.TextPrompt)
    assert prompts and prompts[0].purpose == "repo-group-name"
    assert prompts[-1].text == "fresh"
    assert cli.call_count == 1  # only the listing
    assert run.last.overlay is None
    assert run.last.notice == "Cancelled"


def test_new_group_name_prompt_sets_the_typed_group(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    cli = fake_account_cli(mocker, listing=groups_listing("[]"))

    steps = [*OPEN_REPO_GROUP_PICKER, *["j"] * 2, "enter", *keys("fresh"), "enter"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    assert cli.call_args_list[-1] == mocker.call(["account", "group", "set", "fresh"], cwd=tmp_path)


@pytest.mark.parametrize(
    ("change", "stays_up"),
    [
        (tsession.da.CliResult(False, "an agent is running; pass --force"), True),
        (tsession.da.CliResult(True, "This repo now uses group `team`."), False),
    ],
    ids=["failure", "success"],
)
def test_account_command_failure_shows_a_long_notice(mocker, tmp_path, change, stays_up):
    """A refusal outlives the ordinary 2.5 s notice; a success does not."""
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    fake_account_cli(mocker, listing=groups_listing(TEAM_ROWS), change=change)
    clock = [1000.0]
    # Only the session's clock: patching `time.monotonic` itself would stall asyncio.
    mocker.patch.object(tsession, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def advance(_app):
        clock[0] += 5.0  # the harness ticks after a callable step: expired notices go

    run = drive(mocker, [*OPEN_REPO_GROUP_PICKER, "enter", advance], [group])
    assert run.rc == 0

    notices = run.notices()
    assert change.message in notices  # shown right after the command
    # the frame drawn 5 s later, and the dashboard still running to draw it
    assert (notices[-1] == change.message) is stays_up


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "over-ssh"])
def test_repo_credential_group_config_flag_is_local_only(mocker, tmp_path, over_ssh):
    config_path = tmp_path / ".jailbee" / "config.yaml"
    group = dmodel.RepoGroup("alpha", str(tmp_path), config_path, [])
    cli = fake_account_cli(mocker, listing=groups_listing(TEAM_ROWS))

    run = drive(
        mocker,
        [*OPEN_REPO_GROUP_PICKER, "enter"],
        [group],
        remote=over_ssh,
        over_ssh=over_ssh,
        ssh_policy=RemoteSSHConfig(restrict_host=False) if over_ssh else None,
    )
    assert run.rc == 0

    flags = [] if over_ssh else ["--config", str(config_path)]
    assert [c.args[0] for c in cli.call_args_list] == [
        [*tsession.da.group_ls_argv(), *flags],
        ["account", "group", "set", "team", *flags],
    ]


# --- Credential group… in the container menu ---------------------------------


@pytest.mark.parametrize(
    ("downs", "argv"),
    [
        (0, ["account", "group", "use", "team", "alpha-x"]),
        (1, ["account", "group", "use", "none", "alpha-x"]),
        (2, ["account", "group", "reset", "alpha-x"]),
    ],
    ids=["team", "none", "follow-the-repo"],
)
def test_container_credential_group_flow_uses_the_container_and_reset(
    mocker, tmp_path, downs, argv
):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    cli = fake_account_cli(mocker, listing=groups_listing(TEAM_ROWS))
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*open_container_group_picker(group), *["j"] * downs, "enter"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    assert cli.call_args_list == [
        mocker.call(tsession.da.group_ls_argv(), cwd=tmp_path),
        mocker.call(argv, cwd=tmp_path),
    ]
    child.assert_not_called()  # never dispatched to the CLI as a menu verb
    shown = [i for i, v in enumerate(run.trace) if isinstance(v.overlay, tsession.Picker)]
    picker = run.trace[shown[0]].overlay
    assert (picker.purpose, picker.target) == ("container-group", "alpha-x")
    assert [e.label for e in picker.entries] == [
        "team",
        "none (this container keeps its own login)",
        "Follow the repo's group",
        "New group…",
    ]
    # the cursor stays on the container while the picker is open
    assert {run.trace[i].selected for i in shown} == {dmodel.Row("container", "alpha-x")}
    assert run.last.overlay is None


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "over-ssh"])
def test_container_credential_group_config_flag_is_local_only(mocker, tmp_path, over_ssh):
    config_path = tmp_path / ".jailbee" / "config.yaml"
    group = dmodel.RepoGroup("alpha", str(tmp_path), config_path, [ci("alpha-x", "alpha")])
    cli = fake_account_cli(mocker, listing=groups_listing(TEAM_ROWS))
    policy = RemoteSSHConfig(restrict_host=False) if over_ssh else None
    menu_kwargs = {"remote": over_ssh, "over_ssh": over_ssh, "ssh_policy": policy}

    steps = [*open_container_group_picker(group, **menu_kwargs), "enter"]
    run = drive(mocker, steps, [group], **menu_kwargs)
    assert run.rc == 0

    flags = [] if over_ssh else ["--config", str(config_path)]
    assert [c.args[0] for c in cli.call_args_list] == [
        [*tsession.da.group_ls_argv(), *flags],
        ["account", "group", "use", "team", "alpha-x", *flags],
    ]


def test_container_new_group_name_prompt_uses_the_typed_group(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    cli = fake_account_cli(mocker, listing=groups_listing("[]"))

    # none, Follow the repo's group, New group…
    steps = [*open_container_group_picker(group), *["j"] * 2, "enter", *keys("fresh"), "enter"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    prompts = run.of_type(tsession.TextPrompt)
    assert {(p.purpose, p.target) for p in prompts} == {("container-group-name", "alpha-x")}
    assert cli.call_args_list[-1] == mocker.call(
        ["account", "group", "use", "fresh", "alpha-x"], cwd=tmp_path
    )
    prompt_views = [v for v in run.trace if isinstance(v.overlay, tsession.TextPrompt)]
    assert {v.selected for v in prompt_views} == {dmodel.Row("container", "alpha-x")}


@pytest.mark.parametrize(
    "tail",
    [
        ["escape"],
        ["ctrl+c"],
        [*["j"] * 2, "enter", *keys("fresh"), "escape"],
        [*["j"] * 2, "enter", *keys("fresh"), "ctrl+c"],
    ],
    ids=["esc-at-picker", "ctrl-c-at-picker", "esc-at-name-prompt", "ctrl-c-at-name-prompt"],
)
def test_container_credential_group_esc_runs_nothing(mocker, tmp_path, tail):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    cli = fake_account_cli(mocker, listing=groups_listing("[]"))
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, [*open_container_group_picker(group), *tail], [group])
    assert run.rc == 0

    assert cli.call_args_list == [mocker.call(tsession.da.group_ls_argv(), cwd=tmp_path)]
    child.assert_not_called()
    assert run.of_type(tsession.Picker)
    assert run.last.overlay is None


@pytest.mark.parametrize("when", ["frame-before-enter", "same-read-as-enter"])
def test_container_vanishing_while_the_group_picker_is_open_runs_nothing(mocker, tmp_path, when):
    """Closed by the tick's guard, or refused by the submit's own re-resolve."""
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    cli = fake_account_cli(mocker, listing=groups_listing(TEAM_ROWS))
    child = mocker.patch.object(tsession.subprocess, "run")

    if when == "frame-before-enter":
        # the tick after this step no longer lists the container: it closes the picker
        def vanish(_app):
            group.containers.clear()

    else:
        # the tick still lists the container (so the picker stays open), but its
        # repo has no directory any more: only the submit's re-resolve can catch it
        def vanish(_app):
            group.repo_root = None

    run = drive(mocker, [*open_container_group_picker(group), vanish, "enter"], [group])
    assert run.rc == 0

    assert cli.call_args_list == [mocker.call(tsession.da.group_ls_argv(), cwd=tmp_path)]
    child.assert_not_called()
    notices = " ".join(str(n) for n in run.notices())
    assert "'alpha-x' is gone" in notices


def test_container_group_flow_is_not_misdirected_by_a_repo_of_the_same_name(mocker, tmp_path):
    """Container `alpha-x` of repo `alpha` beside a repo whose prefix is `alpha-x`."""
    alpha_root, other_root = tmp_path / "alpha", tmp_path / "other"
    group = dmodel.RepoGroup("alpha", str(alpha_root), None, [ci("alpha-x", "alpha")])
    namesake = dmodel.RepoGroup("alpha-x", str(other_root), None, [ci("alpha-x-one", "alpha-x")])
    cli = fake_account_cli(mocker, listing=groups_listing(TEAM_ROWS))

    steps = [*open_container_group_picker(group), "enter"]  # "team"
    run = drive(mocker, steps, [group, namesake])
    assert run.rc == 0

    assert cli.call_args_list == [
        mocker.call(tsession.da.group_ls_argv(), cwd=alpha_root),
        mocker.call(["account", "group", "use", "team", "alpha-x"], cwd=alpha_root),
    ]
    picker_views = [v for v in run.trace if isinstance(v.overlay, tsession.Picker)]
    assert picker_views
    assert {v.selected for v in picker_views} == {dmodel.Row("container", "alpha-x")}


# --- The Accounts panel (A) --------------------------------------------------


def test_key_a_opens_the_accounts_panel_with_rows_and_keeps_the_table(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-zebra", "alpha")])
    cli = fake_accounts_cli(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, ["A"], [group], size=(200, 25), screens=True)
    assert run.rc == 0

    assert cli.call_args_list == [mocker.call(ACCOUNT_LS, cwd=tmp_path)]
    child.assert_not_called()
    views = [v for v in run.trace if isinstance(v.overlay, tsession.da.AccountsState)]
    assert views, "the Accounts panel was never drawn"
    state = views[-1].overlay
    assert [r.account for r in state.rows] == ["a@x.io#org12345", "b@x.io~2", None]
    assert (state.index, state.prefix) == (0, "alpha")
    at = max(
        i for i, view in enumerate(run.trace) if isinstance(view.overlay, tsession.da.AccountsState)
    )
    out = run.screens[at]
    assert "NAME" in out and "zebra" in out  # the container table is still drawn
    assert "credential groups and logins" in out
    assert "b@x.io~2" in out
    assert "n new group" in out  # the panel's own hint line


def test_repo_menu_accounts_opens_the_panel_in_that_repo(mocker, tmp_path):
    cli = fake_accounts_cli(mocker)
    steps = ["enter", *["j"] * 3, "enter"]  # repo header → Accounts…

    run = drive(mocker, steps, [alpha_group(tmp_path)])
    assert run.rc == 0

    assert cli.call_args_list == [mocker.call(ACCOUNT_LS, cwd=tmp_path)]
    states = run.of_type(tsession.da.AccountsState)
    assert states
    assert states[-1].prefix == "alpha"


def test_key_a_runs_the_listing_in_the_selected_rows_repo(mocker, tmp_path):
    alpha = dmodel.RepoGroup("alpha", str(tmp_path / "a"), None, [])
    beta = dmodel.RepoGroup("beta", str(tmp_path / "b"), tmp_path / "b.yaml", [])
    cli = fake_accounts_cli(mocker)

    run = drive(mocker, ["j", "A"], [alpha, beta])
    assert run.rc == 0

    assert cli.call_args_list == [
        mocker.call([*ACCOUNT_LS, "--config", str(tmp_path / "b.yaml")], cwd=tmp_path / "b")
    ]


def test_key_a_from_an_orphan_row_falls_back_to_the_first_real_repo(mocker, tmp_path):
    orphan = dmodel.RepoGroup("gamma", None, None, [ci("gamma-x", "gamma")])
    beta = dmodel.RepoGroup("beta", str(tmp_path), None, [])
    cli = fake_accounts_cli(mocker)

    run = drive(mocker, ["A"], [orphan, beta])
    assert run.rc == 0

    assert run.trace[0].selected == dmodel.Row("repo", "gamma")
    assert cli.call_args_list == [mocker.call(ACCOUNT_LS, cwd=tmp_path)]
    assert run.of_type(tsession.da.AccountsState)[-1].prefix == "beta"


def test_key_a_with_no_real_repo_is_a_notice(mocker):
    orphan = dmodel.RepoGroup("gamma", None, None, [ci("gamma-x", "gamma")])
    cli = fake_accounts_cli(mocker)

    run = drive(mocker, ["A"], [orphan])
    assert run.rc == 0

    cli.assert_not_called()
    assert run.last.overlay is None
    assert run.last.notice == "No repo to address account commands at"


@pytest.mark.parametrize(
    ("listing", "reason"),
    [
        (tsession.da.CliResult(False, "error: no pool"), "no pool"),
        (tsession.da.CliResult(True, "done", "not json"), "unexpected output"),
    ],
    ids=["command-failed", "garbled-output"],
)
def test_accounts_panel_survives_a_failing_listing(mocker, tmp_path, listing, reason):
    cli = fake_accounts_cli(mocker, listing=listing)

    # j after the failure: the dashboard is still reading keys, not crashed
    run = drive(mocker, ["A", "j"], [alpha_group(tmp_path)])
    assert run.rc == 0

    assert cli.call_count == 1
    assert not run.of_type(tsession.da.AccountsState)
    assert run.last.overlay is None
    assert run.last.notice.startswith("could not list accounts: ")
    assert reason in run.last.notice
    assert run.last.selected == dmodel.Row("container", "alpha-x")


def test_accounts_panel_with_an_empty_pool_says_so(mocker, tmp_path):
    cli = fake_accounts_cli(mocker, listing=groups_listing("[]"))

    run = drive(mocker, ["A", "enter", "j"], [alpha_group(tmp_path)], screens=True)
    assert run.rc == 0

    assert cli.call_count == 1  # Enter on nothing ran nothing
    assert isinstance(run.last.overlay, tsession.da.AccountsState)
    assert run.last.overlay.rows == ()
    assert run.last.notice == "No actions for this row"
    assert "(no logins or groups on this host)" in run.screens[-1]
    assert not run.of_type(tsession.Picker)


def test_accounts_actions_picker_offers_the_rows_actions(mocker, tmp_path):
    fake_accounts_cli(mocker)

    # row 0 (live in team), then Esc back to the panel, then row 1 (parked)
    run = drive(mocker, ["A", "enter", "escape", "j", "enter"], [alpha_group(tmp_path)])
    assert run.rc == 0

    pickers = run.of_type(tsession.Picker)
    live, parked = pickers[0], pickers[-1]
    assert (live.purpose, live.title, live.target, live.carry) == (
        "acct-action",
        "Group team (claude)",
        "alpha",
        ("claude", "team", "a@x.io#org12345"),
    )
    assert [(e.label, e.value) for e in live.entries] == [
        ("Use a stored login…", "use"),
        ("Park the live login", "park"),
    ]
    assert (parked.title, parked.carry) == ("Login b@x.io~2 (claude)", ("claude", "", "b@x.io~2"))
    assert [e.value for e in parked.entries] == ["use-in", "delete"]
    assert isinstance(parked.back, tsession.da.AccountsState)
    assert parked.back.index == 1  # the panel remembers its cursor


def test_accounts_questions_keep_the_cursor_where_the_key_was_pressed(mocker, tmp_path):
    fake_accounts_cli(mocker)

    # j: onto the container row, then A and Enter (the actions picker)
    run = drive(mocker, ["j", "A", "enter"], [alpha_group(tmp_path)])
    assert run.rc == 0

    views = [v for v in run.trace if isinstance(v.overlay, tsession.Picker)]
    assert views, "the actions picker was never drawn"
    # the picker targets the repo "alpha" but must not pin its header
    assert views[-1].selected == dmodel.Row("container", "alpha-x")


def test_accounts_park_runs_the_scoped_command_and_closes(mocker, tmp_path):
    cli = fake_accounts_cli(mocker, change=tsession.da.CliResult(True, "Parked a@x.io#org12345."))
    child = mocker.patch.object(tsession.subprocess, "run")

    # A, Enter (row 0's actions), Down to "Park the live login", Enter
    run = drive(mocker, ["A", "enter", "j", "enter"], [alpha_group(tmp_path)])
    assert run.rc == 0

    assert cli.call_args_list == [
        mocker.call(ACCOUNT_LS, cwd=tmp_path),
        mocker.call(["account", "park", "-a", "claude", "-g", "team"], cwd=tmp_path),
    ]
    child.assert_not_called()
    # Done is done: the panel closes, the CLI's own message stays as the notice.
    assert run.last.overlay is None
    assert run.last.notice == "Parked a@x.io#org12345."


def test_accounts_refused_change_keeps_the_panel_under_its_notice(mocker, tmp_path):
    refusal = tsession.da.CliResult(False, "error: an agent is running; pass --force")
    cli = fake_accounts_cli(mocker, change=refusal)

    run = drive(mocker, ["A", "enter", "j", "enter"], [alpha_group(tmp_path)])
    assert run.rc == 0

    # no reload after a refusal, and never a silent --force retry
    assert [c.args[0] for c in cli.call_args_list] == [
        ACCOUNT_LS,
        ["account", "park", "-a", "claude", "-g", "team"],
    ]
    assert isinstance(run.last.overlay, tsession.da.AccountsState)
    assert len(run.last.overlay.rows) == 3  # the listing it had before
    assert run.last.notice == "error: an agent is running; pass --force"


def test_accounts_use_stored_login_two_step(mocker, tmp_path):
    cli = fake_accounts_cli(mocker)

    # A, Enter (row 0's actions), Enter ("Use a stored login…"), Enter (the one parked login)
    run = drive(mocker, ["A", "enter", "enter", "enter"], [alpha_group(tmp_path)])
    assert run.rc == 0

    use = [p for p in run.of_type(tsession.Picker) if p.purpose == "acct-use"]
    assert use, "the stored-login picker was never drawn"
    assert use[0].title == "Use which login?"
    assert [(e.label, e.value) for e in use[0].entries] == [("b@x.io~2", "b@x.io~2")]
    assert cli.call_args_list == [
        mocker.call(ACCOUNT_LS, cwd=tmp_path),
        mocker.call(["account", "use", "b@x.io~2", "-a", "claude", "-g", "team"], cwd=tmp_path),
    ]
    assert run.last.overlay is None


@pytest.mark.parametrize(("downs", "group"), [(0, "spare"), (1, "team")])
def test_accounts_use_a_parked_login_in_a_chosen_group(mocker, tmp_path, downs, group):
    cli = fake_accounts_cli(mocker)

    # A, Down (the parked row), Enter, Enter ("Use in a group…"), [Down], Enter
    steps = ["A", "j", "enter", "enter", *["j"] * downs, "enter"]
    run = drive(mocker, steps, [alpha_group(tmp_path)])
    assert run.rc == 0

    use_in = [p for p in run.of_type(tsession.Picker) if p.purpose == "acct-use-in"]
    assert [e.value for e in use_in[0].entries] == ["spare", "team"]
    assert cli.call_args_list == [
        mocker.call(ACCOUNT_LS, cwd=tmp_path),
        mocker.call(["account", "use", "b@x.io~2", "-a", "claude", "-g", group], cwd=tmp_path),
    ]
    assert run.last.overlay is None


def test_accounts_panel_closes_when_its_repo_vanishes(mocker, tmp_path):
    groups = [alpha_group(tmp_path)]
    cli = fake_accounts_cli(mocker)

    # the repo drops out of the registry; the tick after the step closes the panel
    run = drive(mocker, ["A", lambda _app: groups.clear(), "j"], groups)
    assert run.rc == 0

    assert cli.call_count == 1
    assert run.last.overlay is None
    assert run.last.notice == "'alpha' is gone — accounts closed"


# A, Down (the parked row b@x.io~2), Enter, Down ("Delete this login…"), Enter
OPEN_DELETE_CONFIRM = ["A", "j", "enter", "j", "enter"]
# A, Down x2 (the empty "spare" group), Enter, Down ("Remove this group"), Enter
OPEN_GROUP_RM_CONFIRM = ["A", "j", "j", "enter", "j", "enter"]


@pytest.mark.parametrize(
    ("open_keys", "title", "argv"),
    [
        (
            OPEN_DELETE_CONFIRM,
            "Really delete login b@x.io~2?",
            ["account", "rm", "b@x.io~2", "-a", "claude", "--yes"],
        ),
        (
            OPEN_GROUP_RM_CONFIRM,
            "Really remove group spare?",
            ["account", "group", "rm", "spare", "--yes"],
        ),
    ],
    ids=["delete-login", "remove-group"],
)
def test_accounts_confirmation_yes_runs_the_removal_and_closes(
    mocker, tmp_path, open_keys, title, argv
):
    cli = fake_accounts_cli(mocker)

    run = drive(mocker, [*open_keys, "j", "enter"], [alpha_group(tmp_path)])
    assert run.rc == 0

    confirm = next(p for p in run.of_type(tsession.Picker) if p.purpose == "acct-confirm")
    assert confirm.title == title
    assert [(e.label, e.value) for e in confirm.entries] == [
        ("No", "no"),
        ("Yes, delete", "yes"),
    ]
    assert confirm.index == 0  # "No" is where the cursor starts
    assert cli.call_args_list == [
        mocker.call(ACCOUNT_LS, cwd=tmp_path),
        mocker.call(argv, cwd=tmp_path),
    ]
    assert run.last.overlay is None


@pytest.mark.parametrize(
    "open_keys",
    [OPEN_DELETE_CONFIRM, OPEN_GROUP_RM_CONFIRM],
    ids=["delete-login", "remove-group"],
)
def test_accounts_confirmation_stray_enter_removes_nothing(mocker, tmp_path, open_keys):
    cli = fake_accounts_cli(mocker)

    run = drive(mocker, [*open_keys, "enter"], [alpha_group(tmp_path)])
    assert run.rc == 0

    assert cli.call_args_list == [mocker.call(ACCOUNT_LS, cwd=tmp_path)]
    confirm_at = max(
        i
        for i, v in enumerate(run.trace)
        if isinstance(v.overlay, tsession.Picker) and v.overlay.purpose == "acct-confirm"
    )
    back = run.trace[confirm_at + 1].overlay
    assert isinstance(back, tsession.da.AccountsState)
    assert back is run.trace[confirm_at].overlay.back  # the same panel, not reloaded


def test_accounts_new_group_prompt_creates_the_typed_group_and_closes(mocker, tmp_path):
    cli = fake_accounts_cli(mocker, change=tsession.da.CliResult(True, "Created group spare2."))

    run = drive(mocker, ["A", "n", *keys("spare2"), "enter"], [alpha_group(tmp_path)])
    assert run.rc == 0

    prompt = run.of_type(tsession.TextPrompt)[0]
    assert (prompt.purpose, prompt.title, prompt.label, prompt.target) == (
        "acct-group-new",
        "New credential group",
        "Group name",
        "alpha",
    )
    assert cli.call_args_list == [
        mocker.call(ACCOUNT_LS, cwd=tmp_path),
        mocker.call(["account", "group", "create", "spare2"], cwd=tmp_path),
    ]
    assert run.last.overlay is None
    assert run.last.notice == "Created group spare2."


def test_accounts_new_group_prompt_rejects_a_blank_name_inline(mocker, tmp_path):
    cli = fake_accounts_cli(mocker)

    run = drive(mocker, ["A", "n", "space", "enter", "z"], [alpha_group(tmp_path)])
    assert run.rc == 0

    assert cli.call_count == 1  # the listing only
    prompts = run.of_type(tsession.TextPrompt)
    assert prompts[-1].error is None and prompts[-1].text == " z"  # still editing after
    assert any(p.error == "Group name cannot be empty" for p in prompts)


# How to reach each question the panel can ask, and what it is.
ACCOUNT_QUESTIONS = pytest.mark.parametrize(
    ("open_keys", "purpose"),
    [
        (["A", "enter"], "acct-action"),
        (["A", "enter", "enter"], "acct-use"),
        (["A", "j", "enter", "enter"], "acct-use-in"),
        (OPEN_DELETE_CONFIRM, "acct-confirm"),
        (OPEN_GROUP_RM_CONFIRM, "acct-confirm"),
        (["A", "n", *keys("x")], "acct-group-new"),
    ],
    ids=["actions", "use", "use-in", "confirm-delete", "confirm-group-rm", "name-prompt"],
)


@ACCOUNT_QUESTIONS
@pytest.mark.parametrize("cancel", ["escape", "ctrl+c"], ids=["esc", "ctrl-c"])
def test_accounts_cancel_at_every_question_returns_to_the_panel(
    mocker, tmp_path, open_keys, purpose, cancel
):
    cli = fake_accounts_cli(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    # after the cancel, `j` must move the panel's cursor: still open, still live
    run = drive(mocker, [*open_keys, cancel, "j"], [alpha_group(tmp_path)])
    assert run.rc == 0

    asked_at = max(
        i
        for i, v in enumerate(run.trace)
        if isinstance(v.overlay, (tsession.Picker, tsession.TextPrompt))
    )
    question = run.trace[asked_at].overlay
    assert question.purpose == purpose
    after = run.trace[asked_at + 1].overlay
    assert after is question.back
    assert isinstance(after, tsession.da.AccountsState)
    assert run.trace[asked_at + 1].notice == "Cancelled"  # Esc says so, like Ctrl-C
    moved = run.trace[asked_at + 2].overlay
    assert isinstance(moved, tsession.da.AccountsState)
    assert moved.index == min(after.index + 1, len(after.rows) - 1)
    assert cli.call_args_list == [mocker.call(ACCOUNT_LS, cwd=tmp_path)]
    child.assert_not_called()


@pytest.mark.parametrize(
    "open_keys",
    [
        ["A", "enter"],
        ["A", "enter", "enter"],
        ["A", "j", "enter", "enter"],
        OPEN_DELETE_CONFIRM,
    ],
    ids=["actions", "use", "use-in", "confirm-delete"],
)
def test_q_at_an_accounts_picker_steps_back_one_level_like_esc(mocker, tmp_path, open_keys):
    """`q` in a nested picker must not close the whole Accounts panel."""
    cli = fake_accounts_cli(mocker)

    run = drive(mocker, [*open_keys, "q"], [alpha_group(tmp_path)])
    assert run.rc == 0

    asked_at = max(i for i, v in enumerate(run.trace) if isinstance(v.overlay, tsession.Picker))
    after = run.trace[asked_at + 1]
    assert after.overlay is run.trace[asked_at].overlay.back
    assert isinstance(after.overlay, tsession.da.AccountsState)
    assert after.notice == "Cancelled"
    assert cli.call_args_list == [mocker.call(ACCOUNT_LS, cwd=tmp_path)]


def test_esc_at_a_top_level_picker_closes_it_with_a_cancelled_notice(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [])
    fake_account_cli(mocker, listing=groups_listing("[]"))

    run = drive(mocker, [*OPEN_REPO_GROUP_PICKER, "escape"], [group])
    assert run.rc == 0

    assert (run.last.overlay, run.last.notice) == (None, "Cancelled")


@pytest.mark.parametrize("cancel", ["escape", "q"], ids=["esc", "q"])
def test_accounts_esc_or_q_on_the_panel_closes_it(mocker, tmp_path, cancel):
    cli = fake_accounts_cli(mocker)

    # j after closing moves the table cursor: the dashboard is back in the main view
    run = drive(mocker, ["A", cancel, "j"], [alpha_group(tmp_path)])
    assert run.rc == 0

    assert run.last.overlay is None
    assert run.last.selected == dmodel.Row("container", "alpha-x")
    assert cli.call_count == 1


def test_ctrl_c_on_the_bare_accounts_panel_quits(mocker, tmp_path):
    """A documented choice: the panel has no text input, so Ctrl-C keeps its
    generic meaning there (quit), unlike the questions opened from it."""
    fake_accounts_cli(mocker)
    open_keys = ["A"]

    run = drive(mocker, [*open_keys, "ctrl+c"], [alpha_group(tmp_path)])

    assert run.rc == 0
    assert run.steps_taken == len(open_keys) + 1  # the Ctrl-C at the panel ended the session


@pytest.mark.parametrize("when", ["frame-before-enter", "same-read-as-enter"])
@pytest.mark.parametrize(
    "open_keys",
    [
        ["A", "enter", "j"],  # the actions picker, on "Park the live login"
        ["A", "n", *keys("spare2")],  # the new-group prompt, answer typed
    ],
    ids=["park-picker", "name-prompt"],
)
def test_accounts_repo_vanishing_while_a_question_is_open_runs_nothing(
    mocker, tmp_path, open_keys, when
):
    group = alpha_group(tmp_path)
    groups = [group]
    cli = fake_accounts_cli(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    if when == "frame-before-enter":
        # the tick after this step no longer lists the repo: it closes the question
        def vanish(_app):
            groups.clear()

    else:
        # the tick still lists the repo (the question stays open), but it has no
        # directory any more: only the submit's re-resolve can catch it
        def vanish(_app):
            group.repo_root = None

    run = drive(mocker, [*open_keys, vanish, "enter"], groups)
    assert run.rc == 0

    assert cli.call_args_list == [mocker.call(ACCOUNT_LS, cwd=tmp_path)]
    child.assert_not_called()
    notices = " ".join(str(n) for n in run.notices())
    assert "'alpha' is gone" in notices
