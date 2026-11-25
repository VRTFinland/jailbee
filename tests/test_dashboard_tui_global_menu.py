"""Host-wide actions through the shared menu and the terminal dashboard pilot."""

from __future__ import annotations

import pytest

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import menus
from jailbee.dashboard.hit import Hit
from jailbee.dashboard.tui.menu_state import GlobalMenuState
from tests.dashboard_pilot import Click, drive


@pytest.mark.parametrize("opener", ["g", "f10", Click(Hit("global-menu"))])
def test_global_menu_opens_without_a_selection_and_closes_on_escape(mocker, opener):
    run = drive(mocker, [opener, "escape"], [])
    assert isinstance(run.trace[1].overlay, GlobalMenuState)
    assert run.natives[1].kind == "menu"
    assert run.trace[2].overlay is None


def test_global_menu_outside_click_closes_it(mocker, tmp_path):
    from jailbee.dashboard.model import RepoGroup, Row
    from tests.dashboard_fixtures import ci

    group = RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    run = drive(mocker, ["g", Click(Hit("row", ("alpha-x",)))], [group])
    assert run.last.overlay is None
    assert run.last.selected == Row("container", "alpha-x")


def test_git_accelerator_still_works_inside_container_menu(mocker, tmp_path):
    from jailbee.dashboard.model import RepoGroup
    from tests.dashboard_fixtures import ci

    group = RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    run = drive(mocker, ["j", "enter", "g"], [group])
    assert run.natives[3].level == "Git →"


@pytest.mark.parametrize(("index", "shortcut"), [(1, "A"), (2, "Q"), (3, "E"), (4, "f2"), (5, "h")])
def test_global_entries_do_what_their_existing_shortcuts_do(mocker, tmp_path, index, shortcut):
    from jailbee.dashboard.model import RepoGroup
    from jailbee.dashboard.tui import session as tsession
    from tests.dashboard_fixtures import ci, fake_accounts_cli
    from tests.dashboard_pilot import Pick

    group = RepoGroup("alpha", str(tmp_path), None, [ci("alpha-x", "alpha")])
    fake_accounts_cli(mocker)
    child = mocker.patch.object(tsession.subprocess, "run", return_value=mocker.Mock(returncode=0))
    direct = drive(mocker, [shortcut], [group])
    direct_calls = child.call_args_list[:]
    child.reset_mock()
    menu = drive(mocker, ["g", Pick(index)], [group])
    assert menu.trace[2].overlay == direct.trace[1].overlay
    assert child.call_args_list == direct_calls


def test_quit_entry_exits_instead_of_only_closing_the_menu(mocker):
    from tests.dashboard_pilot import Pick

    run = drive(mocker, ["g", Pick(6)], [])
    assert run.steps_taken == 2
    assert run.app.quit_requested


@pytest.mark.parametrize("key", ["A", "Q", "E", "f2", "S", "h", "question_mark"])
def test_existing_shortcuts_select_global_entries(mocker, tmp_path, key):
    from jailbee.dashboard.model import RepoGroup
    from jailbee.dashboard.tui import session as tsession
    from tests.dashboard_fixtures import fake_accounts_cli

    group = RepoGroup("alpha", str(tmp_path), None, [])
    fake_accounts_cli(mocker)
    child = mocker.patch.object(tsession.subprocess, "run", return_value=mocker.Mock(returncode=0))
    run = drive(mocker, ["g", key], [group])
    overlay = run.trace[2].overlay
    if key == "A":
        assert isinstance(overlay, tsession.da.AccountsState)
    elif key == "Q":
        assert isinstance(overlay, tsession.Picker) and overlay.purpose == "queue"
    elif key in ("f2", "S"):
        assert isinstance(overlay, tsession.SettingsState)
    elif key in ("h", "question_mark"):
        assert overlay == "help"
    else:
        assert overlay is None
        child.assert_called_once_with(
            ["jailbee", "config", "edit", "--global"], check=False, cwd=tmp_path
        )


def test_restrictive_ssh_menu_hides_disallowed_commands_on_the_pilot(mocker):
    policy = RemoteSSHConfig(commands={"mode": "allowlist", "allow": ["version"]})
    run = drive(mocker, ["g"], [], transport="ssh", remote=True, ssh_policy=policy, screens=True)
    assert [item[0] for item in run.trace[1].overlay.actions] == [
        "Notifications",
        "Settings",
        "Help",
        "Quit",
    ]
    assert "[Q] Notifications" in run.screens[1]
    assert "[F2] Settings" in run.screens[1]
    assert "Stop all" not in run.screens[1]


def test_ssh_menu_omits_terminal_config_editor_even_when_command_policy_allows_it(mocker):
    policy = RemoteSSHConfig(restrict_host=False, commands={"mode": "full"})
    run = drive(mocker, ["g"], [], remote=True, transport="ssh", ssh_policy=policy)
    verbs = [item[1] for item in run.trace[1].overlay.actions]
    assert "global-stop-all" in verbs
    assert "global-resume" not in verbs
    assert "global-config-edit-global" not in verbs


def test_remote_terminal_without_ssh_transport_hides_and_refuses_config_editor(mocker):
    from jailbee.dashboard.tui.session import REMOTE_CONFIG_EDIT_NOTE

    run = drive(mocker, ["g", "escape", "E"], [], remote=True)
    assert "global-config-edit-global" not in [item[1] for item in run.trace[1].overlay.actions]
    assert run.last.notice == REMOTE_CONFIG_EDIT_NOTE


def test_web_global_actions_omit_terminal_only_entries_and_top_level_settings():
    from jailbee.config.models_remote import RemoteWebConfig

    policy = RemoteWebConfig(commands={"mode": "full"})
    assert menus.global_menu_actions(policy, transport="web", frontend="web") == [
        ("Stop all…", "global-stop-all"),
        ("Accounts", "global-accounts"),
        ("Notifications", "global-queue"),
        ("Help", "global-help"),
    ]
    restrictive = RemoteWebConfig(commands={"mode": "allowlist", "allow": ["version"]})
    assert menus.global_menu_actions(restrictive, transport="web", frontend="web") == [
        ("Notifications", "global-queue"),
        ("Help", "global-help"),
    ]


@pytest.mark.parametrize("repos, expected", [((), False), (("secret",), False), (("public",), True)])
@pytest.mark.parametrize("transport", ["ssh", "web"])
def test_resume_is_offered_only_for_an_eligible_saved_repo(repos, expected, transport):
    policy = RemoteSSHConfig(commands={"mode": "full"}, excluded_repos=["secret"])
    actions = menus.global_menu_actions(policy, transport=transport, resumable_repos=repos)
    assert ("Resume…", "global-resume") in actions if expected else ("Resume…", "global-resume") not in actions
    assert ("Stop all…", "global-stop-all") in actions


def test_web_presentation_filters_terminal_entries_without_a_remote_policy():
    verbs = {verb for _, verb in menus.global_menu_actions(None, frontend="web")}
    assert not verbs & {"global-config-edit-global", "global-settings", "global-quit"}


def test_remote_terminal_refuses_repo_config_editor_key(mocker, tmp_path):
    from jailbee.dashboard.model import RepoGroup
    from jailbee.dashboard.tui.session import REMOTE_CONFIG_EDIT_NOTE

    group = RepoGroup("alpha", str(tmp_path), None, [])
    run = drive(mocker, ["j", "e"], [group], remote=True)
    assert run.last.notice == REMOTE_CONFIG_EDIT_NOTE


@pytest.mark.parametrize("repos, expected", [((), False), (("alpha",), True)])
def test_terminal_menu_uses_resumable_snapshot_fact(mocker, repos, expected):
    from tests.dashboard_pilot import FakeStateClient

    client = FakeStateClient([])
    client.resumable_repos = list(repos)
    run = drive(mocker, ["g"], [], client=client)
    verbs = {verb for _, verb in run.trace[1].overlay.actions}
    assert ("global-resume" in verbs) == expected


def test_open_terminal_menu_tracks_resume_set_changes(mocker):
    from tests.dashboard_pilot import FakeStateClient

    client = FakeStateClient([])
    def saved(app):
        client.resumable_repos = ["alpha"]
        client.seq += 1
    def resumed(app):
        client.resumable_repos = []
        client.seq += 1
    run = drive(mocker, ["g", saved, resumed], [], client=client)
    assert "global-resume" not in {v for _, v in run.trace[1].overlay.actions}
    assert "global-resume" in {v for _, v in run.trace[2].overlay.actions}
    assert "global-resume" not in {v for _, v in run.trace[3].overlay.actions}


def test_shared_global_menu_filters_commands_with_the_remote_policy():
    assert menus.global_menu_actions(resumable_repos=["alpha"]) == [
        ("Stop all…", "global-stop-all"),
        ("Resume…", "global-resume"),
        ("Accounts", "global-accounts"),
        ("Notifications", "global-queue"),
        ("Global config", "global-config-edit-global"),
        ("Settings", "global-settings"),
        ("Help", "global-help"),
        ("Quit", "global-quit"),
    ]
    policy = RemoteSSHConfig(commands={"mode": "allowlist", "allow": ["version"]})
    assert menus.global_menu_actions(policy, transport="ssh") == [
        ("Notifications", "global-queue"),
        ("Settings", "global-settings"),
        ("Help", "global-help"),
        ("Quit", "global-quit"),
    ]
