"""Tests for the dashboard's entries for existing CLI verbs (pure; no terminal)."""

from __future__ import annotations

import os
from typing import Any

import pytest

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import actions as dact
from jailbee.dashboard.model import prompt_target_kind
from jailbee.dashboard.overlays import validate_answer
from jailbee.lifecycle import ContainerInfo

PolicyCase = tuple[bool, dict[str, object] | None]


def _policy(kwargs: dict[str, object] | None) -> RemoteSSHConfig | None:
    return None if kwargs is None else RemoteSSHConfig.model_validate(kwargs)


# The cases every entry is checked against: (over_ssh, RemoteSSHConfig kwargs).
_LOCAL: PolicyCase = (False, None)
# A local dashboard never consults the SSH policy, however strict.
_LOCAL_DISABLED: PolicyCase = (False, {"commands": {"mode": "disabled"}})
# `full` commands under restrict_host: true, what `remote.ssh` defaults to.
_SSH_DEFAULT: PolicyCase = (True, {})
_SSH_ALLOW_OTHER: PolicyCase = (True, {"commands": {"mode": "allowlist", "allow": ["shell"]}})
_SSH_EXCLUDED: PolicyCase = (True, {"excluded_repos": ["other"]})
# An SSH dashboard started without a server policy fails closed.
_SSH_NO_POLICY: PolicyCase = (True, None)


def _allow(*leaves: str, restrict_host: bool = True) -> PolicyCase:
    return (
        True,
        {"commands": {"mode": "allowlist", "allow": list(leaves)}, "restrict_host": restrict_host},
    )


def _repo_verbs(case: PolicyCase) -> set[str]:
    over_ssh, kwargs = case
    extras = dact.repo_extras(_policy(kwargs), over_ssh=over_ssh)
    leaves = [extras.apply, *extras.diagnostics, extras.prune]
    return {leaf[1] for leaf in leaves if leaf is not None}


_ALL_REPO = {"apply", "doctor", "disk-usage", "prune"}


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (_LOCAL, _ALL_REPO),
        (_LOCAL_DISABLED, _ALL_REPO),
        # `apply` manages the host: refused under restrict_host in every mode
        (_SSH_DEFAULT, _ALL_REPO - {"apply"}),
        (_allow("apply", "doctor", "disk-usage", "prune"), _ALL_REPO - {"apply"}),
        (_allow("apply", restrict_host=False), {"apply"}),
        (_allow("doctor"), {"doctor"}),
        (_allow("disk-usage"), {"disk-usage"}),
        (_allow("prune"), {"prune"}),
        (_SSH_ALLOW_OTHER, set()),
        # repository exclusions leave only the router's single-repo `safe` set
        (_SSH_EXCLUDED, {"prune"}),
        (_SSH_NO_POLICY, set()),
    ],
    ids=[
        "local",
        "local-ignores-policy",
        "ssh-default",
        "ssh-allowlist-restricted",
        "ssh-allowlist-apply-unrestricted",
        "ssh-allowlist-doctor",
        "ssh-allowlist-disk-usage",
        "ssh-allowlist-prune",
        "ssh-allowlist-without",
        "ssh-excluded-repos",
        "ssh-no-policy",
    ],
)
def test_repo_extras_follow_the_ssh_policy(case, expected):
    assert _repo_verbs(case) == expected


def test_repo_extras_labels_and_submenu_order():
    extras = dact.repo_extras(None, over_ssh=False)
    assert extras.apply == ("Apply config…", "apply")
    assert extras.diagnostics == (("Doctor", "doctor"), ("Disk usage", "disk-usage"))
    assert extras.prune == ("Prune stale containers…", "prune")


def test_repo_argv_never_answers_the_clis_own_questions():
    assert dact.apply_argv(no_restart=False) == ["apply"]
    assert dact.apply_argv(no_restart=True) == ["apply", "--no-restart"]
    assert dact.prune_argv() == ["prune"]  # no --yes-to-all: prune asks per container
    assert dact.doctor_argv() == ["doctor"]
    assert dact.disk_usage_argv() == ["disk-usage"]


def test_apply_picker_is_a_repo_question_with_restart_first():
    picker = dact.apply_picker("alpha")
    assert picker.purpose == "repo-apply"
    assert prompt_target_kind(picker.purpose) == "repo"
    assert picker.target == "alpha"
    assert [e.value for e in picker.entries] == [dact.APPLY_RESTART, dact.APPLY_NO_RESTART]


@pytest.mark.parametrize(
    ("over_ssh", "expected"),
    [
        (False, ["snapshot", "create", "--config", "/r/c.yaml", "--", "alpha-x", "t"]),
        (True, ["snapshot", "create", "--", "alpha-x", "t"]),
    ],
    ids=["local", "ssh"],
)
def test_addressed_puts_config_before_the_separator_and_never_over_ssh(over_ssh, expected):
    argv = ["snapshot", "create", "--", "alpha-x", "t"]
    assert dact.addressed(argv, ["--config", "/r/c.yaml"], over_ssh=over_ssh) == expected
    assert argv == ["snapshot", "create", "--", "alpha-x", "t"]  # not mutated


def test_addressed_appends_config_when_there_is_no_separator():
    assert dact.addressed(["apply"], ["--config", "/c"], over_ssh=False) == [
        "apply",
        "--config",
        "/c",
    ]


def test_command_label_stops_at_the_first_option():
    assert dact.command_label(["snapshot", "create", "--", "a", "b"]) == "snapshot create"
    assert dact.command_label(["apply", "--no-restart"]) == "apply"
    assert dact.command_label(["autostart", "status", "alpha-x"]) == "autostart status alpha-x"


def _container(state: str = "Running", **fields: Any) -> ContainerInfo:
    return ContainerInfo(
        name="alpha-x",
        state=state,
        network="strict",
        ip=None,
        memory_limit=None,
        repo="alpha",
        **fields,
    )


# os.getpid() is alive, so `background.clearable` sees a live worker.
_AUTOSTART_LIVE = {"job_phase": "autostart", "job_pid": os.getpid(), "job_kind": "autostart"}
_AUTOSTART_DONE = {"job_phase": "failed", "job_pid": os.getpid(), "job_kind": "autostart"}

_ALL_CONTAINER = {"autostart-status", "autostart-cancel", "snapshots", "mount-add", "mount-remove"}


def _container_verbs(case: PolicyCase, container: ContainerInfo | None = None) -> set[str]:
    over_ssh, kwargs = case
    info = container or _container(**_AUTOSTART_LIVE, optional_mounts=("gcloud",))
    extras = dact.container_extras(info, ("aws", "gcloud"), _policy(kwargs), over_ssh=over_ssh)
    return {verb for _label, verb in (*extras.after_job, *extras.before_network)}


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (_LOCAL, _ALL_CONTAINER),
        (_LOCAL_DISABLED, _ALL_CONTAINER),
        # `mount` brings a host path into a container: a host command
        (_SSH_DEFAULT, _ALL_CONTAINER - {"mount-add"}),
        (_allow("mount", "unmount"), {"mount-remove"}),
        (_allow("mount", restrict_host=False), {"mount-add"}),
        (_allow("unmount"), {"mount-remove"}),
        (_allow("autostart status"), {"autostart-status"}),
        (_allow("autostart cancel"), {"autostart-cancel"}),
        (_allow("snapshot ls"), {"snapshots"}),
        (_SSH_ALLOW_OTHER, set()),
        (_SSH_EXCLUDED, {"snapshots"}),
        (_SSH_NO_POLICY, set()),
    ],
    ids=[
        "local",
        "local-ignores-policy",
        "ssh-default",
        "ssh-allowlist-mount-restricted",
        "ssh-allowlist-mount-unrestricted",
        "ssh-allowlist-unmount",
        "ssh-allowlist-autostart-status",
        "ssh-allowlist-autostart-cancel",
        "ssh-allowlist-snapshot-ls",
        "ssh-allowlist-without",
        "ssh-excluded-repos",
        "ssh-no-policy",
    ],
)
def test_container_extras_follow_the_ssh_policy(case, expected):
    assert _container_verbs(case) == expected


def test_container_extras_order_and_labels():
    extras = dact.container_extras(
        _container(**_AUTOSTART_LIVE, optional_mounts=("gcloud",)),
        ("aws", "gcloud"),
        None,
        over_ssh=False,
    )
    assert extras.after_job == (
        ("Autostart status", "autostart-status"),
        ("Cancel autostart…", "autostart-cancel"),
    )
    assert extras.before_network == (
        ("Snapshots…", "snapshots"),
        ("Mount…", "mount-add"),
        ("Unmount…", "mount-remove"),
    )


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, set()),
        ({"job_phase": "creating", "job_pid": os.getpid(), "job_kind": "create"}, set()),
        (_AUTOSTART_DONE, {"autostart-status"}),
        (_AUTOSTART_LIVE, {"autostart-status", "autostart-cancel"}),
    ],
    ids=["no-job", "create-job", "finished-autostart", "live-autostart"],
)
def test_autostart_entries_need_an_autostart_row_and_cancel_a_live_worker(fields, expected):
    verbs = _container_verbs(_LOCAL, _container(**fields))
    assert verbs & {"autostart-status", "autostart-cancel"} == expected


@pytest.mark.parametrize(
    ("state", "offered"),
    [("Running", True), ("Stopped", True), ("—", False), ("Frozen", False)],
)
def test_snapshots_and_mounts_need_an_existing_container(state, offered):
    verbs = _container_verbs(_LOCAL, _container(state=state, optional_mounts=("gcloud",)))
    assert bool(verbs & {"snapshots", "mount-add", "mount-remove"}) is offered


def test_mount_choices_offer_only_configured_kinds_in_the_right_direction():
    info = _container(optional_mounts=("gcloud", "stale"))
    assert dact.mount_choices(info, ("aws", "gcloud"), remove=False) == ("aws",)
    # `stale` is attached but no longer configured: the CLI would refuse it
    assert dact.mount_choices(info, ("aws", "gcloud"), remove=True) == ("gcloud",)


@pytest.mark.parametrize(
    ("attached", "kinds", "expected"),
    [
        ((), (), set()),
        ((), ("aws",), {"mount-add"}),
        (("aws",), ("aws",), {"mount-remove"}),
        (("stale",), ("aws",), {"mount-add"}),
    ],
    ids=["no-kinds", "none-attached", "all-attached", "only-a-stale-device"],
)
def test_mount_entries_appear_only_when_there_is_something_to_do(attached, kinds, expected):
    extras = dact.container_extras(
        _container(optional_mounts=attached), kinds, None, over_ssh=False
    )
    verbs = {verb for _label, verb in extras.before_network}
    assert verbs & {"mount-add", "mount-remove"} == expected


def _parsed(argv: list[str]) -> dict[str, object]:
    """``argv`` parsed by its real Click command, as the child will parse it."""
    from jailbee.remote_ssh import router

    typed, command = router.command_leaf(argv)
    words = typed.split()
    with command.make_context(words[-1], argv[len(words) :], resilient_parsing=True) as ctx:
        return dict(ctx.params)


def test_a_tag_or_kind_spelled_like_an_option_stays_a_positional():
    from jailbee.remote_ssh import router

    create = dact.snapshot_create_argv("alpha-x", "--config")
    assert _parsed(create)["tag"] == "--config"
    assert _parsed(create)["config"] is None
    router.check_arguments(create)  # not a host path: the remote policy accepts it
    assert _parsed(dact.snapshot_restore_argv("alpha-x", "-y"))["tag"] == "-y"
    assert _parsed(dact.snapshot_delete_argv("alpha-x", "-c"))["tag"] == "-c"
    mount = _parsed(dact.mount_argv("--yes", "alpha-x"))
    assert (mount["kind"], mount["name"]) == ("--yes", "alpha-x")
    unmount = _parsed(dact.unmount_argv("aws", "alpha-x"))
    assert (unmount["kind"], unmount["name"]) == ("aws", "alpha-x")


def test_container_argv_shapes():
    assert dact.autostart_status_argv("alpha-x") == ["autostart", "status", "alpha-x"]
    assert dact.autostart_cancel_argv("alpha-x") == ["autostart", "cancel", "alpha-x"]
    assert dact.snapshot_ls_argv("alpha-x") == [
        "snapshot",
        "ls",
        "alpha-x",
        "-o",
        "json",
        "--fields",
        "name,created",
    ]
    assert dact.snapshot_create_argv("alpha-x", None) == ["snapshot", "create", "--", "alpha-x"]
    assert dact.snapshot_create_argv("alpha-x", "") == ["snapshot", "create", "--", "alpha-x"]


def test_mount_and_autostart_pickers_are_container_questions():
    add = dact.mount_picker("alpha-x", ("aws",), remove=False)
    remove = dact.mount_picker("alpha-x", ("gcloud",), remove=True)
    cancel = dact.autostart_cancel_picker("alpha-x")
    for picker in (add, remove, cancel):
        assert prompt_target_kind(picker.purpose) == "container"
        assert picker.target == "alpha-x"
    assert (add.purpose, remove.purpose) == ("container-mount-add", "container-mount-remove")
    assert [e.value for e in add.entries] == ["aws"]
    # "No" first: a stray Enter must not cancel the run
    assert [e.value for e in cancel.entries] == ["no", "yes"]


_SNAPS = (
    '[{"name": "before-upgrade", "created": "2026-09-29T10:00:00.5Z"}, '
    '{"name": "b", "created": null}]'
)


def test_parse_snapshot_rows_reads_the_json_listing():
    assert dact.parse_snapshot_rows(_SNAPS) == (
        dact.SnapshotRow("before-upgrade", "2026-09-29T10:00:00.5Z"),
        dact.SnapshotRow("b", None),
    )
    assert dact.parse_snapshot_rows("[]") == ()


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "No snapshots for x",
        "{}",
        "[1]",
        '[{"created": "x"}]',
        '[{"name": 3}]',
        '[{"name": ""}]',
    ],
    ids=["empty", "table-text", "object", "not-a-row", "no-name", "name-not-str", "blank-name"],
)
def test_parse_snapshot_rows_refuses_anything_else(stdout):
    with pytest.raises(dact.SnapshotLoadError, match="unexpected output"):
        dact.parse_snapshot_rows(stdout)


def test_snapshot_picker_puts_the_create_entries_above_the_listing():
    picker = dact.snapshot_picker("alpha-x", dact.parse_snapshot_rows(_SNAPS), can_create=True)
    assert [(e.label, e.value) for e in picker.entries] == [
        ("Create a snapshot (timestamp tag)", dact.CREATE_TIMESTAMP),
        ("Create a snapshot named…", dact.CREATE_NAMED),
        ("before-upgrade  (2026-09-29 10:00)", "snapshot:before-upgrade"),
        ("b", "snapshot:b"),
    ]
    assert dact.snapshot_picker("alpha-x", (), can_create=False).entries == ()


def test_a_snapshot_named_like_a_sentinel_is_still_a_snapshot():
    picker = dact.snapshot_picker(
        "alpha-x", (dact.SnapshotRow("create:named", None),), can_create=True
    )
    listed = picker.entries[-1].value
    assert listed != dact.CREATE_NAMED
    assert dact.snapshot_tag(listed) == "create:named"
    assert dact.snapshot_tag(dact.CREATE_NAMED) is None


def test_snapshot_action_picker_offers_only_permitted_changes():
    both = dact.snapshot_action_picker("alpha-x", "t", can_restore=True, can_delete=True)
    assert [e.value for e in both.entries] == [dact.RESTORE, dact.DELETE]
    assert both.carry == ("t",)
    only_delete = dact.snapshot_action_picker("alpha-x", "t", can_restore=False, can_delete=True)
    assert [e.value for e in only_delete.entries] == [dact.DELETE]


@pytest.mark.parametrize("action", [dact.RESTORE, dact.DELETE])
def test_snapshot_confirm_puts_no_first(action):
    picker = dact.snapshot_confirm_picker("alpha-x", action, "t")
    assert [e.value for e in picker.entries] == ["no", "yes"]
    assert picker.carry == (action, "t")


def test_every_snapshot_question_is_about_the_container():
    rows = dact.parse_snapshot_rows(_SNAPS)
    questions = (
        dact.snapshot_picker("alpha-x", rows, can_create=True),
        dact.snapshot_action_picker("alpha-x", "t", can_restore=True, can_delete=True),
        dact.snapshot_confirm_picker("alpha-x", dact.RESTORE, "t"),
        dact.snapshot_tag_prompt("alpha-x"),
    )
    for question in questions:
        assert prompt_target_kind(question.purpose) == "container"
        assert question.target == "alpha-x"


def test_the_tag_prompt_refuses_an_empty_answer():
    assert validate_answer(dact.snapshot_tag_prompt("alpha-x")) == "Snapshot tag cannot be empty"
