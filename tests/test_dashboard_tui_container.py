"""Container entries on Pilot: autostart, snapshots, outbox and mounts, and their policy guards."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable

import pytest

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import dispatch as ddispatch
from jailbee.dashboard import menus as dmenus
from jailbee.dashboard.tui import keys as tkeys
from jailbee.dashboard.tui import session as tsession
from tests.dashboard_fixtures import (
    autostart_ci,
    cfg_group,
    ci,
    every_verb_group,
    mount_group,
)
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
        tkeys,
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
        tkeys,
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
        tkeys,
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


# --- Outbox: the same picker panels as Snapshots… ------------------------------

_OUTBOX_JSON = json.dumps(
    {
        "schema": 1,
        "containers": [
            {
                "name": "alpha-x",
                "available": True,
                "error": None,
                "stores": [],
                "proposals": [
                    {
                        "id": "pr/a.json",
                        "state": "pending",
                        "revision": "r1",
                        "actions": [{"index": 0}],
                        "error": None,
                        "edit_block": None,
                    }
                ],
            }
        ],
    }
)
_OUTBOX_LS = ["outbox", "ls", "alpha-x", "-o", "json"]
_TO_PROPOSAL = ["enter"]  # the first entry is the only proposal


def _fake_outbox_ls(mocker, result=None):  # type: ignore[no-untyped-def]
    """Patch the quiet runner the outbox listing (and a delete) go through."""
    return mocker.patch.object(
        tsession.da,
        "run_cli_quiet",
        return_value=result or tsession.da.CliResult(True, "done", _OUTBOX_JSON),
    )


@pytest.mark.parametrize("over_ssh", [False, True], ids=["local", "ssh-default"])
def test_outbox_lists_quietly_in_a_picker_instead_of_the_browser(mocker, tmp_path, over_ssh):
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
    picker = run.of_type(tsession.Picker)[0]
    assert picker.title == "Outbox — alpha-x"
    assert [e.value for e in picker.entries] == ["proposal:pr/a.json", "browse"]
    child.assert_not_called()  # listing is quiet: the screen never blanked


def test_outbox_proposal_show_is_paged_in_the_terminal(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker)
    run_cli = mocker.patch.object(tsession, "_run_cli_foreground", return_value=0)

    steps = [*container_menu_keys(group, "outbox browse"), *_TO_PROPOSAL, "enter"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    actions = [p for p in run.of_type(tsession.Picker) if p.purpose == "container-outbox-proposal"]
    assert [e.value for e in actions[0].entries] == ["show", "publish", "delete"]
    run_cli.assert_called_once()
    assert run_cli.call_args.args[1] == ["outbox", "show", "alpha-x", "pr/a.json", "--color"]
    assert run_cli.call_args.kwargs["style"] == "paged"


def test_outbox_publish_leaves_plan_confirmation_to_the_terminal(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    wait = patch_pause(mocker)

    steps = [
        *container_menu_keys(group, "outbox browse"),
        *_TO_PROPOSAL,
        "j",  # Publish…
        "enter",
        "j",  # "Yes, publish"
        "enter",
    ]
    assert drive(mocker, steps, [group]).rc == 0

    child.assert_called_once_with(
        [
            "jailbee",
            "outbox",
            "apply",
            "alpha-x",
            "pr/a.json",
            "--revision",
            "r1",
            "--config",
            str(group.config_path),
        ],
        check=False,
        cwd=tmp_path,
    )
    wait.assert_called_once()


def test_outbox_delete_runs_quietly_after_a_yes(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    quiet = _fake_outbox_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [
        *container_menu_keys(group, "outbox browse"),
        *_TO_PROPOSAL,
        "j",
        "j",  # Delete…
        "enter",
        "j",  # "Yes, delete"
        "enter",
    ]
    assert drive(mocker, steps, [group]).rc == 0

    flags = ["--config", str(group.config_path)]
    assert quiet.call_args_list[-1] == mocker.call(
        ["outbox", "drop", "alpha-x", "pr/a.json", "--yes", "--revision", "r1", *flags],
        cwd=tmp_path,
    )
    child.assert_not_called()


@pytest.mark.parametrize("downs", [1, 2], ids=["publish", "delete"])
def test_outbox_confirm_no_runs_nothing(mocker, tmp_path, downs):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    quiet = _fake_outbox_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [
        *container_menu_keys(group, "outbox browse"),
        *_TO_PROPOSAL,
        *["j"] * downs,
        "enter",
        "enter",  # a stray Enter lands on "No"
    ]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    child.assert_not_called()
    assert quiet.call_count == 1  # only the listing
    assert "Cancelled" in run.notices()


def test_outbox_browse_entry_hands_the_terminal_to_the_full_browser(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0
    wait = patch_pause(mocker)

    steps = [*container_menu_keys(group, "outbox browse"), "j", "enter"]
    assert drive(mocker, steps, [group]).rc == 0

    child.assert_called_once_with(
        ["jailbee", "outbox", "browse", "alpha-x", "--config", str(group.config_path)],
        check=False,
        cwd=tmp_path,
    )
    wait.assert_not_called()  # interactive: nothing left on screen to read


@pytest.mark.parametrize(
    ("result", "notice"),
    [
        (tsession.da.CliResult(False, "error: boom"), "could not list the outbox: error: boom"),
        (
            tsession.da.CliResult(True, "done", "Container  Proposal"),
            "could not list the outbox: unexpected output from 'jailbee outbox ls'",
        ),
        (
            tsession.da.CliResult(
                False,
                "exit 2",
                json.dumps(
                    {"containers": [{"name": "alpha-x", "available": False, "error": "stopped"}]}
                ),
            ),
            "could not read the outbox: stopped",
        ),
        (
            tsession.da.CliResult(True, "done", json.dumps({"containers": []})),
            "Outbox of 'alpha-x' is empty",
        ),
    ],
    ids=["cli-failed", "not-json", "unavailable", "empty"],
)
def test_an_outbox_with_nothing_to_offer_is_a_notice(mocker, tmp_path, result, notice):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    _fake_outbox_ls(mocker, result)

    run = drive(mocker, container_menu_keys(group, "outbox browse"), [group])
    assert run.rc == 0

    assert not run.of_type(tsession.Picker)
    assert notice in run.notices()


def test_outbox_hides_what_the_ssh_allowlist_does_not_permit(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = _allowlist("shell", "outbox browse", "outbox ls")
    _fake_outbox_ls(mocker)
    kwargs = _kwargs(True, policy)

    steps = [*container_menu_keys(group, "outbox browse", **kwargs), *_TO_PROPOSAL]
    run = drive(mocker, steps, [group], **kwargs)
    assert run.rc == 0

    picker = run.of_type(tsession.Picker)[0]
    assert [e.value for e in picker.entries] == ["proposal:pr/a.json", "browse"]
    assert "Nothing can be done to pr/a.json here" in run.notices()


# --- Mount… / Unmount… -------------------------------------------------------


def test_mount_offers_the_unattached_kinds_and_runs_quietly(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(
        tsession.da,
        "run_cli_quiet",
        return_value=tsession.da.CliResult(True, "✓ Mounted 'aws' in container 'x'"),
    )
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, [*container_menu_keys(group, "mount-add"), "enter"], [group])
    assert run.rc == 0

    assert [e.value for e in run.of_type(tsession.Picker)[0].entries] == ["aws"]
    quiet.assert_called_once_with(
        ["mount", "--config", str(group.config_path), "--", "aws", "alpha-x"], cwd=tmp_path
    )
    child.assert_not_called()  # quiet: the screen never blanked
    assert "✓ Mounted 'aws' in container 'x'" in run.notices()


def test_unmount_offers_the_attached_kinds(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(
        tsession.da, "run_cli_quiet", return_value=tsession.da.CliResult(True, "done")
    )

    run = drive(mocker, [*container_menu_keys(group, "mount-remove"), "enter"], [group])
    assert run.rc == 0

    assert [e.value for e in run.of_type(tsession.Picker)[0].entries] == ["gcloud"]
    quiet.assert_called_once_with(
        ["unmount", "--config", str(group.config_path), "--", "gcloud", "alpha-x"], cwd=tmp_path
    )


def test_unmount_over_ssh_offers_the_attached_kinds_without_config(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(
        tsession.da, "run_cli_quiet", return_value=tsession.da.CliResult(True, "done")
    )
    kwargs = _kwargs(True, RemoteSSHConfig())

    steps = [*container_menu_keys(group, "mount-remove", **kwargs), "enter"]
    run = drive(mocker, steps, [group], **kwargs)
    assert run.rc == 0

    assert [e.value for e in run.of_type(tsession.Picker)[0].entries] == ["gcloud"]
    quiet.assert_called_once_with(["unmount", "--", "gcloud", "alpha-x"], cwd=tmp_path)


def test_a_refused_mount_is_a_long_notice(mocker, tmp_path):
    group = mount_group(tmp_path)
    mocker.patch.object(
        tsession.da,
        "run_cli_quiet",
        return_value=tsession.da.CliResult(False, "error: Unknown optional mount 'aws'"),
    )
    child = mocker.patch.object(tsession.subprocess, "run")

    run = drive(mocker, [*container_menu_keys(group, "mount-add"), "enter"], [group])

    # the CLI's own verdict, shown whole; the dashboard is still running (rc 0)
    assert run.rc == 0
    assert "error: Unknown optional mount 'aws'" in run.notices()
    child.assert_not_called()


def test_mount_picker_escape_runs_nothing(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(tsession.da, "run_cli_quiet")

    run = drive(mocker, [*container_menu_keys(group, "mount-add"), "escape"], [group])
    assert run.rc == 0

    assert run.of_type(tsession.Picker)  # it did open
    quiet.assert_not_called()


@pytest.mark.parametrize("verb", ["mount-add", "mount-remove"])
def test_a_kind_spelled_like_an_option_stays_positional(mocker, tmp_path, verb):
    group = mount_group(tmp_path)
    group.optional_mounts = ("--yes", "gcloud")
    quiet = mocker.patch.object(
        tsession.da, "run_cli_quiet", return_value=tsession.da.CliResult(True, "done")
    )
    if verb == "mount-remove":
        group.containers[0] = dataclasses.replace(
            group.containers[0], optional_mounts=("--yes", "gcloud")
        )

    steps = [*container_menu_keys(group, verb), "enter"]  # the first entry: "--yes"
    assert drive(mocker, steps, [group]).rc == 0

    verb_word = "unmount" if verb == "mount-remove" else "mount"
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

    run = drive(mocker, [*container_menu_keys(group, "mount-remove"), "escape"], [group])
    assert run.rc == 0

    assert [e.value for e in run.of_type(tsession.Picker)[0].entries] == ["gcloud"]


def _drive_mount_menu_then(mocker, group, verb, change) -> Run:  # type: ignore[no-untyped-def]
    """Open the container menu on ``verb``, run ``change()``, then press Enter on it.

    The menu overlay keeps the entries it opened with, so this is a stale menu.
    """
    steps = container_menu_keys(group, verb)
    run = drive(mocker, [*steps[:-1], lambda _app: change(), steps[-1]], [group])
    assert run.rc == 0
    return run


def test_mount_with_nothing_left_to_add_notices_instead_of_opening(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(tsession.da, "run_cli_quiet")

    def attach_everything():  # type: ignore[no-untyped-def]
        group.containers[0] = dataclasses.replace(
            group.containers[0], optional_mounts=("aws", "gcloud")
        )

    run = _drive_mount_menu_then(mocker, group, "mount-add", attach_everything)

    assert not run.of_type(tsession.Picker)
    assert "No optional mount to add" in run.notices()
    quiet.assert_not_called()


def test_unmount_with_nothing_attached_notices_instead_of_opening(mocker, tmp_path):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(tsession.da, "run_cli_quiet")

    def detach_everything():  # type: ignore[no-untyped-def]
        group.containers[0] = dataclasses.replace(group.containers[0], optional_mounts=())

    run = _drive_mount_menu_then(mocker, group, "mount-remove", detach_everything)

    assert not run.of_type(tsession.Picker)
    assert "No optional mount to remove" in run.notices()
    quiet.assert_not_called()


@pytest.mark.parametrize("verb", ["mount-add", "mount-remove"])
def test_container_vanishing_on_the_read_that_opens_the_mount_picker_runs_nothing(
    mocker, tmp_path, verb
):
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

    run = _drive_mount_menu_then(mocker, group, verb, vanish)

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
        tkeys,
        tsession,
        side_effect=refuse,
    )

    run = drive(mocker, [*container_menu_keys(group, "mount-add"), "enter"], [group])
    assert run.rc == 0

    assert run.of_type(tsession.Picker)
    quiet.assert_not_called()
    assert "mount is not permitted" in run.notices()


@_VANISH_WHEN
@pytest.mark.parametrize("verb", ["mount-add", "mount-remove"])
def test_container_vanishing_while_the_mount_picker_is_open_runs_nothing(
    mocker, tmp_path, when, verb
):
    group = mount_group(tmp_path)
    quiet = mocker.patch.object(tsession.da, "run_cli_quiet")
    child = mocker.patch.object(tsession.subprocess, "run")

    steps = [*container_menu_keys(group, verb), *_vanish_steps(group, when), "enter"]
    run = drive(mocker, steps, [group])
    assert run.rc == 0

    purpose = "container-mount-remove" if verb == "mount-remove" else "container-mount-add"
    assert {p.purpose for p in run.of_type(tsession.Picker)} == {purpose}
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


@pytest.mark.parametrize("scope,command", [
    ({"kind": "repo"}, ["pr", "alpha-x"]),
    ({"kind": "submodule", "path": "libs/core"}, ["submodule", "pr", "alpha-x", "libs/core"]),
])
def test_outbox_create_pr_launches_scoped_command_after_fresh_read(mocker, tmp_path, scope, command):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    payload = json.loads(_OUTBOX_JSON)
    proposal = payload["containers"][0]["proposals"][0]
    proposal.update(state="awaiting-pr", create_scope=scope)
    quiet = _fake_outbox_ls(mocker, tsession.da.CliResult(True, "done", json.dumps(payload)))
    child = mocker.patch.object(tsession.subprocess, "run", return_value=mocker.Mock(returncode=0))
    patch_pause(mocker)
    run = drive(mocker, [*container_menu_keys(group, "outbox browse"), *_TO_PROPOSAL, "j", "enter"], [group])
    assert run.rc == 0
    assert quiet.call_count == 2
    child.assert_called_once_with(["jailbee", *command, "--config", str(group.config_path)], check=False, cwd=tmp_path)


def test_outbox_create_pr_refuses_changed_revision(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    payload = json.loads(_OUTBOX_JSON)
    proposal = payload["containers"][0]["proposals"][0]
    proposal.update(state="awaiting-pr", create_scope={"kind": "repo"})
    before = json.dumps(payload)
    proposal["revision"] = "r2"
    quiet = _fake_outbox_ls(mocker)
    quiet.side_effect = [tsession.da.CliResult(True, "done", before), tsession.da.CliResult(True, "done", json.dumps(payload))]
    child = mocker.patch.object(tsession.subprocess, "run")
    run = drive(mocker, [*container_menu_keys(group, "outbox browse"), *_TO_PROPOSAL, "j", "enter"], [group])
    child.assert_not_called()
    assert "Proposal changed; refresh the outbox before creating a PR" in run.notices()


def test_outbox_apply_permission_does_not_authorize_create_pr(mocker, tmp_path):
    group = cfg_group(tmp_path, (ci("alpha-x", "alpha"),))
    policy = _allowlist("outbox browse", "outbox ls", "outbox show", "outbox apply")
    payload = json.loads(_OUTBOX_JSON)
    payload["containers"][0]["proposals"][0].update(state="awaiting-pr", create_scope={"kind": "repo"})
    _fake_outbox_ls(mocker, tsession.da.CliResult(True, "done", json.dumps(payload)))
    kwargs = _kwargs(True, policy)
    run = drive(mocker, [*container_menu_keys(group, "outbox browse", **kwargs), *_TO_PROPOSAL], [group], **kwargs)
    actions = [p for p in run.of_type(tsession.Picker) if p.purpose == "container-outbox-proposal"]
    assert [e.value for e in actions[0].entries] == ["show"]
