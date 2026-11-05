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
    from jailbee.dashboard.model import RepoAdvisory, RepoGroup
    from jailbee.lifecycle import ContainerInfo
    from jailbee.orchestration.runner_model import Runner

Leaf = tuple[str, str]
"""A menu entry, ``(label, verb)``. The verbs below are handled by `run()` itself."""

REPO_APPLY = "apply"
REPO_DOCTOR = "doctor"
REPO_DISK_USAGE = "disk-usage"
REPO_PRUNE = "prune"
REPO_BASE_BUILD = "base-build"
REPO_DISMISS_ADVISORIES = "dismiss-advisories"
REPO_UPGRADE_ALL = "upgrade-all"
REPO_SENT = "sent"
REPO_REMOVE = "repo-remove"


def repo_remove_argv(prefix: str) -> list[str]:
    return ["repo", "rm", prefix]


DIAGNOSTICS_LABEL = "Diagnostics →"

APPLY_RESTART = "restart"
APPLY_NO_RESTART = "no-restart"


def apply_argv(*, no_restart: bool) -> list[str]:
    """`jailbee apply`, never with `--yes`: its restart question is the user's.

    That question is also why it runs in the foreground: `apply` asks before
    restarting a container or its dockerd, and only a terminal can answer.
    """
    return ["apply", *(["--no-restart"] if no_restart else [])]


def base_build_argv() -> list[str]:
    """`jailbee base build`: long-running, so it runs in the foreground terminal."""
    return ["base", "build"]


def upgrade_all_argv() -> list[str]:
    """`jailbee upgrade`: every repo's owed base build and apply."""
    return ["upgrade"]


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


def upgrade_all_picker(prefix: str, builds: int, applies: int) -> Picker:
    """Confirm `jb upgrade`, which can build images in many repos. Esc runs nothing."""
    return Picker(
        "repo-upgrade-all",
        "Upgrade all repos",
        (PickerEntry("Run jb upgrade", "yes"), PickerEntry("Cancel", "no")),
        target=prefix,
        detail=(
            f"base build in {builds} repo(s), apply in {applies} repo(s)",
            "Image builds can take a long time. Nothing is restarted.",
        ),
    )


def dismiss_picker(prefix: str, advisories: Sequence[RepoAdvisory]) -> Picker:
    """Which of a repo's notices to dismiss; the entry value is the advisory key."""
    return Picker(
        "repo-dismiss-advisory",
        f"Dismiss notice — {prefix}",
        tuple(PickerEntry(a.summary, a.key) for a in advisories),
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


AUTOSTART_RERUN = "autostart-rerun"


CUSTOM_SERVICES = "\x00custom"


def autostart_choice_entries(group: RepoGroup) -> tuple[PickerEntry, ...]:
    default = group.autostart_default_profile or "all"
    names = (default, *(name for name in group.autostart_profiles if name != default))
    return (
        *(PickerEntry(f"{name}  (default)" if name == default else name, name) for name in names),
        *((PickerEntry("Custom services…", CUSTOM_SERVICES),) if group.autostart_services else ()),
    )


def autostart_choice_args(
    group: RepoGroup, *, profile: str | None, services: Sequence[str] | None, creating: bool
) -> list[str]:
    args: list[str] = []
    if profile is not None and not (
        creating and profile == (group.autostart_default_profile or "all") and services is None
    ):
        args.extend(("--autostart-profile", profile))
        if not creating and services is None:
            args.append("--reset-services")
    if services is not None:
        args.extend(("--services", ",".join(services)))
    return args


AUTOSTART_STATUS = "autostart-status"
AUTOSTART_CANCEL = "autostart-cancel"
SNAPSHOTS = "snapshots"
MOUNTS = "mounts"
SERVICES = "services"
ORCHESTRATION = "orchestration"
CONTAINER_VERBS = frozenset(
    {
        AUTOSTART_RERUN,
        AUTOSTART_STATUS,
        AUTOSTART_CANCEL,
        SNAPSHOTS,
        MOUNTS,
        SERVICES,
        ORCHESTRATION,
    }
)

SNAPSHOT_FIELDS = "name,created"

# States in which a container exists to snapshot or to give a disk device; a
# mid-creation background row ("—") has no instance yet.
_EXISTING_STATES = frozenset({"Running", "Stopped"})


def service_ls_argv(name: str) -> list[str]:
    return ["service", "ls", name, "--json"]


def service_op_argv(op: str, name: str, service: str) -> list[str]:
    return ["service", op, name, service]


def service_logs_argv(name: str, service: str) -> list[str]:
    return ["service", "logs", name, service, "-n", "200"]


@dataclass(frozen=True)
class ServiceRow:
    name: str
    state: str


def parse_service_rows(stdout: str) -> tuple[ServiceRow, ...]:
    try:
        data = json.loads(stdout)
        if not isinstance(data, list):
            raise ValueError
        rows = []
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                raise ValueError
            name, state = item.get("name"), item.get("state")
            if (
                not isinstance(name, str)
                or not name
                or not name.isprintable()
                or name in seen
                or state not in ("ready", "starting", "stopped", "failed", "exited", "unhealthy")
                or not all(
                    k in item and (item[k] is None or isinstance(item[k], str))
                    for k in ("window", "since")
                )
                or not isinstance(item.get("detail"), str)
            ):
                raise ValueError
            seen.add(name)
            rows.append(ServiceRow(name, state))
        return tuple(rows)
    except (ValueError, TypeError) as exc:
        raise ValueError("unexpected output from 'jailbee service ls'") from exc


def services_picker(container: str, rows: Sequence[ServiceRow]) -> Picker:
    return Picker(
        "container-services",
        f"Services — {container}",
        tuple(PickerEntry(f"{r.name}  ({r.state})", str(i)) for i, r in enumerate(rows)),
        target=container,
        carry=tuple(json.dumps([r.name, r.state]) for r in rows),
    )


def service_action_picker(container: str, service: str, state: str) -> Picker:
    ops: tuple[str, ...] = (
        ("stop", "restart") if state in ("ready", "starting", "unhealthy") else ("start",)
    )
    ops += ("refresh", "logs")
    return Picker(
        "container-service-action",
        f"Service {service} — {container}",
        tuple(PickerEntry(op.title() + ("…" if op == "refresh" else ""), op) for op in ops),
        target=container,
        carry=(service,),
    )


def autostart_list_argv(name: str) -> list[str]:
    return ["autostart", "run", name, "--list", "--json"]


def autostart_rerun_argv(
    name: str,
    *,
    stages: Sequence[str] = (),
    steps: Sequence[str] = (),
    failed: bool = False,
    trigger: str | None = None,
) -> list[str]:
    argv = ["autostart", "run", name]
    for stage in stages:
        argv += ["--stage", stage]
    for step in steps:
        argv += ["--step", step]
    if failed:
        argv.append("--failed")
    if trigger is not None:
        argv += ["--trigger", trigger]
    return argv


@dataclass(frozen=True)
class RerunStep:
    step: str
    state: str | None
    at: str | None


@dataclass(frozen=True)
class RerunRow:
    trigger: str
    stage: str
    state: str | None
    at: str | None
    steps: tuple[RerunStep, ...]


class RerunLoadError(ValueError):
    """The public autostart listing could not be loaded."""


def parse_rerun_rows(stdout: str) -> tuple[RerunRow, ...]:
    """Validate the public stage/steps JSON, including identities and outcome types."""
    from datetime import datetime

    def bad() -> RerunLoadError:
        return RerunLoadError("unexpected output from 'jailbee autostart run --list'")

    def name(value: object) -> str:
        if not isinstance(value, str) or not value or not value.isprintable():
            raise bad()
        return value

    def outcome(value: dict[str, object]) -> tuple[str | None, str | None]:
        if "state" not in value or "at" not in value:
            raise bad()
        state, at = value["state"], value["at"]
        if state is None and at is None:
            return None, None
        if not isinstance(state, str) or state not in ("start", "ok", "fail"):
            raise bad()
        if not isinstance(at, str):
            raise bad()
        try:
            datetime.fromisoformat(at)
        except ValueError as exc:
            raise bad() from exc
        return state, at

    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise bad() from exc
    if not isinstance(data, list):
        raise bad()
    rows: list[RerunRow] = []
    seen: set[tuple[str, str]] = set()
    for row in data:
        if not isinstance(row, dict):
            raise bad()
        trigger, stage = name(row.get("trigger")), name(row.get("stage"))
        if trigger not in ("on_create", "on_start") or (trigger, stage) in seen:
            raise bad()
        seen.add((trigger, stage))
        state, at = outcome(row)
        children = row.get("steps")
        if not isinstance(children, list):
            raise bad()
        steps: list[RerunStep] = []
        step_names: set[str] = set()
        for child in children:
            if not isinstance(child, dict):
                raise bad()
            step = name(child.get("step"))
            if step in step_names:
                raise bad()
            step_names.add(step)
            child_state, child_at = outcome(child)
            steps.append(RerunStep(step, child_state, child_at))
        rows.append(RerunRow(trigger, stage, state, at, tuple(steps)))
    return tuple(rows)


def rerun_picker(container: str, rows: Sequence[RerunRow]) -> Picker:
    """Failures lead, with whole-stage rows kept next to their children.

    Capture each argv beside its opaque value; stage names may contain colons.
    """
    from datetime import datetime

    symbols = {None: "·", "ok": "✓", "fail": "✗", "start": "…"}
    failed_states = {"fail", "start"}
    entries: list[PickerEntry] = []
    commands: list[str] = []

    def add(label: str, value: str, argv: list[str]) -> None:
        entries.append(PickerEntry(label, value))
        commands.append(json.dumps(argv))

    failures = sum(step.state in failed_states for row in rows for step in row.steps)
    if failures:
        add(
            f"Re-run failed steps ({failures})",
            "failed",
            autostart_rerun_argv(container, failed=True),
        )
    for row in sorted(rows, key=lambda r: not any(s.state in failed_states for s in r.steps)):
        ambiguous = len({r.trigger for r in rows if r.stage == row.stage}) > 1
        trigger = row.trigger if ambiguous else None
        label = row.stage + (f"  [{row.trigger}]" if ambiguous else "")
        label += f"  {symbols[row.state]}"
        if row.at is not None:
            label += " " + datetime.fromisoformat(row.at).strftime("%H:%M")
        add(
            label,
            f"stage:{row.trigger}:{row.stage}",
            autostart_rerun_argv(container, stages=(row.stage,), trigger=trigger),
        )
        for step in sorted(row.steps, key=lambda s: s.state not in failed_states):
            add(
                f"  {step.step}  {symbols[step.state]}",
                f"step:{row.trigger}:{row.stage}/{step.step}",
                autostart_rerun_argv(
                    container, steps=(f"{row.stage}/{step.step}",), trigger=trigger
                ),
            )
    return Picker(
        "container-autostart-rerun",
        f"Re-run autostart — {container}",
        tuple(entries),
        target=container,
        carry=tuple(commands),
    )


def rerun_confirm_picker(container: str, label: str, argv: Sequence[str]) -> Picker:
    return Picker(
        "container-autostart-rerun-confirm",
        f"Re-run {label.strip().removeprefix('Re-run ')} — {container}?",
        (PickerEntry("No", "no"), PickerEntry("Yes, re-run", "yes")),
        target=container,
        carry=tuple(argv),
    )


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


def mount_argv(kind: str, name: str, ttl: str | None = None) -> list[str]:
    """`jailbee mount [--for TTL] -- KIND NAME`: options before the separator."""
    return ["mount", *(("--for", ttl) if ttl is not None else ()), "--", kind, name]


def unmount_argv(kind: str, name: str) -> list[str]:
    return ["unmount", "--", kind, name]


def orchestrate_grant_argv(
    name: str,
    *,
    profile: str | None = None,
    children: int | None = None,
    allow: Sequence[str] = (),
    deny: Sequence[str] = (),
    mounts: Sequence[str] | None = None,
    loose: bool | None = None,
    egress: Sequence[str] | None = None,
    runner: Runner | None = None,
    clear_runner: bool = False,
    publish_issues: Sequence[str] | None = None,
    publish_foreign: tuple[Sequence[str], Sequence[str]] | None = None,
    publish_prs: bool | None = None,
    publish_pr_reviews: bool | None = None,
    publish_rate: int | None = None,
) -> list[str]:
    """`jailbee orchestrate grant`: options before the separator, in signature order.

    ``None`` leaves a field as it is. An empty ``mounts`` / ``egress`` clears
    the list (``--clear-mounts`` / ``--clear-egress``); an empty
    ``publish_issues``, or ``publish_foreign`` with no repositories, clears
    those rights (``none``). ``publish_foreign`` is ``(repos, kinds)``.
    """
    options: list[str] = []
    if profile is not None:
        options += ["--profile", profile]
    if children is not None:
        options += ["--children", str(children)]
    for op in allow:
        options += ["--allow", op]
    for op in deny:
        options += ["--deny", op]
    if mounts is not None:
        options += [item for m in mounts for item in ("--mount", m)] or ["--clear-mounts"]
    if loose is not None:
        options.append("--loose" if loose else "--no-loose")
    if egress is not None:
        options += [item for h in egress for item in ("--egress", h)] or ["--clear-egress"]
    if runner is not None:
        options += ["--runner-agent", runner.agent]
        if runner.profile is not None:
            options += ["--runner-profile", runner.profile]
        if runner.model is not None:
            options += ["--runner-model", runner.model]
    if clear_runner:
        options.append("--clear-runner")
    if publish_issues is not None:
        options += ["--publish-issues", ",".join(publish_issues) or "none"]
    if publish_foreign is not None:
        repos, kinds = publish_foreign
        entries = [f"{repo}:{','.join(kinds)}" for repo in repos] or ["none"]
        options += [item for e in entries for item in ("--publish-foreign-issues", e)]
    if publish_prs is not None:
        options.append("--publish-prs" if publish_prs else "--no-publish-prs")
    if publish_pr_reviews is not None:
        options.append("--publish-pr-reviews" if publish_pr_reviews else "--no-publish-pr-reviews")
    if publish_rate is not None:
        options += ["--publish-rate", str(publish_rate)]
    return ["orchestrate", "grant", *options, "--", name]


def parse_foreign_issue_answer(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``owner/name[, owner/name]: kind[, kind]`` as ``(repos, kinds)``; blank is ``((), ())``.

    Repositories are casefolded and both lists sorted, as the grant stores
    them. Raises ``ValueError`` with a readable reason for a malformed answer.
    """
    from pydantic import ValidationError

    from jailbee.orchestration.grant import ISSUE_ACTION_KINDS, ForeignIssueRights

    answer = text.strip()
    if not answer:
        return (), ()
    repos_part, sep, kinds_part = answer.partition(":")
    if not sep:
        raise ValueError("Write the repositories, a colon, then the kinds: owner/name: comment")
    repos = tuple(sorted({r.casefold() for r in repos_part.replace(",", " ").split()}))
    kinds = tuple(sorted(set(kinds_part.replace(",", " ").split())))
    if not repos:
        raise ValueError("Name at least one owner/name repository, or *")
    if not kinds:
        raise ValueError("Name at least one issue action kind")
    unknown = [k for k in kinds if k not in ISSUE_ACTION_KINDS]
    if unknown:
        raise ValueError(
            f"Unknown issue action {unknown[0]!r}; known: {', '.join(ISSUE_ACTION_KINDS)}"
        )
    try:
        ForeignIssueRights.model_validate({"repos": repos, "actions": kinds})
    except ValidationError as e:
        raise ValueError(str(e.errors()[0]["msg"]).removeprefix("Value error, ")) from e
    return repos, kinds


def orchestrate_profile_save_argv(name: str, profile: str, *, force: bool = False) -> list[str]:
    return ["orchestrate", "profile", "save", *(["--force"] if force else []), "--", name, profile]


def orchestrate_revoke_argv(name: str) -> list[str]:
    return ["orchestrate", "revoke", "--", name]


def orchestrate_adopt_argv(orchestrator: str, child: str) -> list[str]:
    return ["orchestrate", "adopt", "--", orchestrator, child]


def orchestrate_release_argv(child: str) -> list[str]:
    return ["orchestrate", "release", "--", child]


def adoptable_rows(rows: Sequence[ContainerInfo], orchestrator: str) -> list[ContainerInfo]:
    parents = {row.child.orchestrator for row in rows if row.child is not None}
    return [
        row
        for row in rows
        if row.name != orchestrator
        and row.grant is None
        and row.child is None
        and row.mode != "mount"
        and row.name not in parents
    ]


def orchestration_grant_permitted(
    container: ContainerInfo, ssh_policy: RemoteSSHConfig | None, *, over_ssh: bool
) -> bool:
    """Whether ``orchestrate grant`` (every edit but revoke) may run for this container."""
    return permitted(orchestrate_grant_argv(container.name), ssh_policy, over_ssh=over_ssh)


def orchestration_offered(
    container: ContainerInfo,
    ssh_policy: RemoteSSHConfig | None,
    *,
    over_ssh: bool,
    rows: Sequence[ContainerInfo] = (),
) -> bool:
    """Offer orchestration when a permitted action applies to this container."""
    if not (
        container.state in _EXISTING_STATES
        and container.child is None
        and container.mode != "mount"
    ):
        return False
    if orchestration_grant_permitted(container, ssh_policy, over_ssh=over_ssh):
        return True
    if container.grant is None:
        return False
    if permitted(orchestrate_revoke_argv(container.name), ssh_policy, over_ssh=over_ssh):
        return True
    return any(
        permitted(orchestrate_adopt_argv(container.name, row.name), ssh_policy, over_ssh=over_ssh)
        for row in adoptable_rows(rows, container.name)
    ) or any(
        row.child is not None
        and row.child.orchestrator == container.name
        and permitted(orchestrate_release_argv(row.name), ssh_policy, over_ssh=over_ssh)
        for row in rows
    )


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
    remote_mountable: Sequence[str] = (),
    rows: Sequence[ContainerInfo] = (),
    services: Sequence[str] = (),
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
    if (
        container.state == "Running"
        and container.autostart_rerunnable
        and not container.job_live
        and allowed(autostart_rerun_argv(name, failed=True))
        and allowed(autostart_list_argv(name))
    ):
        after_job.append(("Re-run…", AUTOSTART_RERUN))
    if container.job_kind == background.JOB_AUTOSTART and container.job_phase is not None:
        if allowed(autostart_status_argv(name)):
            after_job.append(("Autostart status", AUTOSTART_STATUS))
        if autostart_worker_live(container) and allowed(autostart_cancel_argv(name)):
            after_job.append(("Cancel autostart…", AUTOSTART_CANCEL))
    before: list[Leaf] = []
    if container.state in _EXISTING_STATES:
        if allowed(snapshot_ls_argv(name)):
            before.append(("Snapshots…", SNAPSHOTS))
        if mount_entries(
            container, mount_kinds, ssh_policy, over_ssh=over_ssh, remote_mountable=remote_mountable
        ):
            before.append(("Mounts…", MOUNTS))
    if services and container.state == "Running" and allowed(service_ls_argv(name)):
        before.append(("Services…", SERVICES))
    if orchestration_offered(container, ssh_policy, over_ssh=over_ssh, rows=rows):
        before.append(("Orchestration…", ORCHESTRATION))
    return ContainerExtras(tuple(after_job), tuple(before))


_MOUNT_VALUE = "mount:"
_UNMOUNT_VALUE = "unmount:"


def mount_directions(
    container: ContainerInfo,
    kinds: Sequence[str],
    ssh_policy: RemoteSSHConfig | None,
    *,
    over_ssh: bool,
    remote_mountable: Sequence[str] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The configured kinds ``container`` may mount, and those it may unmount.

    Mounting ignores whether the kind is attached already: `jailbee mount` on
    an attached kind sets a new TTL. Unmounting needs it attached. Each
    direction is gated on its own argv, so an SSH allowlist that permits
    `unmount` and not `mount` leaves only the attached kinds. In a restricted
    SSH session a kind is mountable only when `remote_mountable` names it, the
    same rule `jailbee mount` applies there.
    """
    from jailbee.remote_ssh.session import is_remote_session

    can_add = permitted(mount_argv("kind", container.name), ssh_policy, over_ssh=over_ssh)
    can_remove = permitted(unmount_argv("kind", container.name), ssh_policy, over_ssh=over_ssh)
    restricted = over_ssh and is_remote_session()
    addable = tuple(
        kind for kind in kinds if can_add and (not restricted or kind in remote_mountable)
    )
    removable = tuple(kind for kind in kinds if can_remove and kind in container.optional_mounts)
    return addable, removable


def mount_entries(
    container: ContainerInfo,
    kinds: Sequence[str],
    ssh_policy: RemoteSSHConfig | None,
    *,
    over_ssh: bool,
    remote_mountable: Sequence[str] = (),
) -> tuple[PickerEntry, ...]:
    """One entry per configured kind: attached ones unmount, the rest mount."""
    addable, removable = mount_directions(
        container, kinds, ssh_policy, over_ssh=over_ssh, remote_mountable=remote_mountable
    )
    entries: list[PickerEntry] = []
    for kind in kinds:
        if kind in container.optional_mounts:
            if kind in removable:
                entries.append(PickerEntry(f"● {kind} — unmount", _UNMOUNT_VALUE + kind))
        elif kind in addable:
            entries.append(PickerEntry(f"○ {kind} — mount", _MOUNT_VALUE + kind))
    return tuple(entries)


def mount_choice(value: str) -> tuple[str, bool]:
    """The kind a Mounts… entry names, and whether choosing it unmounts."""
    if value.startswith(_UNMOUNT_VALUE):
        return value.removeprefix(_UNMOUNT_VALUE), True
    return value.removeprefix(_MOUNT_VALUE), False


def mounts_picker(container: str, entries: Sequence[PickerEntry]) -> Picker:
    return Picker("container-mounts", f"Mounts — {container}", tuple(entries), target=container)


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


def mem_argv(name: str, size: str, *, force: bool = False) -> list[str]:
    """Set or clear an override without interpreting positional values as options."""
    return ["mem", *(["--force"] if force else []), "--", name, size]
