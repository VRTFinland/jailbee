"""Container entries on Pilot: autostart, snapshots, outbox and mounts, and their policy guards."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable

import pytest

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import dispatch as ddispatch
from jailbee.dashboard import menus as dmenus
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import session as tsession
from tests.dashboard_fixtures import (
    autostart_ci,
    cfg_group,
    ci,
    every_verb_group,
    mount_group,
)
from tests.test_dashboard_outbox import _action as _ob_action
from tests.test_dashboard_outbox import _container as _ob_container
from tests.test_dashboard_outbox import _listing as _ob_listing
from tests.test_dashboard_outbox import _proposal as _ob_proposal
from tests.dashboard_pilot import (
    Run,
    container_menu_keys,
    drive,
    keys,
    patch_in,
    patch_pause,
    repo_menu_keys,
)

pytestmark = pytest.mark.usefixtures("no_real_branch_listing")

_SNAPS_JSON = '[{"name": "before-upgrade", "created": "2026-09-29T10:00:00.5Z"}]'
_SNAPSHOT_LS = ["snapshot", "ls", "alpha-x", "-o", "json", "--fields", "name,created"]
_TO_SNAPSHOT_ROW = ["j", "j", "enter"]
_THREE_SNAPS = json.dumps([{"name": n, "created": "2026-09-29T10:00:00Z"} for n in ("a", "b", "c")])
_CONTAINER_VERB_CASES = sorted(tsession.dact.CONTAINER_VERBS)
_VANISH_WHEN = pytest.mark.parametrize("when", ["frame-before-enter", "same-read-as-enter"])


def _kwargs(over_ssh: bool, policy: RemoteSSHConfig | None) -> dict[str, object]:
    return {"remote": over_ssh, "over_ssh": over_ssh, "ssh_policy": policy}


def _allowlist(*allow: str) -> RemoteSSHConfig:
    return RemoteSSHConfig.model_validate({"commands": {"mode": "allowlist", "allow": list(allow)}})


def _vanish_steps(group, when: str) -> list[Callable[[object], object]]:  # type: ignore[no-untyped-def]
    """The step that makes ``group``'s target vanish just before the Enter that submits.

    ``frame-before-enter``: the listing empties, so the very next tick closes
    the overlay. ``same-read-as-enter``: the harness ticks after every
    mutation, so the target stays listed but loses its repo directory; only
    the submit's own re-resolve can notice (the same production line the old
    "vanish on the Enter's own read" pinned).
    """
    if when == "frame-before-enter":
        return [lambda _app: group.containers.clear()]
    return [lambda _app: setattr(group, "repo_root", None)]


def _gone(run: Run) -> bool:
    return any("is gone" in str(n) for n in run.notices() if n)


# --- Autostart: status and cancel --------------------------------------------


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_autostart_status_runs_in_the_terminal(mocker, tmp_path, over_ssh):
    group = cfg_group(tmp_path, (autostart_ci(),))
    policy = RemoteSSHConfig() if over_ssh else None
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    wait = patch_pause(mocker)
    kwargs = _kwargs(over_ssh, policy)

    steps = container_menu_keys(group, "autostart-status", **kwargs)
    assert drive(mocker, steps, [group], **kwargs).rc == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    child.assert_called_once_with(
        ["jailbee", "autostart", "status", "alpha-x", *flags], check=False, cwd=tmp_path
    )
    wait.assert_called_once()


def test_cancel_autostart_asks_first_and_no_runs_nothing(mocker, tmp_path):
    group = cfg_group(tmp_path, (autostart_ci(),))
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_menu_keys(group, "autostart-cancel"), "enter"]  # Enter on "No"
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    assert [e.value for e in run.of_type(tsession.Picker)[0].entries] == ["no", "yes"]
    child.assert_not_called()
    assert "Cancelled" in run.notices()


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_cancel_autostart_yes_runs_the_cancel(mocker, tmp_path, over_ssh):
    group = cfg_group(tmp_path, (autostart_ci(),))
    policy = RemoteSSHConfig() if over_ssh else None
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    kwargs = _kwargs(over_ssh, policy)

    steps = [*container_menu_keys(group, "autostart-cancel", **kwargs), "j", "enter"]
    assert drive(mocker, steps, [group], **kwargs).rc == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    child.assert_called_once_with(
        ["jailbee", "autostart", "cancel", "alpha-x", *flags], check=False, cwd=tmp_path
    )


# --- Vanish, inert and stale-policy protection of the terminal-only entries --


@_VANISH_WHEN
def test_container_vanishing_while_the_autostart_cancel_picker_is_open_runs_nothing(
    mocker, tmp_path, when
):
    group = cfg_group(tmp_path, (autostart_ci(),))
    child = mocker.patch.object(tsession.subprocess, "run")

    # ``j`` moves the cursor from "No" to "Yes"; the container then disappears
    steps = [*container_menu_keys(group, "autostart-cancel"), "j", *_vanish_steps(group, when)]
    run = drive(mocker, [*steps, "enter"], [group])
    assert run.rc == 0

    child.assert_not_called()
    assert "'alpha-x' is gone" in " ".join(str(n) for n in run.notices())


@_VANISH_WHEN
def test_repo_vanishing_while_the_apply_picker_is_open_runs_nothing(mocker, tmp_path, when):
    group = cfg_group(tmp_path)
    groups = [group]
    child = mocker.patch.object(tsession.subprocess, "run")

    if when == "frame-before-enter":
        vanish = [lambda _app: groups.clear()]  # the next tick closes the picker
    else:
        vanish = _vanish_steps(group, when)  # still listed; only the submit's re-resolve notices
    run = drive(mocker, [*repo_menu_keys(group, "apply"), *vanish, "enter"], groups)
    assert run.rc == 0

    child.assert_not_called()
    assert "'alpha' is gone" in " ".join(str(n) for n in run.notices())


def test_stale_menu_refused_by_the_policy_at_submit_spawns_nothing(mocker, tmp_path):
    group = cfg_group(tmp_path, (autostart_ci(),))
    child = mocker.patch.object(tsession.subprocess, "run")
    steps = container_menu_keys(group, "autostart-status")  # built while the menu offers it
    real_check = tsession.check_dashboard_command

    def refuse(argv, policy, *, over_ssh):
        if argv[:2] == ["autostart", "status"]:
            raise tsession.RouteError("autostart status is not permitted")
        return real_check(argv, policy, over_ssh=over_ssh)

    patch_in(
        mocker,
        "check_dashboard_command",
        ddispatch,
        dmenus,
        tsession,
        side_effect=refuse,
    )

    run = drive(mocker, steps, [group])
    assert run.rc == 0

    child.assert_not_called()
    assert "autostart status is not permitted" in run.notices()


# --- Snapshots…: listing and create -----------------------------------------


def _fake_snapshot_ls(mocker, result=None):  # type: ignore[no-untyped-def]
    """Patch the quiet runner the snapshot listing goes through."""
    return mocker.patch.object(
        tsession.da,
        "run_cli_quiet",
        return_value=result or tsession.da.CliResult(True, "done", _SNAPS_JSON),
    )


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_snapshots_lists_quietly_and_offers_create_above_the_snapshots(mocker, tmp_path, over_ssh):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig() if over_ssh else None
    listing = _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    kwargs = _kwargs(over_ssh, policy)

    run = drive(mocker, container_menu_keys(group, "snapshots", **kwargs), [group], **kwargs)
    assert run.rc == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    listing.assert_called_once_with([*_SNAPSHOT_LS, *flags], cwd=tmp_path)
    picker = run.of_type(tsession.Picker)[0]
    assert [e.value for e in picker.entries] == [
        "create:timestamp",
        "create:named",
        "snapshot:before-upgrade",
    ]
    child.assert_not_called()  # listing is quiet: the screen never blanked


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_snapshot_create_with_a_timestamp_runs_in_the_terminal(mocker, tmp_path, over_ssh):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig() if over_ssh else None
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    wait = patch_pause(mocker)
    kwargs = _kwargs(over_ssh, policy)

    steps = [*container_menu_keys(group, "snapshots", **kwargs), "enter"]
    assert drive(mocker, steps, [group], **kwargs).rc == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    child.assert_called_once_with(
        ["jailbee", "snapshot", "create", *flags, "--", "alpha-x"], check=False, cwd=tmp_path
    )
    wait.assert_called_once()


def test_snapshot_create_named_takes_an_option_like_tag_as_a_tag(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    steps = [*container_menu_keys(group, "snapshots"), "j", "enter", *keys("--yes"), "enter"]
    assert drive(mocker, steps, [group]).rc == 0

    child.assert_called_once_with(
        [
            "jailbee",
            "snapshot",
            "create",
            "--config",
            str(group.config_path),
            "--",
            "alpha-x",
            "--yes",
        ],
        check=False,
        cwd=tmp_path,
    )


def _snapshot_tag_prompts(run: Run) -> list:  # type: ignore[type-arg]
    return [p for p in run.of_type(tsession.TextPrompt) if p.purpose == "container-snapshot-tag"]


def test_snapshot_tag_prompt_escape_runs_nothing(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_menu_keys(group, "snapshots"), "j", "enter", *keys("x"), "escape"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    assert _snapshot_tag_prompts(run)
    assert "Cancelled" in run.notices()
    child.assert_not_called()


def test_snapshot_tag_prompt_ctrl_c_cancels_only_the_prompt(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [
        *container_menu_keys(group, "snapshots"),
        "j",
        "enter",
        *keys("x"),
        "ctrl+c",
        "h",
        "escape",
    ]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    assert _snapshot_tag_prompts(run)
    child.assert_not_called()
    assert "Cancelled" in run.notices()
    # the dashboard survived the Ctrl-C: the later `h` still opened help
    assert "help" in run.overlays()


def test_snapshot_tag_prompt_rejects_a_blank_tag_inline(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_menu_keys(group, "snapshots"), "j", "enter", *keys("  "), "enter"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    child.assert_not_called()
    assert any(n.error == "Snapshot tag cannot be empty" for n in run.prompts())


def test_snapshot_picker_escape_runs_nothing(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, [*container_menu_keys(group, "snapshots"), "escape"], [group])
    assert run.rc == 0

    assert run.of_type(tsession.Picker)
    child.assert_not_called()


@pytest.mark.parametrize(
    ("result", "notice"),
    [
        (tsession.da.CliResult(False, "error: boom"), "could not list snapshots: error: boom"),
        (
            tsession.da.CliResult(True, "done", "No snapshots"),
            "could not list snapshots: unexpected output from 'jailbee snapshot ls'",
        ),
    ],
    ids=["cli-failed", "not-json"],
)
def test_a_failed_snapshot_listing_is_a_notice(mocker, tmp_path, result, notice):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker, result)

    run = drive(mocker, container_menu_keys(group, "snapshots"), [group])
    assert run.rc == 0

    assert not run.of_type(tsession.Picker)
    assert notice in run.notices()


def test_snapshot_listing_is_refused_when_create_is_not_permitted_and_there_are_none(
    mocker, tmp_path
):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = _allowlist("shell", "snapshot ls")
    _fake_snapshot_ls(mocker, tsession.da.CliResult(True, "done", "[]"))
    kwargs = _kwargs(True, policy)

    run = drive(mocker, container_menu_keys(group, "snapshots", **kwargs), [group], **kwargs)
    assert run.rc == 0

    assert not run.of_type(tsession.Picker)
    assert "No snapshots of 'alpha-x'" in run.notices()


def test_snapshot_picker_hides_create_when_only_the_listing_is_permitted(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = _allowlist("shell", "snapshot ls")
    _fake_snapshot_ls(mocker)
    kwargs = _kwargs(True, policy)

    run = drive(mocker, container_menu_keys(group, "snapshots", **kwargs), [group], **kwargs)
    assert run.rc == 0

    picker = run.of_type(tsession.Picker)[0]
    assert [e.value for e in picker.entries] == ["snapshot:before-upgrade"]


def test_snapshot_create_permitted_over_ssh_with_an_empty_listing_still_opens_the_picker(
    mocker, tmp_path
):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = _allowlist("shell", "snapshot ls", "snapshot create")
    _fake_snapshot_ls(mocker, tsession.da.CliResult(True, "done", "[]"))
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    kwargs = _kwargs(True, policy)

    steps = [*container_menu_keys(group, "snapshots", **kwargs), "j", "enter", *keys("--yes")]
    run = drive(mocker, [*steps, "enter"], [group], **kwargs)
    assert run.rc == 0

    picker = run.of_type(tsession.Picker)[0]
    assert [e.value for e in picker.entries] == ["create:timestamp", "create:named"]
    child.assert_called_once_with(
        ["jailbee", "snapshot", "create", "--", "alpha-x", "--yes"], check=False, cwd=tmp_path
    )


def test_snapshot_create_refused_by_the_policy_at_submit_spawns_nothing(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    real_check = tsession.check_dashboard_command

    def refuse(argv, policy, *, over_ssh):
        if argv[:2] == ["snapshot", "create"]:
            raise tsession.RouteError("snapshot create is not permitted")
        return real_check(argv, policy, over_ssh=over_ssh)

    patch_in(
        mocker,
        "check_dashboard_command",
        ddispatch,
        dmenus,
        tsession,
        side_effect=refuse,
    )

    run = drive(mocker, [*container_menu_keys(group, "snapshots"), "enter"], [group])
    assert run.rc == 0

    child.assert_not_called()
    assert "snapshot create is not permitted" in run.notices()


@_VANISH_WHEN
@pytest.mark.parametrize("choice", ["timestamp", "named-tag"])
def test_container_vanishing_while_the_snapshots_picker_or_tag_prompt_is_open_runs_nothing(
    mocker, tmp_path, when, choice
):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    listing = _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = container_menu_keys(group, "snapshots")
    if choice == "named-tag":
        steps = [*steps, "j", "enter", *keys("v1")]
    run = drive(mocker, [*steps, *_vanish_steps(group, when), "enter"], [group])
    assert run.rc == 0

    child.assert_not_called()
    assert listing.call_count == 1  # nothing was listed again either
    assert "'alpha-x' is gone" in " ".join(str(n) for n in run.notices())


# --- Snapshots…: restore and delete, confirmed -------------------------------


@pytest.mark.parametrize(
    ("action_downs", "verb"), [(0, "restore"), (1, "delete")], ids=["restore", "delete"]
)
@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_snapshot_restore_and_delete_run_after_a_yes(
    mocker, tmp_path, action_downs, verb, over_ssh
):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig() if over_ssh else None
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    wait = patch_pause(mocker)
    kwargs = _kwargs(over_ssh, policy)

    steps = [
        *container_menu_keys(group, "snapshots", **kwargs),
        *_TO_SNAPSHOT_ROW,
        *["j"] * action_downs,
        "enter",
        "j",  # "Yes, …"
        "enter",
    ]
    assert drive(mocker, steps, [group], **kwargs).rc == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    child.assert_called_once_with(
        ["jailbee", "snapshot", verb, *flags, "--", "alpha-x", "before-upgrade"],
        check=False,
        cwd=tmp_path,
    )
    wait.assert_called_once()


@pytest.mark.parametrize("action_downs", [0, 1], ids=["restore", "delete"])
def test_snapshot_confirm_no_runs_nothing(mocker, tmp_path, action_downs):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [
        *container_menu_keys(group, "snapshots"),
        *_TO_SNAPSHOT_ROW,
        *["j"] * action_downs,
        "enter",
        "enter",  # a stray Enter lands on "No"
    ]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    confirm = [p for p in run.of_type(tsession.Picker) if p.purpose == "container-snapshot-confirm"]
    assert confirm and confirm[0].entries[0].value == "no"
    child.assert_not_called()
    assert "Cancelled" in run.notices()


@pytest.mark.parametrize("step", ["action", "confirm"])
@pytest.mark.parametrize("key", ["escape", "ctrl+c"], ids=["esc", "ctrl-c"])
def test_snapshot_action_and_confirm_cancel_runs_nothing(mocker, tmp_path, step, key):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_menu_keys(group, "snapshots"), *_TO_SNAPSHOT_ROW]
    if step == "confirm":
        steps += ["j", "enter", "j"]  # Delete, then onto "Yes"
    run = drive(mocker, [*steps, key, "enter"], [group])
    assert run.rc == 0

    purposes = {p.purpose for p in run.of_type(tsession.Picker)}
    assert "container-snapshot-action" in purposes
    assert ("container-snapshot-confirm" in purposes) == (step == "confirm")
    child.assert_not_called()


def test_snapshot_changes_the_policy_refuses_are_not_offered(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = _allowlist("shell", "snapshot ls", "snapshot delete")
    _fake_snapshot_ls(mocker)
    kwargs = _kwargs(True, policy)

    # no create entries: the listed snapshot is row 0
    steps = [*container_menu_keys(group, "snapshots", **kwargs), "enter"]
    run = drive(mocker, steps, [group], **kwargs)
    assert run.rc == 0

    pickers = run.of_type(tsession.Picker)
    assert [e.value for e in pickers[0].entries] == ["snapshot:before-upgrade"]
    actions = [p for p in pickers if p.purpose == "container-snapshot-action"]
    assert [e.value for e in actions[0].entries] == ["delete"]


def test_snapshot_restore_alone_is_offered_when_delete_is_refused(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = _allowlist("shell", "snapshot ls", "snapshot restore")
    _fake_snapshot_ls(mocker)
    kwargs = _kwargs(True, policy)

    steps = [*container_menu_keys(group, "snapshots", **kwargs), "enter"]
    run = drive(mocker, steps, [group], **kwargs)
    assert run.rc == 0

    actions = [p for p in run.of_type(tsession.Picker) if p.purpose == "container-snapshot-action"]
    assert [e.value for e in actions[0].entries] == ["restore"]


def test_a_snapshot_the_policy_permits_no_change_to_is_a_notice(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = _allowlist("shell", "snapshot ls")
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    kwargs = _kwargs(True, policy)

    steps = [*container_menu_keys(group, "snapshots", **kwargs), "enter"]
    run = drive(mocker, steps, [group], **kwargs)
    assert run.rc == 0

    assert "No change to snapshot before-upgrade is permitted here" in run.notices()
    assert not [p for p in run.of_type(tsession.Picker) if p.purpose.endswith("action")]
    child.assert_not_called()


@pytest.mark.parametrize("verb", ["restore", "delete"])
@pytest.mark.parametrize("name", ["--yes", "snapshot:x"])
def test_snapshot_names_that_look_like_options_or_sentinels_stay_positional(
    mocker, tmp_path, verb, name
):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    listing = json.dumps([{"name": name, "created": "2026-09-29T10:00:00Z"}])
    _fake_snapshot_ls(mocker, tsession.da.CliResult(True, "done", listing))
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    steps = [
        *container_menu_keys(group, "snapshots"),
        *_TO_SNAPSHOT_ROW,
        *(["j"] if verb == "delete" else []),
        "enter",
        "j",
        "enter",
    ]
    assert drive(mocker, steps, [group]).rc == 0

    child.assert_called_once_with(
        [
            "jailbee",
            "snapshot",
            verb,
            "--config",
            str(group.config_path),
            "--",
            "alpha-x",
            name,
        ],
        check=False,
        cwd=tmp_path,
    )


@pytest.mark.parametrize("verb", ["restore", "delete"])
def test_snapshot_change_refused_by_the_policy_at_submit_spawns_nothing(mocker, tmp_path, verb):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    real_check = tsession.check_dashboard_command

    def refuse(argv, policy, *, over_ssh):
        if argv[:2] == ["snapshot", verb]:
            raise tsession.RouteError(f"snapshot {verb} is not permitted")
        return real_check(argv, policy, over_ssh=over_ssh)

    patch_in(
        mocker,
        "check_dashboard_command",
        ddispatch,
        dmenus,
        tsession,
        side_effect=refuse,
    )

    steps = [
        *container_menu_keys(group, "snapshots"),
        *_TO_SNAPSHOT_ROW,
        *(["j"] if verb == "delete" else []),
        "enter",
        "j",
        "enter",
    ]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    child.assert_not_called()
    assert f"snapshot {verb} is not permitted" in run.notices()


@_VANISH_WHEN
@pytest.mark.parametrize("step", ["action", "confirm"])
def test_container_vanishing_while_a_snapshot_action_or_confirm_is_open_runs_nothing(
    mocker, tmp_path, when, step
):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_menu_keys(group, "snapshots"), *_TO_SNAPSHOT_ROW]
    if step == "confirm":
        steps += ["enter", "j"]  # Restore, then onto "Yes"
    submit, opens_confirm = ["enter"], step == "confirm"
    if step == "action" and when == "same-read-as-enter":
        # The tick that precedes every key does not close a still-listed
        # container's picker, so Enter does open the confirmation; the
        # refusal is then the confirmed run's re-resolve.
        submit, opens_confirm = ["enter", "j", "enter"], True
    run = drive(mocker, [*steps, *_vanish_steps(group, when), *submit], [group])
    assert run.rc == 0

    purposes = {p.purpose for p in run.of_type(tsession.Picker)}
    assert "container-snapshot-action" in purposes
    assert ("container-snapshot-confirm" in purposes) == opens_confirm
    child.assert_not_called()
    assert "'alpha-x' is gone" in " ".join(str(n) for n in run.notices())


@pytest.mark.parametrize("verb", ["restore", "delete"])
@pytest.mark.parametrize(("row", "tag"), [(0, "a"), (1, "b"), (2, "c")], ids=["a", "b", "c"])
def test_snapshot_change_acts_on_the_chosen_snapshot_not_the_first(
    mocker, tmp_path, verb, row, tag
):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker, tsession.da.CliResult(True, "done", _THREE_SNAPS))
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)

    steps = [
        *container_menu_keys(group, "snapshots"),
        *["j"] * (2 + row),  # past the two create entries
        "enter",
        *(["j"] if verb == "delete" else []),
        "enter",
        "j",
        "enter",
    ]
    assert drive(mocker, steps, [group]).rc == 0

    child.assert_called_once_with(
        ["jailbee", "snapshot", verb, "--config", str(group.config_path), "--", "alpha-x", tag],
        check=False,
        cwd=tmp_path,
    )


@pytest.mark.parametrize("verb", ["restore", "delete"])
def test_snapshot_named_config_stays_positional_over_ssh(mocker, tmp_path, verb):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    listing = json.dumps([{"name": "--config", "created": "2026-09-29T10:00:00Z"}])
    _fake_snapshot_ls(mocker, tsession.da.CliResult(True, "done", listing))
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    kwargs = _kwargs(True, RemoteSSHConfig())

    steps = [
        *container_menu_keys(group, "snapshots", **kwargs),
        *_TO_SNAPSHOT_ROW,
        *(["j"] if verb == "delete" else []),
        "enter",
        "j",
        "enter",
    ]
    assert drive(mocker, steps, [group], **kwargs).rc == 0

    child.assert_called_once_with(
        ["jailbee", "snapshot", verb, "--", "alpha-x", "--config"], check=False, cwd=tmp_path
    )


def test_an_unknown_snapshot_action_spawns_nothing(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_snapshot_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    real = tsession.dact.snapshot_confirm_picker
    mocker.patch.object(
        tsession.dact,
        "snapshot_confirm_picker",
        side_effect=lambda container, _action, tag: real(container, "bogus", tag),
    )

    steps = [*container_menu_keys(group, "snapshots"), *_TO_SNAPSHOT_ROW, "enter", "j", "enter"]
    assert drive(mocker, steps, [group]).rc == 0

    child.assert_not_called()


# --- Outbox: the Outbox box ---------------------------------------------------

_OUTBOX_JSON = _ob_listing(_ob_container(_ob_proposal(actions=[_ob_action(0)])))
_OUTBOX_LS = ["outbox", "ls", "alpha-x", "-o", "json"]
_PLAN_JSON = json.dumps(
    {
        "schema": 1,
        "proposal": "pr/a.json",
        "revision": "r1",
        "actions": [{"index": 0, "kind": "comment", "target": "o/r#42", "will": "publish"}],
        "warnings": [],
        "notes": [],
        "login": "me",
        "total": 1,
        "foreign": False,
        "draftable": False,
        "refusals": [],
    }
)


def _fake_outbox_ls(mocker, result=None):  # type: ignore[no-untyped-def]
    """Patch the quiet runner the outbox listing (and a delete) go through."""
    return mocker.patch.object(
        tsession.da,
        "run_cli_quiet",
        return_value=result or tsession.da.CliResult(True, "done", _OUTBOX_JSON),
    )


def _boxes(run: Run) -> list[tsession.OutboxState]:
    return run.of_type(tsession.OutboxState)


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_outbox_lists_quietly_in_the_outbox_box(mocker, tmp_path, over_ssh):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = RemoteSSHConfig() if over_ssh else None
    listing = _fake_outbox_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    kwargs = _kwargs(over_ssh, policy)

    steps = container_menu_keys(group, "outbox browse", **kwargs)
    run = drive(mocker, steps, [group], **kwargs)
    assert run.rc == 0

    flags = [] if over_ssh else ["--config", str(group.config_path)]
    listing.assert_called_once_with([*_OUTBOX_LS, *flags], cwd=tmp_path)
    box = _boxes(run)[0]
    assert (box.title, box.scope_name, box.prefix) == ("alpha-x", "alpha-x", "alpha")
    assert [p.id for p in box.containers[0].proposals] == ["pr/a.json"]
    assert not run.of_type(tsession.Picker)
    child.assert_not_called()  # listing is quiet: the screen never blanked


def test_outbox_box_esc_goes_back_to_the_container_menu(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker)

    run = drive(mocker, [*container_menu_keys(group, "outbox browse"), "escape"], [group])

    assert _boxes(run)
    assert isinstance(run.last.overlay, tmenu.MenuState)


def test_outbox_delete_runs_quietly_after_a_yes(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    quiet = _fake_outbox_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_menu_keys(group, "outbox browse"), "d", "j", "enter"]  # "Yes, delete"
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    flags = ["--config", str(group.config_path)]
    drops = [c for c in quiet.call_args_list if c.args[0][:2] == ["outbox", "drop"]]
    assert drops == [
        mocker.call(
            ["outbox", "drop", "alpha-x", "pr/a.json", "--yes", "--revision", "r1", *flags],
            cwd=tmp_path,
        )
    ]
    assert quiet.call_args_list[-1].args[0][:2] == ["outbox", "ls"]  # the box re-lists
    assert ("refresh",) in run.client.events
    child.assert_not_called()


def test_outbox_delete_confirm_no_runs_nothing(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    quiet = _fake_outbox_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_menu_keys(group, "outbox browse"), "d", "enter"]  # "No" is the default
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    assert _boxes(run)[-1].question is None
    assert "Nothing deleted." in _boxes(run)[-1].status
    child.assert_not_called()
    assert quiet.call_count == 1  # only the listing


def test_outbox_publish_shows_the_plan_and_publishes_in_the_background(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker)
    child = mocker.patch.object(
        tsession.subprocess,
        "run",
        side_effect=[
            mocker.Mock(returncode=0, stdout=_PLAN_JSON, stderr=""),
            mocker.Mock(returncode=0, stdout="Published 1 action\n", stderr=""),
        ],
    )
    wait = patch_pause(mocker)

    steps = [*container_menu_keys(group, "outbox browse"), "p", "k", "enter"]  # "Publish"
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    asked = [b.question for b in _boxes(run) if b.question is not None]
    assert asked and asked[0].purpose == "publish"
    assert [v for _, v in asked[0].choices] == ["publish", "cancel"]
    flags = ["--config", str(group.config_path)]
    assert [c.args[0] for c in child.call_args_list] == [
        ["jailbee", *tsession.dob.outbox_plan_argv("alpha-x", "pr/a.json", "r1"), *flags],
        ["jailbee", "outbox", "apply", "alpha-x", "pr/a.json", "--revision", "r1", "-y", *flags],
    ]
    assert "Published 1 action" in _boxes(run)[-1].status
    wait.assert_not_called()  # the terminal was never handed over


def test_outbox_create_pr_is_c_in_the_box(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    payload = _ob_listing(
        _ob_container(
            _ob_proposal(
                state="awaiting-pr",
                create_scope={"kind": "repo"},
                actions=[_ob_action(0, kind="description")],
            )
        )
    )
    _fake_outbox_ls(mocker, tsession.da.CliResult(True, "done", payload))
    child = mocker.patch.object(tsession.subprocess, "run", return_value=mocker.Mock(returncode=0))
    patch_pause(mocker)

    assert drive(mocker, [*container_menu_keys(group, "outbox browse"), "c"], [group]).rc == 0

    child.assert_called_once_with(
        ["jailbee", "pr", "alpha-x", "--config", str(group.config_path)], check=False, cwd=tmp_path
    )


@pytest.mark.parametrize(
    ("result", "notice"),
    [
        (tsession.da.CliResult(False, "error: boom"), "could not list the outbox: error: boom"),
        (
            tsession.da.CliResult(True, "done", "Container  Proposal"),
            "could not list the outbox: unexpected output from 'jailbee outbox ls'",
        ),
    ],
    ids=["cli-failed", "not-json"],
)
def test_an_unreadable_outbox_is_a_notice(mocker, tmp_path, result, notice):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker, result)

    run = drive(mocker, container_menu_keys(group, "outbox browse"), [group])
    assert run.rc == 0

    assert not _boxes(run)
    assert notice in run.notices()


def test_an_empty_outbox_still_opens_the_box(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker, tsession.da.CliResult(True, "done", json.dumps({"containers": []})))

    run = drive(mocker, container_menu_keys(group, "outbox browse"), [group])

    assert _boxes(run) and _boxes(run)[0].containers == ()


def test_outbox_offers_only_what_the_ssh_allowlist_permits(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = _allowlist("shell", "outbox browse", "outbox ls")
    quiet = _fake_outbox_ls(mocker)
    kwargs = _kwargs(True, policy)

    steps = [*container_menu_keys(group, "outbox browse", **kwargs), "d", "p", "X"]
    run = drive(mocker, steps, [group], **kwargs)
    assert run.rc == 0

    box = _boxes(run)[-1]
    assert box.abilities == tsession.dob.Abilities(False, False, False, False, False, False)
    assert box.question is None
    hint = tsession.dob.outbox_hint(box, standalone=False)
    assert "d delete" not in hint and "p publish" not in hint
    assert quiet.call_count == 1  # the keys did nothing


def test_outbox_apply_permission_does_not_authorize_create_pr(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = _allowlist("outbox browse", "outbox ls", "outbox apply")
    _fake_outbox_ls(mocker)
    kwargs = _kwargs(True, policy)

    run = drive(mocker, container_menu_keys(group, "outbox browse", **kwargs), [group], **kwargs)

    abilities = _boxes(run)[-1].abilities
    assert not abilities.create_pr
    assert not abilities.publish  # `-y` is refused over SSH: publishing hands off
    assert abilities.publish_hand_off


# --- Mounts… -------------------------------------------------------


def test_mount_offers_the_unattached_kinds_and_runs_quietly(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(
        tsession.da,
        "run_cli_quiet",
        return_value=tsession.da.CliResult(True, "✓ Mounted 'aws' in container 'x'"),
    )
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, [*container_menu_keys(group, "mounts"), "enter"], [group])
    assert run.rc == 0

    # one panel for both directions: aws is free to mount, gcloud is attached
    assert [e.value for e in run.of_type(tsession.Picker)[0].entries] == [
        "mount:aws",
        "unmount:gcloud",
    ]
    quiet.assert_called_once_with(
        ["mount", "--config", str(group.config_path), "--", "aws", "alpha-x"], cwd=tmp_path
    )
    child.assert_not_called()  # quiet: the screen never blanked
    assert "✓ Mounted 'aws' in container 'x'" in run.notices()


@pytest.mark.parametrize("ttl_key, expected", [("enter", "15m"), ("end", "never")])
def test_mount_with_a_ttl_default_asks_and_passes_for(mocker, tmp_path, ttl_key, expected):
    group = mount_group(tmp_path)
    group.mount_ttl_defaults = {"aws": "15m", "gcloud": "15m"}
    quiet = mocker.patch.object(
        tsession.da, "run_cli_quiet", return_value=tsession.da.CliResult(True, "ok")
    )
    keys = [*container_menu_keys(group, "mounts"), "enter"]
    keys += ["enter"] if ttl_key == "enter" else ["end", "enter"]
    run = drive(mocker, keys, [group])
    assert run.rc == 0
    ttl = next(p for p in run.of_type(tsession.Picker) if p.purpose == "container-mount-ttl")
    assert ttl.carry == ("aws",)
    assert ttl.entries[0].value == "15m"
    assert ttl.entries[-1].value == "never"
    quiet.assert_called_once_with(
        [
            "mount",
            "--for",
            expected,
            "--config",
            str(group.config_path),
            "--",
            "aws",
            "alpha-x",
        ],
        cwd=tmp_path,
    )


def test_mount_without_a_ttl_default_runs_immediately(mocker, tmp_path):
    group = mount_group(tmp_path)
    group.mount_ttl_defaults = {"aws": None}
    quiet = mocker.patch.object(
        tsession.da, "run_cli_quiet", return_value=tsession.da.CliResult(True, "ok")
    )
    run = drive(mocker, [*container_menu_keys(group, "mounts"), "enter"], [group])
    assert run.rc == 0
    assert not any(p.purpose == "container-mount-ttl" for p in run.of_type(tsession.Picker))
    quiet.assert_called_once_with(
        ["mount", "--config", str(group.config_path), "--", "aws", "alpha-x"], cwd=tmp_path
    )


def test_mount_ttl_escape_runs_nothing(mocker, tmp_path):
    group = mount_group(tmp_path)
    group.mount_ttl_defaults = {"aws": "15m"}
    quiet = mocker.patch.object(tsession.da, "run_cli_quiet")
    run = drive(mocker, [*container_menu_keys(group, "mounts"), "enter", "escape"], [group])
    assert run.rc == 0
    assert any(p.purpose == "container-mount-ttl" for p in run.of_type(tsession.Picker))
    quiet.assert_not_called()


def test_unmount_offers_the_attached_kinds(mocker, tmp_path):
    group = mount_group(tmp_path)
    group.mount_ttl_defaults = {"aws": "15m", "gcloud": "15m"}
    quiet = mocker.patch.object(
        tsession.da, "run_cli_quiet", return_value=tsession.da.CliResult(True, "done")
    )

    run = drive(mocker, [*container_menu_keys(group, "mounts"), "j", "enter"], [group])
    assert run.rc == 0

    quiet.assert_called_once_with(
        ["unmount", "--config", str(group.config_path), "--", "gcloud", "alpha-x"], cwd=tmp_path
    )


def test_unmount_over_ssh_offers_the_attached_kinds_without_config(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(
        tsession.da, "run_cli_quiet", return_value=tsession.da.CliResult(True, "done")
    )
    kwargs = _kwargs(True, RemoteSSHConfig())

    steps = [*container_menu_keys(group, "mounts", **kwargs), "enter"]
    run = drive(mocker, steps, [group], **kwargs)
    assert run.rc == 0

    # `mount` is a host command the default policy hides; only the unmount is offered
    assert [e.value for e in run.of_type(tsession.Picker)[0].entries] == ["unmount:gcloud"]
    quiet.assert_called_once_with(["unmount", "--", "gcloud", "alpha-x"], cwd=tmp_path)


def test_a_refused_mount_is_a_long_notice(mocker, tmp_path):
    group = mount_group(tmp_path)
    mocker.patch.object(
        tsession.da,
        "run_cli_quiet",
        return_value=tsession.da.CliResult(False, "error: Unknown optional mount 'aws'"),
    )
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, [*container_menu_keys(group, "mounts"), "enter"], [group])

    # the CLI's own verdict, shown whole; the dashboard is still running (rc 0)
    assert run.rc == 0
    assert "error: Unknown optional mount 'aws'" in run.notices()
    child.assert_not_called()


def test_mount_picker_escape_runs_nothing(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(tsession.da, "run_cli_quiet")

    run = drive(mocker, [*container_menu_keys(group, "mounts"), "escape"], [group])
    assert run.rc == 0

    assert run.of_type(tsession.Picker)  # it did open
    quiet.assert_not_called()


@pytest.mark.parametrize("remove", [False, True])
def test_a_kind_spelled_like_an_option_stays_positional(mocker, tmp_path, remove):
    group = mount_group(tmp_path)
    group.optional_mounts = ("--yes", "gcloud")
    quiet = mocker.patch.object(
        tsession.da, "run_cli_quiet", return_value=tsession.da.CliResult(True, "done")
    )
    if remove:
        group.containers[0] = dataclasses.replace(
            group.containers[0], optional_mounts=("--yes", "gcloud")
        )

    steps = [*container_menu_keys(group, "mounts"), "enter"]  # the first entry: "--yes"
    assert drive(mocker, steps, [group]).rc == 0

    verb_word = "unmount" if remove else "mount"
    quiet.assert_called_once_with(
        [verb_word, "--config", str(group.config_path), "--", "--yes", "alpha-x"], cwd=tmp_path
    )


def test_kinds_missing_from_the_config_are_not_offered_to_unmount(mocker, tmp_path):
    group = mount_group(tmp_path)
    group.containers[0] = dataclasses.replace(
        group.containers[0], optional_mounts=("gcloud", "retired")
    )
    mocker.patch.object(
        tsession.da, "run_cli_quiet", return_value=tsession.da.CliResult(True, "done")
    )

    run = drive(mocker, [*container_menu_keys(group, "mounts"), "escape"], [group])
    assert run.rc == 0

    assert [e.value for e in run.of_type(tsession.Picker)[0].entries] == [
        "mount:aws",
        "unmount:gcloud",
    ]


def _drive_mount_menu_then(mocker, group, verb, change) -> Run:  # type: ignore[no-untyped-def]
    """Open the container menu on ``verb``, run ``change()``, then press Enter on it.

    The menu overlay keeps the entries it opened with, so this is a stale menu.
    """
    steps = container_menu_keys(group, verb)
    run = drive(mocker, [*steps[:-1], lambda _app: change(), steps[-1]], [group])
    assert run.rc == 0
    return run


def test_mounts_with_nothing_configured_any_more_notices_instead_of_opening(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(tsession.da, "run_cli_quiet")

    def drop_every_kind():  # type: ignore[no-untyped-def]
        group.optional_mounts = ()

    run = _drive_mount_menu_then(mocker, group, "mounts", drop_every_kind)

    assert not run.of_type(tsession.Picker)
    assert "No optional mount to add or remove" in run.notices()
    quiet.assert_not_called()


def test_container_vanishing_on_the_read_that_opens_the_mount_picker_runs_nothing(mocker, tmp_path):
    """The tick closes a menu whose row left the listing, so the container is
    "gone" only to the Enter's own lookup: that lookup is made to miss.
    """
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(tsession.da, "run_cli_quiet")
    gone = False
    real_find = tsession._find_group
    mocker.patch.object(
        tsession,
        "_find_group",
        side_effect=lambda groups, name: None if gone else real_find(groups, name),
    )

    def vanish() -> None:
        nonlocal gone
        gone = True

    run = _drive_mount_menu_then(mocker, group, "mounts", vanish)

    assert not run.of_type(tsession.Picker)
    quiet.assert_not_called()
    assert "'alpha-x' is gone" in " ".join(str(n) for n in run.notices())


def test_stale_mount_menu_refused_by_the_policy_at_submit_runs_nothing(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(tsession.da, "run_cli_quiet")
    real_check = tsession.check_dashboard_command

    def refuse(argv, policy, *, over_ssh):
        if argv[:1] == ["mount"]:
            raise tsession.RouteError("mount is not permitted")
        return real_check(argv, policy, over_ssh=over_ssh)

    patch_in(
        mocker,
        "check_dashboard_command",
        ddispatch,
        dmenus,
        tsession,
        side_effect=refuse,
    )

    run = drive(mocker, [*container_menu_keys(group, "mounts"), "enter"], [group])
    assert run.rc == 0

    assert run.of_type(tsession.Picker)
    quiet.assert_not_called()
    assert "mount is not permitted" in run.notices()


@_VANISH_WHEN
def test_container_vanishing_while_the_mount_picker_is_open_runs_nothing(mocker, tmp_path, when):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(tsession.da, "run_cli_quiet")
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_menu_keys(group, "mounts"), *_vanish_steps(group, when), "enter"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    assert {p.purpose for p in run.of_type(tsession.Picker)} == {"container-mounts"}
    quiet.assert_not_called()
    child.assert_not_called()
    assert "'alpha-x' is gone" in " ".join(str(n) for n in run.notices())


@pytest.mark.parametrize("verb", _CONTAINER_VERB_CASES)
def test_a_terminal_only_container_entry_never_reaches_the_shared_dispatcher(
    mocker, tmp_path, verb
):
    """Each entry opens an overlay or spawns through the dashboard's own runners.

    It must never fall through to ``_dispatch_action``, which would build the
    invalid ``jailbee <verb> <container>`` command.
    """
    group = every_verb_group(tmp_path)
    dispatch = mocker.patch.object(tsession, "_dispatch_action")
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    patch_pause(mocker)
    # one quiet runner: the snapshot listing needs JSON, the mount a plain success
    mocker.patch.object(
        tsession.da,
        "run_cli_quiet",
        return_value=tsession.da.CliResult(True, "done", _SNAPS_JSON),
    )

    run = drive(mocker, [*container_menu_keys(group, verb), "enter"], [group])
    assert run.rc == 0

    dispatch.assert_not_called()
    opened = run.of_type(tsession.Picker) or run.of_type(tsession.TextPrompt)
    spawned = [call.args[0] for call in child.call_args_list]
    assert opened or spawned
    assert ["jailbee", verb, "alpha-x"] not in spawned
    assert all(argv[:2] != ["jailbee", verb] for argv in spawned)


def test_an_orphan_row_opens_its_own_small_menu(mocker):
    group = dmodel.RepoGroup("gamma", None, None, [ci("gamma-x", "gamma")])

    menu = tmenu.open_menu([group], "gamma-x")

    assert menu is not None
    assert [verb for _label, verb in menu.actions] == [
        "orphan tmux",
        "orphan shell",
        "orphan stop",
        "orphan destroy",
    ]


def test_an_orphan_row_has_no_menu_in_a_remote_session():
    group = dmodel.RepoGroup("gamma", None, None, [ci("gamma-x", "gamma")])

    assert tmenu.open_menu([group], "gamma-x", remote=True, over_ssh=True) is None


def test_an_orphan_action_runs_from_home_with_no_config_flag(mocker):
    group = dmodel.RepoGroup("gamma", None, None, [ci("gamma-x", "gamma")])
    dispatch = mocker.patch.object(tsession, "_dispatch_action", return_value=0)

    drive(mocker, container_menu_keys(group, "orphan tmux"), groups=[group])

    dispatch.assert_called_once()
    target, verb, name = dispatch.call_args.args[:3]
    assert (target, verb, name) == (dmodel.RepoTarget.orphan(), "orphan tmux", "gamma-x")


def test_an_orphan_destroy_is_dispatched_so_the_cli_asks(mocker):
    group = dmodel.RepoGroup("gamma", None, None, [ci("gamma-x", "gamma")])
    dispatch = mocker.patch.object(tsession, "_dispatch_action", return_value=0)

    drive(mocker, container_menu_keys(group, "orphan destroy"), groups=[group])

    assert dispatch.call_args.args[1] == "orphan destroy"


def test_the_dispatcher_runs_an_orphan_verb_as_a_plain_cli_child(mocker):
    run = mocker.patch.object(ddispatch.subprocess, "run")
    run.return_value.returncode = 0
    mocker.patch.object(ddispatch, "_wait_for_return")

    ddispatch._dispatch_action(dmodel.RepoTarget.orphan(), "orphan stop", "gamma-x")

    assert run.call_args.args[0] == ["jailbee", "orphan", "stop", "gamma-x"]
    assert run.call_args.kwargs["cwd"] == dmodel.RepoTarget.orphan().cwd()
