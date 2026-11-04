"""Profile state crosses the gather/snapshot boundary and drives the picker."""

from dataclasses import replace

import pytest

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard.tui import session as ts
from tests.dashboard_pilot import drive, keys, patch_pause

from jailbee.dashboard.model import RepoGroup
from jailbee.lifecycle import ContainerInfo


def test_gather_repo_without_profiles_includes_builtins(mocker, tmp_path, make_cfg):
    from jailbee.dashboard import model

    cfg = make_cfg(tmp_path, services={"db": {"run": "db"}, "web": {"run": "web"}})
    mocker.patch.object(model, "load_repo_config", return_value=cfg)
    mocker.patch.object(model, "list_containers", return_value=[])
    group = model.gather_rows(mocker.MagicMock(), [tmp_path], with_git=False)[0]
    assert group.autostart_profiles == ("none", "all")
    assert group.autostart_default_profile == "all"
    assert group.autostart_services == ("db", "web")


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
    assert ("Autostart profile…", "autostart-profile") in menu.actions
    grouped = group_menu_actions(menu.actions, include_network=True, terminal_order=True)
    life = next(i for i in grouped if isinstance(i, MenuGroup) and i.label == "Lifecycle →")
    assert ("Autostart profile…", "autostart-profile") in life.actions


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
        i for i, e in enumerate(entries) if isinstance(e, MenuGroup) and e.label == "Lifecycle →"
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


def _choice_group(tmp_path):
    return RepoGroup(
        "alpha",
        str(tmp_path),
        str(tmp_path / "custom.yaml"),
        [ContainerInfo("alpha-x", "Running", None, None, None)],
        autostart_profiles=("none", "all", "light"),
        autostart_default_profile="all",
        autostart_services=("db", "web"),
    )


def _restart_with(app):
    app.session.overlay = ts.MenuState("alpha-x", [])
    app.session.menu_chosen("restart-with", "Lifecycle →", 1)


@pytest.mark.parametrize("from_pr", [False, True])
@pytest.mark.parametrize("over_ssh", [False, True])
@pytest.mark.parametrize("profile", ["all", "none"])
def test_create_always_ends_with_choice_and_preserves_addressing(
    mocker, tmp_path, from_pr, over_ssh, profile
):
    group = _choice_group(tmp_path)
    child = mocker.patch.object(ts.subprocess, "run", return_value=mocker.Mock(returncode=0))
    mocker.patch.object(ts, "new_container_base_default", return_value="main")
    mocker.patch.object(ts, "host_branches", return_value=())
    before = (
        ["enter", "down", "enter", *keys("123"), "enter"]
        if from_pr else ["n", *keys("work"), "enter", "enter"]
    )

    def not_yet(_app):
        child.assert_not_called()

    result = drive(
        mocker, [*before, not_yet, *(["down"] if profile == "none" else []), "enter"],
        [group], over_ssh=over_ssh, ssh_policy=RemoteSSHConfig(),
    )
    picker = next(p for p in result.of_type(ts.Picker) if p.purpose == "new-autostart")
    assert [e.value for e in picker.entries] == ["all", "none", "light", ts.dact.CUSTOM_SERVICES]
    flags = [] if over_ssh else ["--config", str(tmp_path / "custom.yaml")]
    extra = ["--autostart-profile", "none"] if profile == "none" else []
    suffix = ["--pr", "123", *extra] if from_pr else [*extra, "--", "work", "main"]
    child.assert_called_once_with(
        ["jailbee", "new", *flags, "--background", *suffix], check=False, cwd=tmp_path
    )


@pytest.mark.parametrize("over_ssh", [False, True])
@pytest.mark.parametrize("selection", ["db", "empty", "none"])
def test_restart_with_custom_toggle_or_profile_dispatches_final_argv(
    mocker, tmp_path, over_ssh, selection
):
    child = mocker.patch.object(ts.subprocess, "run", return_value=mocker.Mock(returncode=0))
    patch_pause(mocker)
    steps = [_restart_with, "down"]  # all is first; none is second
    if selection == "none":
        steps += ["enter"]
        extra = ["--autostart-profile", "none", "--reset-services"]
    else:
        steps += ["down", "down", "enter"]  # custom, Done first
        if selection == "db":
            steps += ["down", "enter", "up"]  # toggle db; cursor stays on db
        steps += ["enter"]
        extra = ["--services", "db" if selection == "db" else ""]
    result = drive(
        mocker, steps, [_choice_group(tmp_path)], over_ssh=over_ssh, ssh_policy=RemoteSSHConfig()
    )
    pickers = result.of_type(ts.Picker)
    assert any(p.purpose == "restart-autostart" for p in pickers)
    if selection != "none":
        toggles = [p for p in pickers if p.purpose == "autostart-services"]
        assert toggles[0].entries[0].label == "Done"
        assert toggles[0].entries[1].label == "[ ] db"
        if selection == "db":
            assert toggles[-1].entries[1].label == "[x] db"
            assert toggles[-1].start_index == 1
    flags = [] if over_ssh else ["--config", str(tmp_path / "custom.yaml")]
    child.assert_called_once_with(
        ["jailbee", "restart", "alpha-x", "--background", *extra, *flags],
        check=False, cwd=tmp_path,
    )


def test_custom_escape_returns_to_choice_without_execution(mocker, tmp_path):
    child = mocker.patch.object(ts.subprocess, "run")
    result = drive(
        mocker, [_restart_with, "down", "down", "down", "enter", "down", "enter", "escape"],
        [_choice_group(tmp_path)],
    )
    pickers = result.of_type(ts.Picker)
    assert pickers[-1].purpose == "restart-autostart"
    child.assert_not_called()


@pytest.mark.parametrize("state", ["Running", "Stopped"])
@pytest.mark.parametrize("flow", ["branch", "pr", "restart"])
@pytest.mark.parametrize("over_ssh", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_denied_final_autostart_argv_never_executes(mocker, tmp_path, flow, over_ssh, custom, state):
    child = mocker.patch.object(ts.subprocess, "run")
    mocker.patch.object(ts, "new_container_base_default", return_value="main")
    mocker.patch.object(ts, "host_branches", return_value=())
    original = ts.check_dashboard_command
    checked = []

    def deny_selected(argv, policy, *, over_ssh=False):
        checked.append(list(argv))
        if "--autostart-profile" in argv or "--services" in argv:
            raise ts.RouteError("final selection denied")
        return original(argv, policy, over_ssh=over_ssh)

    mocker.patch.object(ts, "check_dashboard_command", side_effect=deny_selected)
    before = {
        "branch": ["n", *keys("work"), "enter", "enter"],
        "pr": ["enter", "down", "enter", *keys("123"), "enter"],
        "restart": [_restart_with],
    }[flow]
    select = ["down", "down", "down", "enter", "down", "enter", "up", "enter"] if custom else ["down", "enter"]
    group = _choice_group(tmp_path)
    group.containers[0] = replace(group.containers[0], state=state)
    result = drive(
        mocker, [*before, *select], [group],
        over_ssh=over_ssh, ssh_policy=RemoteSSHConfig(),
    )
    option = "--services" if custom else "--autostart-profile"
    assert any(option in argv for argv in checked)
    assert any("final selection denied" in str(n) for n in result.notices())
    child.assert_not_called()


def test_restart_with_menu_is_adjacent_and_permission_gated(mocker, tmp_path):
    from jailbee.dashboard.tui import menu_state as tm

    menu = tm.open_menu([_choice_group(tmp_path)], "alpha-x")
    verbs = [v for _, v in menu.actions]
    assert verbs[verbs.index("restart") + 1] == "restart-with"
    original = tm.permitted
    mocker.patch.object(
        tm, "permitted",
        side_effect=lambda argv, *a, **kw: argv[0] != "restart" and original(argv, *a, **kw),
    )
    menu = tm.open_menu([_choice_group(tmp_path)], "alpha-x")
    assert "restart-with" not in [v for _, v in menu.actions]


@pytest.mark.parametrize("flow", ["branch", "pr"])
def test_create_custom_services_and_escape_keep_flow_answers(mocker, tmp_path, flow):
    child = mocker.patch.object(ts.subprocess, "run", return_value=mocker.Mock(returncode=0))
    mocker.patch.object(ts, "new_container_base_default", return_value="main")
    mocker.patch.object(ts, "host_branches", return_value=())
    before = (
        ["enter", "down", "enter", *keys("123"), "enter"]
        if flow == "pr" else ["j", "n", *keys("work"), "enter", "enter"]
    )
    selected = []

    def remember(app):
        selected.append(app.session.selected)

    result = drive(
        mocker,
        [*before, "down", "down", "down", "enter", remember,
         "down", "enter", "enter", "escape", "enter", "down", "enter", "up", "enter"],
        [_choice_group(tmp_path)],
    )
    # Toggle db on then off, Esc to custom, reopen and select db, Done.
    assert any(p.purpose == "autostart-services" for p in result.of_type(ts.Picker))
    if flow == "branch":
        from jailbee.dashboard.model import Row

        assert selected == [Row("container", "alpha-x")]
    flags = ["--config", str(tmp_path / "custom.yaml")]
    suffix = ["--pr", "123", "--services", "db"] if flow == "pr" else [
        "--services", "db", "--", "work", "main"
    ]
    child.assert_called_once_with(
        ["jailbee", "new", *flags, "--background", *suffix], check=False, cwd=tmp_path
    )


def test_restart_with_menu_key_opens_picker_and_builtin_switch_is_offered(mocker, tmp_path):
    group = replace(_choice_group(tmp_path), autostart_profiles=("none", "all"))
    child = mocker.patch.object(ts.subprocess, "run")
    result = drive(mocker, ["j", "enter", "l", "W", "escape", "enter", "l"], [group])
    assert any(p.purpose == "restart-autostart" for p in result.of_type(ts.Picker))
    from jailbee.dashboard.tui.menu_state import open_menu

    assert ("Autostart profile…", "autostart-profile") in open_menu([group], "alpha-x").actions
    child.assert_not_called()


@pytest.mark.parametrize("state", ["Running", "Stopped"])
def test_restart_with_rechecks_open_permission(mocker, tmp_path, state):
    child = mocker.patch.object(ts.subprocess, "run")
    policy = RemoteSSHConfig.model_validate({"commands": {"mode": "allowlist", "allow": ["shell"]}})
    group = _choice_group(tmp_path)
    group.containers[0] = replace(group.containers[0], state=state)
    result = drive(mocker, [_restart_with], [group], over_ssh=True, ssh_policy=policy)
    assert not any(p.purpose == "restart-autostart" for p in result.of_type(ts.Picker))
    child.assert_not_called()


@pytest.mark.parametrize("vanish", ["container", "repo"])
def test_restart_service_picker_resolves_container_not_same_named_repo(mocker, tmp_path, vanish):
    owner = _choice_group(tmp_path)
    collision = RepoGroup("alpha-x", str(tmp_path / "other"), None, [])
    groups = [owner, collision]
    child = mocker.patch.object(ts.subprocess, "run", return_value=mocker.Mock(returncode=0))

    def disappear(_app):
        if vanish == "container":
            owner.containers.clear()
        else:
            groups.remove(collision)

    result = drive(
        mocker, [_restart_with, "down", "down", "down", "enter", disappear, "enter"], groups
    )
    if vanish == "container":
        child.assert_not_called()
        assert any("prompt closed" in str(n) for n in result.notices())
    else:
        child.assert_called_once_with(
            ["jailbee", "restart", "alpha-x", "--background", "--services", "",
             "--config", str(tmp_path / "custom.yaml")], check=False, cwd=tmp_path,
        )


@pytest.mark.parametrize("over_ssh", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_stopped_restart_with_full_menu_to_final_argv(mocker, tmp_path, over_ssh, custom):
    from jailbee.dashboard.tui import menu_state as tm

    group = _choice_group(tmp_path)
    group.containers[0] = replace(group.containers[0], state="Stopped")
    menu = tm.open_menu([group], "alpha-x", over_ssh=over_ssh, ssh_policy=RemoteSSHConfig())
    verbs = [v for _, v in menu.actions]
    assert verbs[verbs.index("start") + 1] == "restart-with"
    assert "restart" not in verbs  # Shared/plain restart remains running-only.
    child = mocker.patch.object(ts.subprocess, "run", return_value=mocker.Mock(returncode=0))
    select = ["down", "down", "down", "enter", "down", "enter", "up", "enter"] if custom else ["down", "enter"]
    result = drive(mocker, ["j", "enter", "l", "W", *select], [group],
                   over_ssh=over_ssh, ssh_policy=RemoteSSHConfig())
    assert any(p.purpose == "restart-autostart" for p in result.of_type(ts.Picker))
    extra = ["--services", "db"] if custom else ["--autostart-profile", "none", "--reset-services"]
    flags = [] if over_ssh else ["--config", str(tmp_path / "custom.yaml")]
    child.assert_called_once_with(
        ["jailbee", "restart", "alpha-x", "--background", *extra, *flags],
        check=False, cwd=tmp_path,
    )


@pytest.mark.parametrize("over_ssh", [False, True])
@pytest.mark.parametrize("change", ["state", "container", "repo"])
def test_restart_with_rechecks_final_availability(mocker, tmp_path, over_ssh, change):
    group = _choice_group(tmp_path)
    group.containers[0] = replace(group.containers[0], state="Stopped")
    child = mocker.patch.object(ts.subprocess, "run")

    def disappear(_app):
        if change == "state":
            group.containers[0] = replace(group.containers[0], state="Frozen")
        elif change == "container":
            group.containers.clear()
        else:
            group.repo_root = None

    drive(mocker, [_restart_with, disappear, "enter"], [group],
          over_ssh=over_ssh, ssh_policy=RemoteSSHConfig())
    child.assert_not_called()


def test_stopped_restart_with_requires_restart_permission_not_start(mocker, tmp_path):
    from jailbee.dashboard.tui import menu_state as tm

    group = _choice_group(tmp_path)
    group.containers[0] = replace(group.containers[0], state="Stopped")
    policy = RemoteSSHConfig.model_validate({"commands": {"mode": "allowlist", "allow": ["restart"]}})
    menu = tm.open_menu([group], "alpha-x", over_ssh=True, ssh_policy=policy)
    assert "start" not in [verb for _, verb in menu.actions]
    assert "restart-with" in [verb for _, verb in menu.actions]
    child = mocker.patch.object(ts.subprocess, "run", return_value=mocker.Mock(returncode=0))
    drive(mocker, ["j", "enter", "l", "W", "enter"], [group], over_ssh=True, ssh_policy=policy)
    child.assert_called_once_with(
        ["jailbee", "restart", "alpha-x", "--background", "--autostart-profile", "all", "--reset-services"],
        check=False, cwd=tmp_path,
    )


@pytest.mark.parametrize("state", ["Running", "Stopped"])
@pytest.mark.parametrize("when", ["open", "final"])
@pytest.mark.parametrize("over_ssh", [False, True])
def test_restart_with_busy_gate(mocker, tmp_path, state, when, over_ssh):
    group = _choice_group(tmp_path)
    group.containers[0] = replace(group.containers[0], state=state)
    child = mocker.patch.object(ts.subprocess, "run")
    busy = mocker.patch.object(ts.DashboardSession, "bulk_running", return_value=frozenset())

    def become_busy(_app):
        busy.return_value = frozenset({"alpha-x"})

    steps = [become_busy, _restart_with] if when == "open" else [_restart_with, become_busy, "enter"]
    result = drive(mocker, steps, [group], over_ssh=over_ssh, ssh_policy=RemoteSSHConfig())
    child.assert_not_called()
    assert any("busy" in str(n) for n in result.notices())
    if when == "open":
        assert not any(p.purpose == "restart-autostart" for p in result.of_type(ts.Picker))


@pytest.mark.parametrize("state", ["Running", "Stopped"])
@pytest.mark.parametrize("flow", ["branch", "pr", "restart"])
def test_same_service_picker_toggle_off_dispatches_empty_selection(mocker, tmp_path, flow, state):
    group = _choice_group(tmp_path)
    group.containers[0] = replace(group.containers[0], state=state)
    child = mocker.patch.object(ts.subprocess, "run", return_value=mocker.Mock(returncode=0))
    mocker.patch.object(ts, "new_container_base_default", return_value="main")
    mocker.patch.object(ts, "host_branches", return_value=())
    before = {
        "branch": ["n", *keys("work"), "enter", "enter"],
        "pr": ["enter", "down", "enter", *keys("123"), "enter"],
        "restart": [_restart_with],
    }[flow]
    observed = []

    def remember(app):
        picker = app.session.overlay
        assert isinstance(picker, ts.Picker)
        observed.append((picker.entries[1].label, picker.start_index))

    drive(mocker, [*before, "down", "down", "down", "enter", remember,
                   "down", "enter", remember, "enter", remember, "up", "enter"], [group])
    assert observed == [("[ ] db", 0), ("[x] db", 1), ("[ ] db", 1)]
    flags = ["--config", str(tmp_path / "custom.yaml")]
    expected = {
        "branch": ["jailbee", "new", *flags, "--background", "--services", "", "--", "work", "main"],
        "pr": ["jailbee", "new", *flags, "--background", "--pr", "123", "--services", ""],
        "restart": ["jailbee", "restart", "alpha-x", "--background", "--services", "", *flags],
    }[flow]
    child.assert_called_once_with(expected, check=False, cwd=tmp_path)


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
