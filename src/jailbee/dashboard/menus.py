"""What can be done to a container or repo: action lists, verb sets and gates.

Frontend-agnostic: the terminal menus and the Qt action menu both read it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import actions as dact
from jailbee.dashboard.commands import (
    check_dashboard_command,
    dashboard_action_argv,
)
from jailbee.dashboard.model import AppMenuEntry, RepoGroup, RepoTarget, Row, _find_group
from jailbee.remote_ssh.router import RouteError

if TYPE_CHECKING:
    from jailbee.git_status import GitStatus


_NETWORK_MODES: tuple[str, ...] = ("strict", "loose")


@dataclass(frozen=True)
class MenuContext:
    """Everything :func:`menu_actions` needs to know about one row.

    Assembled by :func:`actions_for_container` from a ``ContainerInfo`` +
    ``RepoGroup`` pair. A dataclass rather than a tenth keyword argument: the
    call sites had already stopped being readable, and every field here is a
    plain fact about the row rather than an option.

    ``has_job`` is "there is a background-job row at all" (what makes the log
    worth offering); ``job_running`` is "its worker is still alive" (what makes
    ``--follow`` the right form); ``job_clearable`` is the failed/stale case
    that "Clear failed job" corrects.

    ``pr_author`` splits the PR containers the way the PR column's ``↓`` marker
    already does: False is a container built from someone else's PR (a review),
    True one whose PR jailbee opened from the container's own branch.

    ``apps`` mirrors the repo's GUI app registry (sourced from
    ``RepoGroup.apps``) rather than two integration switches — one
    "Launch <label>" entry appears per :class:`AppMenuEntry`, in order.

    ``remote`` is a remote SSH session (see :func:`run`), which gets no app
    launches: a GUI app would open on the host's display, not the client's.
    ``gui_remote`` (``remote.ssh.gui``) lets a remote session launch apps,
    which then draw on the shared RDP display.
    """

    state: str
    has_repo: bool
    mode: str = "clone"
    apps: list[AppMenuEntry] = field(default_factory=list)
    current_network: str | None = None
    pr_number: int | None = None
    pr_author: bool = False
    job_clearable: bool = False
    has_job: bool = False
    job_running: bool = False
    git_status: GitStatus | None = None
    remote: bool = False
    gui_remote: bool = False


@dataclass(frozen=True)
class MenuGroup:
    """A presentation-only submenu of already permitted action leaves."""

    label: str
    actions: tuple[tuple[str, str], ...]


MenuItem = tuple[str, str] | MenuGroup

_PR_MENU_VERBS = frozenset({"pr --open", "pr", "submodule pr", "review apply"})
_GIT_MENU_VERBS = frozenset(
    {"merge", "git pull", "git push", "git push --pr", "git retarget", "git diff"}
)
# Legacy apply leaves only ever exist with pending work: the terminal hoists
# them. "Outbox" is offered unless both outboxes are known to be empty, and
# `menu_actions` places it by its count.
_PENDING_APPLY_VERBS = frozenset({"review apply", "issue apply"})
_SHELL_VERB = frozenset({"shell"})
_TMUX_VERB = frozenset({"tmux"})


def group_menu_actions(
    actions: Sequence[tuple[str, str]],
    *,
    include_network: bool = False,
    terminal_order: bool = False,
) -> list[MenuItem]:
    """Group filtered Launch, PR and Git leaves; optionally group Network for the TUI.

    Relative order within each submenu and among ungrouped leaves is retained;
    this function never changes eligibility or adds executable verbs.

    ``terminal_order`` is the terminal dashboard's presentation, see
    :func:`_terminal_order`. It is opt-in because the Qt dashboard shares this
    function and keeps its order.
    """
    pr_verbs = _PR_MENU_VERBS - _PENDING_APPLY_VERBS if terminal_order else _PR_MENU_VERBS
    launch_actions = tuple(action for action in actions if action[0].startswith("Launch "))
    pr_actions = tuple(action for action in actions if action[1] in pr_verbs)
    git_actions = tuple(action for action in actions if action[1] in _GIT_MENU_VERBS)
    network_actions = tuple(action for action in actions if action[1].startswith("net "))
    result: list[MenuItem] = []
    seen: set[str] = set()
    for action in actions:
        verb = action[1]
        if action[0].startswith("Launch "):
            if "launch" not in seen:
                result.append(MenuGroup("Launch →", launch_actions))
                seen.add("launch")
        elif verb in pr_verbs:
            if "pr" not in seen:
                result.append(MenuGroup("PR →", pr_actions))
                seen.add("pr")
        elif verb in _GIT_MENU_VERBS:
            if "git" not in seen:
                result.append(MenuGroup("Git →", git_actions))
                seen.add("git")
        elif include_network and verb.startswith("net "):
            if "network" not in seen:
                result.append(MenuGroup("Network →", network_actions))
                seen.add("network")
        else:
            result.append(action)
    return _terminal_order(result) if terminal_order else result


def _terminal_order(items: list[MenuItem]) -> list[MenuItem]:
    """The terminal dashboard's arrangement of already grouped menu items.

    Pending apply leaves lead and ``Launch →`` follows the session entry.
    ``Git →``, ``PR →``, ``Lifecycle →`` (restart/stop/destroy; a lone Destroy
    stays a leaf) and ``Network →`` form one block, in that order, where the
    first of them used to be. "Open shell" is not listed: the ``s`` key and
    the ``!`` prompt still reach it, and it stays among the offered leaves
    those gate on. Everything else keeps its relative order.
    """

    def is_leaf(item: MenuItem, verbs: frozenset[str]) -> bool:
        return isinstance(item, tuple) and item[1] in verbs

    def group(label: str) -> MenuGroup | None:
        return next((i for i in items if isinstance(i, MenuGroup) and i.label == label), None)

    pending = [i for i in items if is_leaf(i, _PENDING_APPLY_VERBS)]
    rest = [
        i for i in items if not is_leaf(i, _PENDING_APPLY_VERBS) and not is_leaf(i, _SHELL_VERB)
    ]

    launch = group("Launch →")
    if launch is not None:
        rest.remove(launch)
        session = next((n for n, i in enumerate(rest) if is_leaf(i, _TMUX_VERB)), -1)
        rest.insert(session + 1, launch)

    lifecycle = [i for i in rest if isinstance(i, tuple) and i[1] in _CONTAINER_LIFECYCLE_VERBS]
    git, pr, network = group("Git →"), group("PR →"), group("Network →")
    block: list[MenuItem] = [g for g in (git, pr) if g is not None]
    block += [MenuGroup("Lifecycle →", tuple(lifecycle))] if len(lifecycle) > 1 else lifecycle
    block += [network] if network is not None else []
    members = {id(i) for i in (git, pr, network, *lifecycle) if i is not None}

    arranged: list[MenuItem] = []
    for item in rest:
        if id(item) not in members:
            arranged.append(item)
        elif block:
            arranged.extend(block)
            block = []
    return [*pending, *arranged]


# The GitStatus cell values that mean "there is provably nothing to do". Every
# other value — including "—" and "?" — means unknown, and an unknown answer
# never hides an entry.
_NO_COMMITS = "0"
_NO_CHANGES = "clean"


def _bridge_possible(ctx: MenuContext) -> bool:
    """Whether the PR and git-bridge verbs can run for this row at all.

    They all read the container's own clone, so they need a running container
    that has one: ``sync.assert_container_publishable`` rejects a stopped or
    mount-mode container up front, and offering an entry whose only outcome is
    that error is worse than not offering it.
    """
    return ctx.state == "Running" and ctx.mode != "mount"


def _has_commits_for_host(git: GitStatus | None) -> bool:
    """Whether `jailbee git pull` has commits to send to the host."""
    return git is None or git.ahead_count != _NO_COMMITS


def _has_diff_to_show(git: GitStatus | None) -> bool:
    """Whether `jailbee git diff` would print anything."""
    if git is None:
        return True
    return not (git.wt == _NO_CHANGES and git.ahead_count == _NO_COMMITS)


def _outbox_pending(git: GitStatus | None) -> int | None:
    """Manifests waiting in the PR and issue outboxes, or None when unprobed."""
    if git is None:
        return None
    counts = (git.pending_pr_actions, git.pending_issue_actions)
    if all(n is None for n in counts):
        return None
    return sum(n or 0 for n in counts)


def _outbox_empty(git: GitStatus | None) -> bool:
    """Whether the probe counted no manifest in *both* outboxes; unknown is not empty."""
    return git is not None and git.pending_pr_actions == 0 and git.pending_issue_actions == 0


def menu_actions(ctx: MenuContext) -> list[tuple[str, str]]:
    """(label, jailbee-subcommand) options for the highlighted container.

    Empty for orphan rows (no repo root ⇒ nothing to address a child at, see
    :meth:`RepoTarget.of`); a repo with no config file of its own is *not* one
    of those and gets the full menu. One "Launch <label>" entry appears per
    :class:`AppMenuEntry` in ``ctx.apps`` (sourced from ``RepoGroup.apps``,
    itself `apps.resolve_apps`) — offering only apps the repo's own config
    actually registers, since dispatching `jailbee <verb>` for one that is not
    would just fail.

    For running containers, one "Network: <mode>" entry appears per mode
    other than ``ctx.current_network`` (sourced from ``ContainerInfo.network``),
    dispatching the two-token ``jailbee net <mode>`` subcommand.

    Running rows lead with session actions, Outbox and app actions, followed by
    job diagnostics, PR leaves (including submodule PR when changes are probed),
    Git leaves, network modes and lifecycle actions.
    Git pull and diff are hidden when status proves they would do nothing;
    unknown status still offers them. Stopped rows lead with Start, followed
    by eligible diagnostics and Open PR, then Destroy.

    A review container — one carrying a PR that jailbee did not open from its
    own branch (``pr_number`` set, ``pr_author`` false) — gains "Refresh from
    PR head" beside the base update: the same `git push`, sourced from the PR
    instead of the base branch. It is withheld from an authored PR, whose head
    the container's branch is upstream of, so the refresh could only be a
    no-op.

    "Outbox" (``outbox browse``) is offered on addressable running containers,
    including mount mode, unless the probe counted no manifest in both
    outboxes; an unknown count still offers it. Its fixed stores do not require
    a clone or an existing PR. The Qt dashboard opens its own
    window for it; the terminal dashboard its own pickers (`jailbee.dashboard.outbox`).
    With manifests pending in the PR or issue outbox (read from
    ``ctx.git_status``) it leads the menu and
    carries the count; with an unknown count it follows "Open shell".

    Verbs may carry flags (``"pr --open"``, ``"job log --follow"``,
    ``"apps run <name> --container"`` for a config-sourced app — see
    :func:`_app_menu_verb`): every front-end splits them into argv, and Typer
    accepts options before the positional container name.
    """
    if not ctx.has_repo:
        return []
    actions: list[tuple[str, str]] = []
    if ctx.state == "Running":
        session = [("Attach tmux", "tmux"), ("Open shell", "shell")]
        pending = _outbox_pending(ctx.git_status)
        if pending:
            actions.extend([(f"Outbox ({pending} pending)", "outbox browse"), *session])
        elif _outbox_empty(ctx.git_status):
            actions.extend(session)
        else:
            actions.extend([*session, ("Outbox", "outbox browse")])
        for app in [] if (ctx.remote and not ctx.gui_remote) else ctx.apps:
            actions.append((f"Launch {app.label}", app.verb))
    elif ctx.state == "Stopped":
        actions.append(("Start", "start"))
    if ctx.job_clearable:
        actions.append(("Clear failed job", "job clear"))
    if ctx.has_job:
        actions.append(("Job log", "job log --follow" if ctx.job_running else "job log"))
    if ctx.pr_number is not None and not ctx.remote:
        # `pr --open` is a browser on the host's display.
        actions.append(("Open PR", "pr --open"))
    if _bridge_possible(ctx):
        actions.append(("Create/update PR", "pr"))
        if ctx.git_status is not None and ctx.git_status.submodules:
            # The submodule command opens its own picker. A submodule whose
            # gitlink is not yet bumped is missing from this probe.
            actions.append(("Create/update submodule PR…", "submodule pr"))
    if _bridge_possible(ctx):
        actions.append(("Merge into…", "merge"))
        if _has_commits_for_host(ctx.git_status):
            actions.append(("Send commits to host (git pull)", "git pull"))
        actions.append(("Update from base (git push)", "git push"))
        if ctx.pr_number is not None and not ctx.pr_author:
            actions.append(("Refresh from PR head (git push --pr)", "git push --pr"))
        actions.append(("Change base branch (git retarget)", "git retarget"))
        if _has_diff_to_show(ctx.git_status):
            actions.append(("Show diff (git diff)", "git diff"))
    if _bridge_possible(ctx):
        actions.append(("Fork…", "fork"))
    if ctx.state in ("Running", "Stopped"):
        # An alias is metadata, so a stopped container takes one too.
        actions.append(("Rename…", "rename"))
    if ctx.state == "Running":
        for mode in _NETWORK_MODES:
            if mode != ctx.current_network:
                actions.append((f"Network: {mode}", f"net {mode}"))
        actions.append(("Egress…", "net egress ls"))
        actions += [
            ("Restart", "restart"),
            ("Stop", "stop"),
            ("Destroy", "destroy"),
        ]
        return actions
    if ctx.state == "Stopped":
        actions.append(("Egress…", "net egress ls"))
    return [*actions, ("Destroy", "destroy")]


_CONTAINER_LIFECYCLE_VERBS = frozenset({"restart", "stop", "destroy"})
_JOB_VERBS = frozenset({"job clear", "job log", "job log --follow"})

# Container-menu verbs the terminal dashboard handles itself. They are never in
# the Qt-shared `menu_actions` list and never passed to `dispatch`.
TERMINAL_MENU_VERBS: frozenset[str] = frozenset({"credential-group", *dact.CONTAINER_VERBS})


def _insert_before_network(
    actions: Sequence[tuple[str, str]], extra: Sequence[tuple[str, str]]
) -> list[tuple[str, str]]:
    """``extra`` before the first ``net …`` leaf, else before lifecycle, else last."""
    at = next(
        (i for i, (_label, verb) in enumerate(actions) if verb.startswith("net ")),
        None,
    )
    if at is None:
        at = next(
            (i for i, (_label, verb) in enumerate(actions) if verb in _CONTAINER_LIFECYCLE_VERBS),
            len(actions),
        )
    return [*actions[:at], *extra, *actions[at:]]


def _insert_after_job(
    actions: Sequence[tuple[str, str]], extra: Sequence[tuple[str, str]]
) -> list[tuple[str, str]]:
    """``extra`` right after the last job entry; before network when there is none.

    There may be none: an SSH policy can hide `job log` while permitting
    `autostart status`.
    """
    at = max((i for i, (_label, verb) in enumerate(actions) if verb in _JOB_VERBS), default=None)
    if at is None:
        return _insert_before_network(actions, extra)
    return [*actions[: at + 1], *extra, *actions[at + 1 :]]


def _with_credential_group(actions: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """``actions`` with ``Credential group…`` just before network and lifecycle.

    That is before the first ``net …`` leaf (the ``Network →`` group), or the
    first lifecycle leaf when there is no network entry; last otherwise.
    """
    return _insert_before_network(actions, (("Credential group…", "credential-group"),))


def actions_for_container(
    groups: list[RepoGroup],
    name: str | None,
    *,
    remote: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
) -> list[tuple[str, str]]:
    """Resolve the ``(label, verb)`` action list for a container by name.

    Single source of truth shared by the TUI action menu, the Qt table view,
    and the Qt card view. Returns ``[]`` for an unknown container or a
    view-only (orphan) group. ``remote`` is :attr:`MenuContext.remote`; the
    Qt views never pass it, since a remote session never gets them.
    """
    group = _find_group(groups, name)
    if group is None or name is None:
        return []
    container = next((c for c in group.containers if c.name == name), None)
    if container is None:
        return []
    from jailbee import background

    job_clearable = (
        container.job_phase is not None
        and container.job_pid is not None
        and background.clearable(container.job_phase, container.job_pid)
    )
    actions = menu_actions(
        MenuContext(
            state=container.state,
            has_repo=RepoTarget.of(group) is not None,
            mode=container.mode,
            apps=group.apps,
            current_network=container.network,
            pr_number=container.pr_number,
            pr_author=container.pr_author,
            job_clearable=job_clearable,
            has_job=container.job_phase is not None,
            # A job row that is not clearable is one whose worker is still
            # alive — that is what makes `--follow` the right form.
            job_running=container.job_phase is not None and not job_clearable,
            git_status=container.git_status,
            remote=remote,
            gui_remote=bool(remote and ssh_policy is not None and ssh_policy.gui),
        )
    )
    if over_ssh:
        permitted: list[tuple[str, str]] = []
        for label, verb in actions:
            try:
                check_dashboard_command(
                    dashboard_action_argv(verb, name, force=verb in ATTACH_VERBS),
                    ssh_policy,
                    over_ssh=True,
                )
            except RouteError:
                continue
            permitted.append((label, verb))
        return permitted
    return actions


def view_only_note(groups: list[RepoGroup], name: str | None) -> str | None:
    """Why ``name`` offers no actions, as one user-facing sentence.

    ``None`` when there is nothing to explain: the container has actions, or
    it isn't on screen at all (a stale selection). Every front-end shows this
    the way its medium allows — a transient subtitle notice in the TUI, a
    disabled entry in the Qt menus — because an action menu that silently
    declines to open
    is indistinguishable from a broken one.

    The one remaining cause is an orphan group: jailbee-managed containers
    whose repo could not be located at all. It is deliberately not phrased as
    "no config loaded" any more — a repo with no config file still *has* a
    loaded config (a synthesized one) and is fully actionable, so that wording
    described the wrong thing.
    """
    group = _find_group(groups, name)
    if group is None or RepoTarget.of(group) is not None:
        return None
    return f"No repo found for '{group.prefix}' — '{name}' is view-only"


def new_container_target(groups: list[RepoGroup], selected: Row | None) -> RepoGroup | None:
    """The repo a new container would be created in, for the current selection.

    A container row yields its own group; a repo header yields that group.
    None when nothing is selected, when the selection is stale (the row moved
    out from under the cursor between frames), or when the group has no repo
    root to create in — an orphan group is jailbee-managed containers whose
    repo could not be located, the same reason it gets no action menu. A repo
    with no config file is not one of those: `jailbee new` run in its root
    synthesizes the same config the dashboard is already showing.
    """
    if selected is None:
        return None
    if selected.kind == "repo":
        group = next((g for g in groups if g.prefix == selected.key), None)
    else:
        group = _find_group(groups, selected.key)
    if group is None or RepoTarget.of(group) is None:
        return None
    return group


def new_container_reject_note_for_prefix(groups: list[RepoGroup], prefix: str) -> str | None:
    """Why a container cannot be created in the repo named ``prefix``, or None
    when it can.

    The prefix-keyed counterpart to :func:`new_container_reject_note`, shared
    by both front-ends so a single sentence is authored per refusal reason
    rather than each front-end wording it independently (that duplication is
    what let the Qt dashboard's "No repo selected" dialog fire for an orphan
    group it actually had a real prefix for). The TUI, which resolves a
    ``Row`` rather than a bare prefix, delegates to this for its repo-header
    case. An empty or unrecognised ``prefix`` gets the generic "select a
    repo" wording; a real group with no repo root names itself.
    """
    group = next((g for g in groups if g.prefix == prefix), None) if prefix else None
    if group is None:
        return "Select a repo or a container first"
    if RepoTarget.of(group) is not None:
        return None
    return f"'{group.prefix}' has no repo directory — nothing to create against"


REMOTE_CONFIG_EDIT_NOTE = "Config editing is not available over remote SSH"


def config_edit_reject_note_for_prefix(
    groups: list[RepoGroup], prefix: str, *, global_layer: bool = False
) -> str | None:
    """Why the config editor cannot be opened for ``prefix``, or None when it can.

    The config-editing twin of :func:`new_container_reject_note_for_prefix`:
    the same "is this a real repo" test (:meth:`RepoTarget.of`), but its own
    sentence, so a refusal names configuring rather than creating. Shared by
    both front-ends, so the wording is authored once.

    A repo with no ``.jailbee/config.yaml`` (``config_path is None`` on a group
    that does have a root) is refused for the *repo* layer only. Its effective
    config comes from a third source the editor knows nothing about —
    ``global.yaml``'s ``scratch.config`` — so every row would be wrong and the
    first save would create a file that stops that source being used at all.
    ``jailbee config edit`` refuses it too; refusing here as well is what turns
    "exited 1" into a sentence. The *global* layer is unaffected: it edits
    ``global.yaml``, which is exactly where such a directory's settings live.
    """
    group = next((g for g in groups if g.prefix == prefix), None) if prefix else None
    if group is None:
        return "Select a repo or a container first"
    if RepoTarget.of(group) is None:
        return f"'{group.prefix}' has no repo directory — no config to edit"
    if not global_layer and group.config_path is None:
        return (
            f"'{group.prefix}' has no config file — its settings come from "
            f"global.yaml's scratch.config. Run 'jailbee config init' there first"
        )
    return None


def new_container_reject_note(groups: list[RepoGroup], selected: Row | None) -> str | None:
    """Why a container cannot be created here, or None when it can.

    The counterpart to :func:`view_only_note`: a front-end that silently does
    nothing is indistinguishable from a broken one, so every refusal has a
    sentence naming its own cause. Delegates to
    :func:`new_container_reject_note_for_prefix` once a ``Row`` has been
    resolved to a prefix, so the "has no repo directory" sentence is phrased
    in exactly one place.
    """
    if new_container_target(groups, selected) is not None:
        return None
    if selected is None:
        return "Select a repo or a container first"
    if selected.kind == "repo":
        if not any(g.prefix == selected.key for g in groups):
            # Same wording the container branch below uses for the same
            # cause: the row's group vanished between frames. "Select a
            # repo" would be false advice — one was selected.
            return f"'{selected.key}' is no longer listed"
        return new_container_reject_note_for_prefix(groups, selected.key)
    group = _find_group(groups, selected.key)
    if group is None:
        return f"'{selected.key}' is no longer listed"
    return new_container_reject_note_for_prefix(groups, group.prefix)


def new_container_base_default(repo_root: str | None) -> str | None:
    """The branch ``repo_root``'s checkout is on, for the base field's default.

    Read from the *group's* repo, not the process's cwd: both dashboards are
    cross-repo, so the branch offered has to belong to the repo the row is in.
    None for a null root (an orphan group) or a detached HEAD — an empty field
    beats a guess, and `jailbee new` would fork off the wrong branch.
    """
    if repo_root is None:
        return None
    from jailbee import git

    return git.get_current_branch(Path(repo_root))


def host_branches(repo_root: str | None, *, exclude: str | None = None) -> tuple[str, ...]:
    """``repo_root``'s local branches, for a branch prompt's suggestions.

    The group's repo, not the cwd, for the same reason as
    :func:`new_container_base_default`. Empty for a null root (an orphan group)
    or a failing ``git`` — the prompt then takes plain text.
    """
    if repo_root is None:
        return ()
    from jailbee import git

    return tuple(b for b in git.list_branches(Path(repo_root)) if b != exclude)


def new_container_argv(target: RepoTarget, branch: str, base: str) -> list[str]:
    """``jailbee new <branch> <base>``, plus ``target``'s ``--config`` if any.

    A repo with no config file gets no flag; the caller runs the child in
    ``target.cwd()`` instead — see :class:`RepoTarget`.

    ``base`` is positional, not a flag: `jailbee new`'s second positional is
    the branch a *new* branch forks off (`lifecycle.resolve_clone_ref`).
    Omitted, a new branch forks off `cfg.default_branch` instead — which is
    not what someone picking their current branch means. (`--from-base` is the
    golden-image alias and has nothing to do with git.)

    `--background`: creation detaches and the terminal returns at once instead
    of holding the operator for the whole provision; progress shows in the JOB
    column.

    No `--yes`: `jailbee new` asks about reusing an existing branch and about
    the branch-autostart escalation, and the TUI gives it a terminal to ask in
    rather than answering for the user — by re-running it in the foreground
    when a detached attempt stopped to ask. Those questions are asked by the
    foreground parent before it detaches.

    Both answers are typed free text, so they follow `--`: a branch named
    `--mount` or `--yes` is refused as a branch name by `jailbee new`, never
    read as the option it spells.
    """
    return ["jailbee", "new", *target.flags(), "--background", "--", branch, base]


def fork_container_argv(target: RepoTarget, source: str, name: str) -> list[str]:
    """``jailbee fork --background <source> <name>``; both names follow ``--``."""
    return ["jailbee", "fork", *target.flags(), "--background", "--", source, name]


def rename_argv(container: str, alias: str) -> list[str]:
    """``rename <container> -- <alias>``, or ``rename <container> --clear`` for an empty alias.

    The alias follows ``--``: a typed ``--clear`` or ``-x`` is an alias for
    `aliases.set_alias` to judge, never an option.
    """
    return ["rename", container, "--", alias] if alias else ["rename", container, "--clear"]


def new_pr_container_argv(target: RepoTarget, number: int) -> list[str]:
    """Create a review container using the CLI's existing PR resolution flow."""
    return ["jailbee", "new", *target.flags(), "--background", "--pr", str(number)]


# Verbs routed through the CLI's attach guard, which asks "continue anyway?"
# when the container's background job failed or is unfinished. Both dashboards
# have already shown that state in the JOB column, so the question would only
# ask the operator to re-read what they were looking at when they acted on the
# row — hence both dispatch these with `--force`. Shared rather than copied, for
# the same reason as :data:`PRINTING_VERBS` (`qtui/actions.py` imports this).
#
# `qtui/actions.py` derives `_ASSUME_YES_VERBS` from this at import time,
# before any `Config` exists, so it must stay a plain module-level constant.
# The one place that decides `--force` at runtime (`_dispatch_action`, below)
# takes a `RepoTarget` — repo_root/config_path, no loaded `Config` — so it too
# reads this constant rather than a repo's own `apps:` entries. A config-aware
# version would need a real call site with a `Config` in hand before it is
# worth adding.
ATTACH_VERBS: frozenset[str] = frozenset({"shell", "tmux", "ide", "chrome", "firefox", "browser"})

# The attach verbs that open a window rather than a terminal.
_GUI_VERBS: frozenset[str] = ATTACH_VERBS - {"shell", "tmux"}

# The verb prefix `_app_menu_verb` composes for a config-sourced `apps:`
# entry (see its docstring). These are attach verbs too — the app launches
# in a container exactly like `ide`/`chrome` — but can't join ATTACH_VERBS
# itself: each carries its own app name, so there is no fixed set of them to
# enumerate. Checked by prefix instead, in :func:`_dispatch_action` and (the
# same reason as :data:`PRINTING_VERBS`) `qtui/actions.py`'s own dispatch.
APPS_RUN_PREFIX = "apps run "


def _is_gui_verb(verb: str) -> bool:
    return verb in _GUI_VERBS or verb.startswith(APPS_RUN_PREFIX)


# Verbs whose whole point is the text they print, rather than the state they
# change. Both front-ends need to know which those are — the TUI to keep their
# output on screen, the Qt dashboard to route it into a window of its own
# (`qtui/actions.py` imports this) — so the list lives here, beside
# :func:`menu_actions`, which is where the verb vocabulary is defined.
#
# Matched exactly, not by leading token: `pr --open` only opens a browser, and
# `job log` and `git push` each appear in two forms.
PRINTING_VERBS: frozenset[str] = frozenset(
    {
        "pr",
        "submodule pr",
        "git push",
        "git push --pr",
        "git pull",
        "git diff",
        "git retarget",
        "merge",
        "job log",
        "job log --follow",
        "net egress ls",
    }
)
