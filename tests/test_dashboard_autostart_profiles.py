"""Profile state crosses the gather/snapshot boundary and drives the picker."""

from dataclasses import replace

from jailbee.dashboard.model import RepoGroup
from jailbee.lifecycle import ContainerInfo


def test_ls_profile_column_off_by_default_and_shows_value():
    from jailbee.lifecycle import ls_field_specs

    c = ContainerInfo("alpha-x", "Running", None, None, None, autostart_profile="light")
    from datetime import UTC, datetime

    spec = next(s for s in ls_field_specs(now=datetime.now(UTC)) if s.name == "autostart_profile")
    assert not spec.default_table
    assert spec.header == "PROFILE"
    assert spec.json(c) == "light"
    assert "light" in str(spec.cell(c))


def test_menu_shows_autostart_profile_only_with_profiles(tmp_path):
    from jailbee.dashboard.menus import MenuGroup, group_menu_actions
    from jailbee.dashboard.tui.menu_state import open_menu as menu_for

    c = ContainerInfo("alpha-x", "Running", None, None, None)
    g = RepoGroup("alpha", str(tmp_path), None, [c])
    assert "autostart-profile" not in dict((v, k) for k, v in menu_for([g], "alpha-x").actions)
    g = replace(g, autostart_profiles=("full", "light"), autostart_default_profile="full")
    menu = menu_for([g], "alpha-x")
    assert ("Profile…", "autostart-profile") in menu.actions
    grouped = group_menu_actions(menu.actions, include_network=True, terminal_order=True)
    life = next(i for i in grouped if isinstance(i, MenuGroup) and i.label == "Autostart →")
    assert ("Profile…", "autostart-profile") in life.actions


def test_profile_picker_marks_current_and_runs_switch_command(mocker, tmp_path):
    from jailbee.dashboard.tui import session as ts
    from tests.dashboard_pilot import drive

    c = ContainerInfo("alpha-x", "Running", None, None, None, autostart_profile="light")
    g = RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [c],
        autostart_profiles=("full", "light"),
        autostart_default_profile="full",
    )
    run_cli = mocker.patch.object(ts.DashboardSession, "run_dashboard_command")
    from jailbee.dashboard.menus import MenuGroup
    from jailbee.dashboard.tui import menu_state as tm

    menu = tm.open_menu([g], "alpha-x")
    entries = list(tm.menu_entries(menu))
    at = next(
        i for i, e in enumerate(entries) if isinstance(e, MenuGroup) and e.label == "Autostart →"
    )
    life = entries[at]
    index = next(i for i, e in enumerate(life.actions) if e[1] == "autostart-profile")
    steps = ["j", "enter", *["j"] * at, "enter", *["j"] * index, "enter", "enter"]
    result = drive(mocker, steps, [g])
    assert result.rc == 0
    pickers = result.of_type(ts.Picker)
    assert any("light (current)" in [e.label for e in p.entries] for p in pickers)
    run_cli.assert_called_once_with(
        "alpha-x", "container", ["autostart", "profile", "alpha-x", "full"]
    )


def test_gather_rerunnable_uses_effective_stored_profile(mocker, tmp_path, make_cfg):
    from jailbee.dashboard import model

    cfg = make_cfg(
        tmp_path,
        autostart={
            "on_start": [{"stage": "deps", "steps": []}, {"stage": "agents", "steps": []}],
            "profiles": {"full": "all", "agents-only": {"stages": ["agents"]}},
            "default_profile": "agents-only",
        },
    )
    containers = [
        ContainerInfo("alpha-" + name, "Running", None, None, None, autostart_profile=profile)
        for name, profile in [("full", "full"), ("agents", "agents-only"), ("fallback", "removed")]
    ]
    mocker.patch.object(model, "load_repo_config", return_value=cfg)
    mocker.patch.object(model, "list_containers", side_effect=[containers, []])
    groups = model.gather_rows(mocker.MagicMock(), [tmp_path], with_git=False)
    assert [c.autostart_rerunnable for c in groups[0].containers] == [True, False, False]


def test_autostart_submenu_has_fixed_unique_mnemonics_and_does_not_change_qt(tmp_path):
    from jailbee.dashboard import actions, menus
    from jailbee.dashboard.tui import menu_state

    leaves = [
        ("Re-run…", "autostart-rerun"),
        ("Autostart status", "autostart-status"),
        ("Cancel autostart…", "autostart-cancel"),
        ("Profile…", "autostart-profile"),
    ]
    menu = menu_state.MenuState(
        "alpha-x",
        [
            *leaves,
            ("Launch editor", "ide"),
            ("Snapshots…", "snapshots"),
            ("Restart", "restart"),
            ("Stop", "stop"),
            ("Destroy", "destroy"),
        ],
    )
    root = menu_state.menu_entries(menu)
    assert "u" in menu_state.menu_hotkeys(root)
    group = menu_state.menu_entries(menu, "Autostart →")
    assert group == (
        ("Re-run…", "autostart-rerun"),
        ("Status", "autostart-status"),
        ("Cancel…", "autostart-cancel"),
        ("Profile…", "autostart-profile"),
    )
    assert menu_state.menu_hotkeys(group) == ["r", "T", "C", "p"]
    for entries in (root, group):
        keys = menu_state.menu_hotkeys(entries)
        assert len(keys) == len(set(keys))
        assert all(
            menu_state._preferred_menu_key(e) == k for e, k in zip(entries, keys, strict=True)
        )
    assert menus.group_menu_actions(leaves) == leaves  # Qt does not opt in.
    c = ContainerInfo("alpha-x", "Running", None, None, None)
    extras = actions.container_extras(c, (), None, over_ssh=False)
    empty = menu_state.MenuState("alpha-x", list(extras.after_job))
    assert menu_state.menu_entries(empty) == []


def test_gatherer_carries_profiles(mocker, tmp_path):
    from tests.test_state_gatherer import _Clock, _gatherer

    c = ContainerInfo("alpha-x", "Running", None, None, None, autostart_profile="light")
    g = RepoGroup(
        "alpha",
        str(tmp_path),
        None,
        [c],
        autostart_profiles=("full", "light"),
        autostart_default_profile="full",
    )
    gatherer = _gatherer(mocker, lambda *args, **kwargs: [g], _Clock())
    # The schema roundtrip is checked separately by the state-service suite;
    # the gathered groups must retain the repo choices and stored row value.
    snapshot = gatherer.tick(active=True, refresh=False, roots=[])
    assert snapshot.groups[0].autostart_profiles == ("full", "light")
    assert snapshot.groups[0].containers[0].autostart_profile == "light"
