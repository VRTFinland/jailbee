"""Terminal-dashboard entries for existing `jailbee` verbs: argv, gating, questions.

The repo menu gains `apply`, `doctor`, `disk-usage` and `prune`; the container
menu gains the autostart run, snapshots and optional mounts. Everything here
is pure. `jailbee.dashboard.tui.session` wires it into `DashboardSession`, and every command runs as
a real `jailbee` child, so the CLI stays the one place that validates a tag, a
mount kind or a restart. This module only decides which entries a row offers
and which argv each one runs.

Visibility follows the remote-SSH policy exactly. An entry is offered only
when `dashboard.commands.permitted` accepts the argv shape it will run, and
the spawn re-checks the real argv anyway. Locally (`over_ssh` false) the
policy is never consulted.

Must not import `jailbee.dashboard.tui`, which imports this module.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import takewhile
from typing import TYPE_CHECKING

from jailbee.dashboard.commands import insert_options_before_separator, permitted
from jailbee.dashboard.overlays import Picker, PickerEntry, TextPrompt

if TYPE_CHECKING:
    from jailbee.config.models_remote import RemoteSSHConfig
    from jailbee.lifecycle import ContainerInfo

Leaf = tuple[str, str]
"""A menu entry, ``(label, verb)``. The verbs below are handled by `run()` itself."""

REPO_APPLY = "apply"
REPO_DOCTOR = "doctor"
REPO_DISK_USAGE = "disk-usage"
REPO_PRUNE = "prune"

DIAGNOSTICS_LABEL = "Diagnostics →"

APPLY_RESTART = "restart"
APPLY_NO_RESTART = "no-restart"


def apply_argv(*, no_restart: bool) -> list[str]:
    """`jailbee apply`, never with `--yes`: its restart question is the user's.

    That question is also why it runs in the foreground: `apply` asks before
    restarting a container or its dockerd, and only a terminal can answer.
    """
    return ["apply", *(["--no-restart"] if no_restart else [])]


def doctor_argv() -> list[str]:
    return ["doctor"]


def disk_usage_argv() -> list[str]:
    return ["disk-usage"]


def prune_argv() -> list[str]:
    """`jailbee prune` without `--yes-to-all`: its per-container questions confirm it."""
    return ["prune"]


@dataclass(frozen=True)
class RepoExtras:
    """The repo-menu entries this module adds, already filtered by the SSH policy.

    ``diagnostics`` becomes the ``Diagnostics →`` submenu, omitted when empty.
    """

    apply: Leaf | None
    diagnostics: tuple[Leaf, ...]
    prune: Leaf | None


def repo_extras(ssh_policy: RemoteSSHConfig | None, *, over_ssh: bool) -> RepoExtras:
    """What an actionable repo's menu adds; each entry hidden when its command would be refused."""

    def offer(leaf: Leaf, argv: list[str]) -> Leaf | None:
        return leaf if permitted(argv, ssh_policy, over_ssh=over_ssh) else None

    diagnostics = (
        offer(("Doctor", REPO_DOCTOR), doctor_argv()),
        offer(("Disk usage", REPO_DISK_USAGE), disk_usage_argv()),
    )
    return RepoExtras(
        apply=offer(("Apply config…", REPO_APPLY), apply_argv(no_restart=False)),
        diagnostics=tuple(leaf for leaf in diagnostics if leaf is not None),
        prune=offer(("Prune stale containers…", REPO_PRUNE), prune_argv()),
    )


def apply_picker(prefix: str) -> Picker:
    """Whether `apply` may restart what it changes. Esc runs nothing."""
    return Picker(
        "repo-apply",
        f"Apply config — {prefix}",
        (
            PickerEntry("Apply (asks before restarting anything)", APPLY_RESTART),
            PickerEntry("Apply without restarting (--no-restart)", APPLY_NO_RESTART),
        ),
        target=prefix,
    )


def addressed(argv: Sequence[str], flags: Sequence[str], *, over_ssh: bool) -> list[str]:
    """``argv`` pointed at its repo: ``flags`` (``--config``) go before any ``--``.

    Over SSH nothing is added. The child is addressed by its cwd alone, because
    a host config path is exactly the host-reaching argument the remote policy
    refuses (`router.check_arguments`).
    """
    if over_ssh:
        return list(argv)
    return insert_options_before_separator(list(argv), flags)


def command_label(argv: Sequence[str]) -> str:
    """The words of ``argv`` up to its first option or ``--``, for an exit notice."""
    return " ".join(takewhile(lambda word: not word.startswith("-"), argv))


AUTOSTART_STATUS = "autostart-status"
AUTOSTART_CANCEL = "autostart-cancel"
SNAPSHOTS = "snapshots"
MOUNT_ADD = "mount-add"
MOUNT_REMOVE = "mount-remove"
CONTAINER_VERBS = frozenset(
    {AUTOSTART_STATUS, AUTOSTART_CANCEL, SNAPSHOTS, MOUNT_ADD, MOUNT_REMOVE}
)

SNAPSHOT_FIELDS = "name,created"

# States in which a container exists to snapshot or to give a disk device; a
# mid-creation background row ("—") has no instance yet.
_EXISTING_STATES = frozenset({"Running", "Stopped"})


def autostart_status_argv(name: str) -> list[str]:
    return ["autostart", "status", name]


def autostart_cancel_argv(name: str) -> list[str]:
    return ["autostart", "cancel", name]


def snapshot_ls_argv(name: str) -> list[str]:
    return ["snapshot", "ls", name, "-o", "json", "--fields", SNAPSHOT_FIELDS]


def snapshot_create_argv(name: str, tag: str | None) -> list[str]:
    """Create a snapshot. A typed tag follows ``--``, so ``--yes`` is a tag, never an option.

    No tag lets the CLI pick its sortable timestamp tag.
    """
    return ["snapshot", "create", "--", name, *([tag] if tag else [])]


def retarget_argv(name: str, base: str) -> list[str]:
    """Re-point ``name`` at ``base``. Both follow ``--``: a typed name is never an option."""
    return ["git", "retarget", "--", name, base]


def snapshot_restore_argv(name: str, tag: str) -> list[str]:
    return ["snapshot", "restore", "--", name, tag]


def snapshot_delete_argv(name: str, tag: str) -> list[str]:
    return ["snapshot", "delete", "--", name, tag]


def mount_argv(kind: str, name: str) -> list[str]:
    """`jailbee mount KIND NAME`: kind first, as the CLI declares it."""
    return ["mount", "--", kind, name]


def unmount_argv(kind: str, name: str) -> list[str]:
    return ["unmount", "--", kind, name]


def mount_choices(
    container: ContainerInfo, kinds: Sequence[str], *, remove: bool
) -> tuple[str, ...]:
    """The configured kinds Mount… (or, with ``remove``, Unmount…) can act on.

    Only kinds the repo's config still defines. The CLI refuses any other kind
    (`mounts.remove_optional_mount`), so a device left over from a removed
    config entry is not offered.
    """
    attached = set(container.optional_mounts)
    return tuple(kind for kind in kinds if (kind in attached) is remove)


def autostart_worker_live(container: ContainerInfo) -> bool:
    """Whether the container's job row has a worker still running."""
    from jailbee import background

    return (
        container.job_phase is not None
        and container.job_pid is not None
        and not background.clearable(container.job_phase, container.job_pid)
    )


@dataclass(frozen=True)
class ContainerExtras:
    """Terminal-only container entries, already filtered by row state and SSH policy.

    ``after_job`` goes right after the job entries. ``before_network`` goes just
    before ``Credential group…`` and the ``Network →`` group.
    """

    after_job: tuple[Leaf, ...]
    before_network: tuple[Leaf, ...]


def container_extras(
    container: ContainerInfo,
    mount_kinds: Sequence[str],
    ssh_policy: RemoteSSHConfig | None,
    *,
    over_ssh: bool,
) -> ContainerExtras:
    """What one container's menu adds; each entry hidden when its command would be refused.

    ``Snapshots…`` needs `snapshot ls` (the listing *is* its picker). The
    create/restore/delete entries inside are gated one by one when it opens.
    """
    from jailbee import background

    name = container.name

    def allowed(argv: list[str]) -> bool:
        return permitted(argv, ssh_policy, over_ssh=over_ssh)

    after_job: list[Leaf] = []
    if container.job_kind == background.JOB_AUTOSTART and container.job_phase is not None:
        if allowed(autostart_status_argv(name)):
            after_job.append(("Autostart status", AUTOSTART_STATUS))
        if autostart_worker_live(container) and allowed(autostart_cancel_argv(name)):
            after_job.append(("Cancel autostart…", AUTOSTART_CANCEL))
    before: list[Leaf] = []
    if container.state in _EXISTING_STATES:
        if allowed(snapshot_ls_argv(name)):
            before.append(("Snapshots…", SNAPSHOTS))
        addable = mount_choices(container, mount_kinds, remove=False)
        if addable and allowed(mount_argv(addable[0], name)):
            before.append(("Mount…", MOUNT_ADD))
        removable = mount_choices(container, mount_kinds, remove=True)
        if removable and allowed(unmount_argv(removable[0], name)):
            before.append(("Unmount…", MOUNT_REMOVE))
    return ContainerExtras(tuple(after_job), tuple(before))


def mount_picker(container: str, kinds: Sequence[str], *, remove: bool) -> Picker:
    return Picker(
        "container-mount-remove" if remove else "container-mount-add",
        f"Unmount from {container}" if remove else f"Mount into {container}",
        tuple(PickerEntry(kind, kind) for kind in kinds),
        target=container,
    )


def autostart_cancel_picker(container: str) -> Picker:
    """Confirm the cancel. "No" comes first, so a stray Enter stops nothing."""
    return Picker(
        "container-autostart-cancel",
        f"Cancel the autostart run of {container}?",
        (PickerEntry("No", "no"), PickerEntry("Yes, cancel it", "yes")),
        target=container,
    )


@dataclass(frozen=True)
class SnapshotRow:
    """One row of `jailbee snapshot ls -o json`: the tag, and its creation time as printed."""

    name: str
    created: str | None


class SnapshotLoadError(Exception):
    """`jailbee snapshot ls` failed, or printed something that is not a snapshot list."""


def _bad_snapshots() -> SnapshotLoadError:
    return SnapshotLoadError("unexpected output from 'jailbee snapshot ls'")


def parse_snapshot_rows(stdout: str) -> tuple[SnapshotRow, ...]:
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise _bad_snapshots() from exc
    if not isinstance(data, list):
        raise _bad_snapshots()
    rows: list[SnapshotRow] = []
    for item in data:
        if not isinstance(item, dict):
            raise _bad_snapshots()
        name, created = item.get("name"), item.get("created")
        if not isinstance(name, str) or not name:
            raise _bad_snapshots()
        rows.append(SnapshotRow(name, created if isinstance(created, str) and created else None))
    return tuple(rows)


CREATE_TIMESTAMP = "create:timestamp"
CREATE_NAMED = "create:named"
RESTORE = "restore"
DELETE = "delete"

# Listed snapshots carry this prefix in their picker value, so a snapshot that
# happens to be named `create:named` can never be read as the create entry.
_SNAPSHOT_VALUE = "snapshot:"


def snapshot_value(tag: str) -> str:
    return _SNAPSHOT_VALUE + tag


def snapshot_tag(value: str) -> str | None:
    """The tag a picker value names, or None for a create entry."""
    return value.removeprefix(_SNAPSHOT_VALUE) if value.startswith(_SNAPSHOT_VALUE) else None


def _created_label(created: str) -> str:
    """``2026-09-30T08:15:00.123Z`` as ``2026-09-30 08:15``; anything shorter as printed."""
    return created[:16].replace("T", " ") if len(created) >= 16 else created


def snapshot_picker(container: str, rows: Sequence[SnapshotRow], *, can_create: bool) -> Picker:
    """The two create entries (when permitted), then the snapshots in listing order."""
    create = (
        (
            PickerEntry("Create a snapshot (timestamp tag)", CREATE_TIMESTAMP),
            PickerEntry("Create a snapshot named…", CREATE_NAMED),
        )
        if can_create
        else ()
    )
    listed = tuple(
        PickerEntry(
            f"{row.name}  ({_created_label(row.created)})" if row.created else row.name,
            snapshot_value(row.name),
        )
        for row in rows
    )
    return Picker(
        "container-snapshots", f"Snapshots — {container}", (*create, *listed), target=container
    )


def snapshot_action_picker(
    container: str, tag: str, *, can_restore: bool, can_delete: bool
) -> Picker:
    entries: list[PickerEntry] = []
    if can_restore:
        entries.append(PickerEntry("Restore this snapshot…", RESTORE))
    if can_delete:
        entries.append(PickerEntry("Delete this snapshot…", DELETE))
    return Picker(
        "container-snapshot-action",
        f"Snapshot {tag} — {container}",
        tuple(entries),
        target=container,
        carry=(tag,),
    )


def snapshot_confirm_picker(container: str, action: str, tag: str) -> Picker:
    """Confirm a restore or a delete. "No" comes first, so a stray Enter changes nothing."""
    if action == RESTORE:
        question = f"Restore {container} to {tag}? Changes made since then are lost"
        yes = "Yes, restore"
    else:
        question = f"Delete snapshot {tag} of {container}?"
        yes = "Yes, delete"
    return Picker(
        "container-snapshot-confirm",
        question,
        (PickerEntry("No", "no"), PickerEntry(yes, "yes")),
        target=container,
        carry=(action, tag),
    )


def snapshot_tag_prompt(container: str) -> TextPrompt:
    return TextPrompt(
        "container-snapshot-tag", f"New snapshot — {container}", "Snapshot tag", target=container
    )
