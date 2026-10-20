"""Repo-level actions on Pilot: the repo menu, apply, diagnostics, config edit, service notices."""

from __future__ import annotations

import pytest

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import dispatch as ddispatch
from jailbee.dashboard import menus as dmenus
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import session as tsession
from tests.dashboard_fixtures import cfg_group, ci, repo_menu_verbs
from tests.dashboard_pilot import drive, patch_pause, repo_menu_keys

pytestmark = pytest.mark.usefixtures("no_real_branch_listing")


def test_repo_header_enter_opens_menu_without_folding(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    save = mocker.patch.object(tsession, "save_view_state")

    run = drive(mocker, ["enter"], groups=[group])
    assert run.rc == 0

    menus = [overlay for overlay in run.overlays() if overlay]
    assert menus
    assert menus[0].repo == "alpha"
    assert [
        item.label if isinstance(item, dmenus.MenuGroup) else item[0] for item in menus[0].actions
    ] == [
        "New container…",
        "New from PR…",
        "Credential group…",
        "Accounts…",
        "Network →",
        "Apply config…",
        "Diagnostics →",
        "Prune stale containers…",
        "Fold",
    ]
    save.assert_not_called()


# --- Repo-level CLI entries (apply, diagnostics, prune) ----------------------


@pytest.mark.parametrize(
    ("downs", "tail"), [(0, []), (1, ["--no-restart"])], ids=["restart", "no-restart"]
)
def test_repo_apply_runs_in_the_terminal_with_the_chosen_restart_policy(
    mocker, tmp_path, downs, tail
):
    group = cfg_group(tmp_path)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    wait = patch_pause(mocker)

    steps = [*repo_menu_keys(group, "apply"), *["j"] * downs, "enter"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    picker = run.of_type(tsession.Picker)[0]
    assert [e.value for e in picker.entries] == ["restart", "no-restart"]
    child.assert_called_once_with(
        ["jailbee", "apply", *tail, "--config", str(group.config_path)], check=False, cwd=tmp_path
    )
    wait.assert_called_once()


@pytest.mark.parametrize("key", ["escape", "ctrl+c"], ids=["escape", "ctrl-c"])
def test_repo_apply_cancel_runs_nothing(mocker, tmp_path, key):
    group = cfg_group(tmp_path)
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, [*repo_menu_keys(group, "apply"), key], [group])
    assert run.rc == 0

    child.assert_not_called()
    assert run.of_type(tsession.Picker)
    if key == "escape":
        assert "Cancelled" in run.notices()


def test_repo_apply_over_unrestricted_ssh_sends_no_config_flag(mocker, tmp_path):
    group = cfg_group(tmp_path)
    policy = RemoteSSHConfig(restrict_host=False)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    steps = [*repo_menu_keys(group, "apply", ssh_policy=policy, over_ssh=True), "enter"]
    assert drive(mocker, steps, [group], over_ssh=True, ssh_policy=policy).rc == 0

    child.assert_called_once_with(["jailbee", "apply"], check=False, cwd=tmp_path)


def test_repo_apply_is_not_offered_to_a_default_ssh_session(mocker, tmp_path):
    group = cfg_group(tmp_path)

    run = drive(
        mocker, ["enter"], [group], remote=True, over_ssh=True, ssh_policy=RemoteSSHConfig()
    )
    assert run.rc == 0

    menus = run.of_type(tmenu.RepoMenuState)
    assert menus and "apply" not in repo_menu_verbs(menus[0])


def test_repo_apply_nonzero_exit_is_a_notice(mocker, tmp_path):
    group = cfg_group(tmp_path)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 2
    patch_pause(mocker)

    run = drive(mocker, [*repo_menu_keys(group, "apply"), "enter"], [group])

    assert "'jailbee apply' exited 2" in run.notices()


def test_run_reports_a_vanished_repo_root_instead_of_crashing(mocker, tmp_path):
    """The dispatch runs the child with ``cwd=<repo root>``. If that
    directory disappears between a refresh and this keypress,
    ``subprocess.run`` raises rather than exiting non-zero, and — before the
    fix — nothing in the key loop caught it: the whole TUI went down with a
    traceback. Drives the real dispatch (not just `_dispatch_action` in
    isolation) so a regression in the `try/except` wrapped around it is what
    this test actually exercises.
    """
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    mocker.patch.object(tsession.subprocess, "run", side_effect=OSError("gone"))

    # "j" moves the highlight off the repo header onto the container row;
    # "t" (tmux) is offered for a Running container and dispatches through
    # the real dispatch, not `run_new_container`'s separate path.
    run = drive(mocker, ["j", "t"], groups=[group])

    assert run.rc == 0  # the OSError did not propagate
    assert any(n is not None and str(tmp_path) in n for n in run.notices())


def test_a_service_problem_shows_as_a_notice_without_exiting(mocker, tmp_path):
    group = dmodel.RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])

    run = drive(mocker, ["j", "j"], [group], status="refresh failed: incus is down")

    assert run.rc == 0
    assert any(notice == "refresh failed: incus is down" for notice in run.notices())


@pytest.mark.parametrize("key", ["f5", "ctrl+r"])
def test_the_refresh_keys_ask_the_service_for_a_refresh(mocker, key):
    run = drive(mocker, [key], [])

    assert run.rc == 0
    assert ("refresh",) in run.client.events


def test_run_pins_its_own_cwd_and_applies_its_scope(mocker, tmp_path):
    """Neither is baked into the shared snapshot: every frame, the dashboard
    hands its own ``cwd_root`` and ``scope`` to `present`."""
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    a = dmodel.RepoGroup("alpha", "/a", None, [])
    b = dmodel.RepoGroup("beta", str(tmp_path), None, [])
    s = dmodel.RepoGroup("secret", "/s", None, [])

    run = drive(
        mocker, [], [a, b, s], cwd_root=tmp_path, scope=RemoteRepoScope(frozenset({"secret"}))
    )

    assert [g.prefix for g in run.last.groups] == ["beta", "alpha"]


# --- config edit --------------------------------------------------------------


def test_run_dispatches_e_and_shift_e_to_edit_config(mocker, tmp_path):
    """Drive `e`/`E` through the session's real dispatch, not just
    `parse_key`/the binding shape in isolation -- a wrong key comparison or an
    inverted ``global_layer`` would be caught by nothing else.

    Asserts on the argv each keypress actually spawns (``--global`` present
    or absent), not merely that ``edit_config`` was reached, so an inverted
    ``global_layer`` fails this test rather than sailing through it.
    """
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0

    run = drive(mocker, ["e", "E"], groups=[group])

    assert run.rc == 0
    argvs = [call.args[0] for call in child.call_args_list]
    assert len(argvs) == 2
    assert argvs[0][:3] == ["jailbee", "config", "edit"]
    assert "--global" not in argvs[0]
    assert "--global" in argvs[1]


def test_remote_run_never_opens_the_config_editor(mocker, tmp_path):
    """Over remote SSH, `e`/`E` would hand the client an editor for host
    mounts and for `remote.ssh` itself — the policy that is meant to bound
    that very client. Nothing is spawned; a notice says why."""
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, ["e", "E"], groups=[group], remote=True)

    assert run.rc == 0
    child.assert_not_called()
    assert dmenus.REMOTE_CONFIG_EDIT_NOTE in run.notices()


def test_edit_config_reports_a_vanished_repo_root_instead_of_crashing(mocker, tmp_path):
    """The identical failure as the vanished-root tests for dispatch and for
    new-container, reached through the config-edit keypress: `edit_config`'s
    own ``subprocess.run(argv, cwd=repo.cwd())`` raises the same uncaught
    `OSError` if the repo root disappeared between a refresh and "e".
    Exercises `_report_vanished_repo`'s third call site rather than assuming
    the fix generalizes.
    """
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    mocker.patch.object(tsession.subprocess, "run", side_effect=OSError("gone"))

    run = drive(mocker, ["e"], groups=[group])

    assert run.rc == 0  # the OSError did not propagate
    assert any(n is not None and str(tmp_path) in n for n in run.notices())


# --- diagnostics and prune ----------------------------------------------------


def test_repo_doctor_is_paged_locally(mocker, tmp_path):
    group = cfg_group(tmp_path)
    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    paged = mocker.patch.object(ddispatch, "_run_paged", return_value=0)
    child = mocker.patch.object(tsession.subprocess, "run")

    assert drive(mocker, repo_menu_keys(group, "doctor"), [group]).rc == 0

    paged.assert_called_once_with(
        ["jailbee", "doctor", "--config", str(group.config_path)], ["less", "-R"], tmp_path
    )
    child.assert_not_called()


def test_repo_doctor_over_ssh_pauses_instead_of_paging_and_sends_no_config(mocker, tmp_path):
    group = cfg_group(tmp_path)
    policy = RemoteSSHConfig()
    mocker.patch.object(ddispatch, "pager_argv", return_value=["less", "-R"])
    paged = mocker.patch.object(ddispatch, "_run_paged")
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    wait = patch_pause(mocker)

    steps = repo_menu_keys(group, "doctor", ssh_policy=policy, over_ssh=True)
    assert drive(mocker, steps, [group], remote=True, over_ssh=True, ssh_policy=policy).rc == 0

    paged.assert_not_called()
    child.assert_called_once_with(["jailbee", "doctor"], check=False, cwd=tmp_path)
    wait.assert_called_once()


@pytest.mark.parametrize(
    ("verb", "argv"),
    [("disk-usage", ["disk-usage"]), ("prune", ["prune"])],
    ids=["disk-usage", "prune"],
)
@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_repo_disk_usage_and_prune_run_in_the_terminal_with_a_pause(
    mocker, tmp_path, verb, argv, over_ssh
):
    group = cfg_group(tmp_path)
    policy = RemoteSSHConfig() if over_ssh else None
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    wait = patch_pause(mocker)

    steps = repo_menu_keys(group, verb, ssh_policy=policy, over_ssh=over_ssh)
    run = drive(mocker, steps, [group], remote=over_ssh, over_ssh=over_ssh, ssh_policy=policy)
    assert run.rc == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    child.assert_called_once_with(["jailbee", *argv, *flags], check=False, cwd=tmp_path)
    assert "--yes-to-all" not in child.call_args.args[0]
    wait.assert_called_once()


def test_repo_doctor_failure_is_a_notice(mocker, tmp_path):
    group = cfg_group(tmp_path)
    mocker.patch.object(ddispatch, "pager_argv", return_value=None)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 1
    patch_pause(mocker)

    run = drive(mocker, repo_menu_keys(group, "doctor"), [group])

    assert "'jailbee doctor' exited 1" in run.notices()


# --- advisory entries in the repo menu ---------------------------------------


def _owing_group(tmp_path, prefix, *specs):
    group = cfg_group(tmp_path)
    group.prefix = prefix
    group.advisories = tuple(
        dmodel.RepoAdvisory(key, prefix, ("l",), "fp", f"{key} owed", action)
        for key, action in specs
    )
    return group


_BOTH = (("apply", "apply"), ("base-build", "base_build"))


def test_repo_menu_offers_advisory_entries(tmp_path):
    groups = [_owing_group(tmp_path, "alpha", *_BOTH), _owing_group(tmp_path, "beta", *_BOTH)]
    verbs = repo_menu_verbs(tmenu.open_repo_menu(groups, "alpha", frozenset()))
    assert {"base-build", "dismiss-advisories", "upgrade-all"} <= verbs
    menu = tmenu.open_repo_menu(groups, "alpha", frozenset())
    order = [i[1] for i in menu.actions if not isinstance(i, dmenus.MenuGroup)]
    assert order.index("apply") + 1 == order.index("base-build")
    assert order.count("apply") == 1


def test_repo_menu_hides_them_without_advisories(tmp_path):
    groups = [cfg_group(tmp_path)]
    verbs = repo_menu_verbs(tmenu.open_repo_menu(groups, "alpha", frozenset()))
    assert not verbs & {"base-build", "dismiss-advisories", "upgrade-all"}


def test_repo_menu_upgrade_all_counts_hidden_repos(tmp_path):
    alpha = _owing_group(tmp_path, "alpha", ("apply", "apply"))
    beta = _owing_group(tmp_path, "beta", ("apply", "apply"))
    visible = [alpha]  # beta is hidden by a visibility preference
    assert "upgrade-all" not in repo_menu_verbs(tmenu.open_repo_menu(visible, "alpha", frozenset()))
    menu = tmenu.open_repo_menu(visible, "alpha", frozenset(), all_groups=[alpha, beta])
    assert "upgrade-all" in repo_menu_verbs(menu)


def test_repo_menu_upgrade_all_needs_two_owing_repos(tmp_path):
    groups = [_owing_group(tmp_path, "alpha", ("apply", "apply"))]
    verbs = repo_menu_verbs(tmenu.open_repo_menu(groups, "alpha", frozenset()))
    assert "dismiss-advisories" in verbs
    assert "upgrade-all" not in verbs


def test_repo_menu_hides_them_over_ssh(tmp_path):
    groups = [_owing_group(tmp_path, "alpha", *_BOTH), _owing_group(tmp_path, "beta", *_BOTH)]
    verbs = repo_menu_verbs(
        tmenu.open_repo_menu(
            groups, "alpha", frozenset(), ssh_policy=RemoteSSHConfig(), over_ssh=True
        )
    )
    assert not verbs & {"base-build", "dismiss-advisories", "upgrade-all"}


def test_dismiss_menu_picker_dismisses_chosen_advisory(mocker, tmp_path):
    from tests.dashboard_pilot import bare_session

    group = _owing_group(tmp_path, "alpha", *_BOTH)
    session, _t = bare_session(mocker, [group])
    dismiss = mocker.patch("jailbee.dashboard.advisories.dismiss_advisory")
    session.overlay = tmenu.open_repo_menu([group], "alpha", frozenset())
    session.menu_chosen("dismiss-advisories", None, 0)
    picker = session.overlay
    assert picker.purpose == "repo-dismiss-advisory"
    assert [e.label for e in picker.entries] == ["apply owed", "base-build owed"]
    session.submit_picker(picker, picker.entries[1])
    dismiss.assert_called_once_with(group.advisories[1])


def test_dismissed_advisory_disappears_before_the_next_snapshot(mocker, tmp_path):
    from tests.dashboard_pilot import bare_session

    group = _owing_group(tmp_path, "alpha", *_BOTH)
    session, _t = bare_session(mocker, [group])
    mocker.patch("jailbee.dashboard.advisories.dismiss_advisory")
    key = group.advisories[0].key
    session.dismiss_advisory("alpha", key)
    session.tick()  # the client still serves the old snapshot
    shown = next(g for g in session.groups if g.prefix == "alpha")
    assert key not in [a.key for a in shown.advisories]
    assert len(shown.advisories) == len(group.advisories) - 1


def test_repo_menu_upgrade_all_not_offered_for_one_repo_owing_both(tmp_path):
    groups = [_owing_group(tmp_path, "alpha", *_BOTH), _owing_group(tmp_path, "beta")]
    verbs = repo_menu_verbs(tmenu.open_repo_menu(groups, "alpha", frozenset()))
    assert "base-build" in verbs
    assert "upgrade-all" not in verbs
