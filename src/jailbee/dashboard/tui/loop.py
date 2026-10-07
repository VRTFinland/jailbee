"""jailbee dashboard — the live, auto-refreshing terminal view and its key loop.

Container state is read ONLY through the shared state service.
"""

from __future__ import annotations

import logging
import os
import select
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jailbee.accounts.groups import RESERVED_GROUP_NAMES
from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import accounts as da
from jailbee.dashboard import actions as dact
from jailbee.dashboard import outbox as dob
from jailbee.dashboard.columns import (
    all_column_names,
    clamp_column_offset,
    default_columns,
    nonempty_columns,
    optimize_column_widths,
    seed_view_state,
    settings_repo_prefixes,
)
from jailbee.dashboard.commands import (
    check_dashboard_command,
    command_argv,
    completion_candidates,
    dashboard_action_argv,
    insert_options_before_separator,
    permitted,
)
from jailbee.dashboard.dispatch import (
    DispatchStyle,
    _dispatch_action,
    _run_cli_foreground,
    _wait_for_return,
    command_needs_pause,
)
from jailbee.dashboard.egress import (
    EgressState,
    egress_argv,
    move_egress,
    removable_entry,
    replace_egress_rows,
)
from jailbee.dashboard.egress_data import load_egress_rows
from jailbee.dashboard.jobs import JobResult, JobRunner, needs_terminal
from jailbee.dashboard.menus import (
    APPS_RUN_PREFIX,
    ATTACH_VERBS,
    REMOTE_CONFIG_EDIT_NOTE,
    actions_for_container,
    config_edit_reject_note_for_prefix,
    host_branches,
    new_container_argv,
    new_container_base_default,
    new_container_reject_note,
    new_container_target,
    new_pr_container_argv,
    view_only_note,
)
from jailbee.dashboard.model import (
    NOTHING_TO_SHOW,
    STARTUP_TIMEOUT_SECONDS,
    RepoGroup,
    RepoTarget,
    Row,
    _find_group,
    collect_repo_roots,
    container_of,
    dashboard_config_migration_notice,
    dashboard_group_notices,
    fold_target,
    move_selection,
    present,
    prompt_target_kind,
    reconcile_selection,
    selectable_rows,
    target_group,
    toggle_folded,
)
from jailbee.dashboard.overlays import (
    Picker,
    PickerEntry,
    TextPrompt,
    handle_prompt_key,
    move_picker,
    parse_pr_number,
    picked,
)
from jailbee.dashboard.settings import (
    SettingsState,
    enabled_names,
    move_settings,
    open_settings,
    switch_tab,
    toggle_current,
)
from jailbee.dashboard.tui.frame import render
from jailbee.dashboard.tui.keys import parse_key, quick_reject_note, quick_verb
from jailbee.dashboard.tui.menu_state import (
    MenuState,
    RepoMenuState,
    back_menu,
    enter_menu,
    hotkey_menu,
    move_menu,
    open_menu,
    open_repo_menu,
)
from jailbee.dashboard.tui.overlay import (
    CommandState,
    Overlay,
    _egress_panel,
    _with_egress_panel,
    edit_command,
)
from jailbee.dashboard.tui.terminal import set_terminal_title, terminal_title, terminal_title_scope
from jailbee.dashboard.visibility import visible_repo_groups
from jailbee.db.view_prefs import ViewState, save_view_state
from jailbee.lifecycle import (
    tracking_notices,
)
from jailbee.remote_ssh import router as ssh_router
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.remote_ssh.router import RouteError
from jailbee.remote_ssh.session import host_restricted
from jailbee.state_service import StateServiceUnavailable
from jailbee.tui import console, error

if TYPE_CHECKING:
    from collections.abc import Callable

    from jailbee.incus import Incus
    from jailbee.state_service.client import StateClient


log = logging.getLogger(__name__)


def open_state_client(cwd_root: Path | None) -> StateClient:
    """A started `StateClient` for this dashboard (the tests' seam)."""
    from jailbee.state_service.client import StateClient

    client = StateClient(cwd_root)
    client.start()
    return client


_KEY_READ_BYTES = 8  # covers all standard arrow/function-key CSI sequences
_NOTICE_SECONDS = 2.5  # how long a transient subtitle message stays up
_FAILURE_NOTICE_SECONDS = 8.0  # a refused account command's reason, long enough to read


def run(
    incus: Incus,
    cwd_root: Path | None,
    *,
    remote: bool = False,
    over_ssh: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    scope: RemoteRepoScope | None = None,
) -> int:
    """Main dashboard loop.

    Container state comes from the shared state service
    (`jailbee.state_service`), which gathers once for every open dashboard;
    this thread only renders the latest snapshot it pushed — with this
    dashboard's own scope and cwd pin applied — and handles input on a fast
    timer, so keystrokes stay responsive while a gather is in flight.

    ``remote`` is a remote SSH session, whose user may reach containers and
    the repos' git bridge but not the host itself. Everything here that runs
    on the host beyond that is withheld: the config editor (a config decides
    host mounts and the SSH policy itself), the pager (which can start a
    shell) and GUI app launches (which open on the host's display).
    """
    from rich.live import Live

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        error("jailbee dashboard requires an interactive terminal.")
        return 1

    # Launch-time guard only; the state service re-resolves the list per gather.
    roots = (
        collect_repo_roots(cwd_root) if scope is None else collect_repo_roots(cwd_root, scope=scope)
    )
    if not roots:
        error(NOTHING_TO_SHOW)
        return 1

    from jailbee.db import get_engine
    from jailbee.db.view_prefs import FRONTEND_TUI

    # Resolved once for the whole run — a live-refreshing dashboard must not
    # re-merge config on every frame.
    engine = get_engine()
    column_notices: list[str] = []
    view_state = seed_view_state(engine, FRONTEND_TUI, on_migration=column_notices.append)
    column_notice = "; ".join(column_notices) if column_notices else None
    config_notice = dashboard_config_migration_notice()
    if config_notice:
        column_notice = "; ".join(filter(None, (column_notice, config_notice)))
    enabled: tuple[str, ...] | None = view_state.columns
    folded: frozenset[str] = view_state.folded
    show_empty_repos = view_state.show_empty_repos
    hidden_repos = view_state.hidden_repos
    show_details = view_state.show_details
    column_widths: dict[str, int] | None = None
    column_offset = 0

    def now() -> datetime:
        return datetime.now().astimezone()

    # Waited for before `Live` takes the screen, so the first frame is already
    # populated — and so "the state service is unreachable" is said on the
    # user's own terminal rather than by taking the screen only to hand it back.
    client = open_state_client(cwd_root)
    try:
        with console.status("⏳ Surveying containers…"):
            client.wait_first_snapshot(STARTUP_TIMEOUT_SECONDS)
    except StateServiceUnavailable as exc:
        client.close()
        error(f"dashboard refresh failed: {exc}")
        return 1

    jobs = JobRunner()

    fd = sys.stdin.fileno()
    old_term = termios.tcgetattr(fd)
    selected: Row | None = None
    sel_index = 0
    overlay: Overlay | None = None
    egress_parent: MenuState | RepoMenuState | None = None
    notice: str | None = column_notice
    notice_until = time.monotonic() + 10.0 if column_notice else 0.0

    def set_notice(text: str, seconds: float = _NOTICE_SECONDS) -> None:
        """Show ``text`` in the panel subtitle for ``seconds``.

        The dashboard owns the whole screen while Live is running, so a
        rejected key or a view-only row has nowhere to print — but staying
        silent is indistinguishable from being broken, hence this. A failure
        worth reading (a refused account command) is kept up longer.
        """
        nonlocal notice, notice_until
        notice = text
        notice_until = time.monotonic() + seconds

    def persist_view_state(state: ViewState) -> None:
        """Write ``state`` to ``view_prefs``, degrading instead of crashing.

        The repo menu and settings overlay commit to SQLite straight from a
        keypress (fold and setting toggles).
        ``run()``'s own ``try`` only catches ``KeyboardInterrupt``, so a
        write failure here (``database is locked`` against a concurrent
        background worker, a read-only state dir) would otherwise end the
        whole session with a traceback. The fold/toggle already took effect
        on screen by the time this runs — only persistence is lost.
        """
        try:
            save_view_state(engine, FRONTEND_TUI, state)
        except Exception:
            log.debug("failed to save dashboard view state", exc_info=True)
            set_notice("could not save view settings")

    def open_settings_overlay() -> SettingsState:
        """A fresh settings overlay over the current ``groups``/``folded``.

        A small closure rather than inlining twice: opening from the plain
        table and switching in from another overlay (see the `F2` handling
        below) both need it.
        """
        return open_settings(
            field_names=all_column_names(),
            enabled=frozenset(enabled if enabled is not None else default_columns()),
            repo_prefixes=settings_repo_prefixes(all_groups, folded),
            folded=folded,
            visibility_repo_prefixes=tuple(dict.fromkeys(g.prefix for g in all_groups)),
            show_empty_repos=show_empty_repos,
            hidden_repos=hidden_repos,
        )

    last_title: str | None = None
    try:
        tty.setcbreak(fd)
        # Pushed before Live takes the screen and popped after it gives it
        # back, so the terminal's own title is saved and restored intact.
        with (
            terminal_title_scope(sys.stdout),
            Live(console=console, screen=True, auto_refresh=False) as live,
        ):

            def foreground(fn: Callable[[], int]) -> int:
                """Hand the terminal to a real ``jailbee`` command, then take it back.

                Interactive verbs (``tmux``, ``shell``) need the raw terminal
                and the normal screen, so Live is stopped for the duration —
                but only for the *dispatch*. Opening the menu no longer
                touches the terminal at all, which is what keeps the
                dashboard on screen behind it.
                """
                nonlocal last_title
                # Nothing of this dashboard is on screen while `fn` runs: let
                # the shared service stop gathering on its behalf.
                client.set_active(False)
                live.stop()
                termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
                try:
                    return fn()
                finally:
                    tty.setcbreak(fd)
                    live.start(refresh=True)
                    # The snapshot is as old as the command was long.
                    client.set_active(True)
                    client.refresh()
                    # `fn` (jailbee shell / tmux) may have set its own OSC 2
                    # title; forget the last one we wrote so the next frame's
                    # title-changed check doesn't compare against it and skip
                    # the rewrite, leaving the child's title on screen forever.
                    last_title = None

            def _report_vanished_repo(repo: RepoTarget) -> None:
                """Notice-and-refresh for an `OSError` from a repo-rooted dispatch.

                Shared by `dispatch` and `run_new_container`: both hand a real
                repo root to a child process as `cwd`, and both can have that
                directory vanish between a refresh and the keypress that
                dispatches — `subprocess`/`Popen` raise, not exit non-zero,
                for a missing `cwd`. Previously uncaught on either path, this
                took the whole TUI down.
                """
                set_notice(f"'{repo.repo_root}' no longer exists")
                client.refresh()

            def dispatchable(target: str, verb: str) -> RepoTarget | None:
                """The repo to run ``verb`` on ``target`` in, or None after noticing why not.

                The SSH policy and the action's current availability, checked
                before anything is shown or run.
                """
                group = _find_group(groups, target)
                if group is None:
                    return None
                repo = RepoTarget.of(group)
                if repo is None:
                    return None  # an orphan group: no repo root to address a child at
                try:
                    check_dashboard_command(
                        dashboard_action_argv(
                            verb,
                            target,
                            force=verb in ATTACH_VERBS or verb.startswith(APPS_RUN_PREFIX),
                        ),
                        ssh_policy,
                        over_ssh=over_ssh,
                    )
                except RouteError as exc:
                    set_notice(str(exc))
                    return None
                if verb not in {
                    current_verb
                    for _label, current_verb in actions_for_container(
                        groups,
                        target,
                        remote=remote,
                        ssh_policy=ssh_policy,
                        over_ssh=over_ssh,
                    )
                }:
                    set_notice(f"Action '{verb}' is no longer available for '{target}'")
                    return None
                return repo

            def dispatch(target: str, verb: str) -> None:
                repo = dispatchable(target, verb)
                if repo is None:
                    return
                try:
                    rc = foreground(
                        lambda: _dispatch_action(
                            repo,
                            verb,
                            target,
                            remote=remote,
                            over_ssh=over_ssh,
                            ssh_policy=ssh_policy,
                        )
                    )
                except RouteError as exc:
                    set_notice(str(exc))
                    return
                except OSError:
                    _report_vanished_repo(repo)
                    return
                if rc != 0:
                    set_notice(f"'jailbee {verb} {target}' exited {rc}")
                client.refresh()  # an action likely changed state — refresh ASAP

            def open_egress(prefix: str, container: str | None) -> EgressState | None:
                """Load one scoped view after checking the read permission."""
                group = next((item for item in groups if item.prefix == prefix), None)
                if group is None or RepoTarget.of(group) is None:
                    set_notice(f"'{prefix}' is no longer listed or has no repo directory")
                    return None
                if container is not None and not any(c.name == container for c in group.containers):
                    set_notice(f"'{container}' is no longer listed")
                    return None
                argv = ["net", "egress", "ls", *([container] if container else ["--repo"])]
                try:
                    check_dashboard_command(argv, ssh_policy, over_ssh=over_ssh)
                    rows = load_egress_rows(Path(group.repo_root or ""), incus, container)
                except Exception as exc:
                    set_notice(f"could not load egress entries: {exc}")
                    return None
                state = EgressState(prefix, container, rows)
                return replace(
                    state,
                    can_add=egress_permitted(state, "add"),
                    can_rm=egress_permitted(state, "rm"),
                )

            def egress_permitted(state: EgressState, action: Literal["add", "rm"]) -> bool:
                """Check the current remote policy for the scoped mutation."""
                try:
                    check_dashboard_command(
                        egress_argv(state, action, "example.com"),
                        ssh_policy,
                        over_ssh=over_ssh,
                    )
                except RouteError:
                    return False
                return True

            def egress_target(state: EgressState) -> RepoTarget | None:
                """The panel's repo, or None once it or its container is gone."""
                group = next((item for item in groups if item.prefix == state.prefix), None)
                target = RepoTarget.of(group) if group is not None else None
                if (
                    group is None
                    or target is None
                    or (
                        state.container is not None
                        and not any(c.name == state.container for c in group.containers)
                    )
                ):
                    return None
                return target

            def begin_egress_add(state: EgressState) -> TextPrompt | EgressState | None:
                """Open the destination question, or explain why not."""
                if egress_target(state) is None:
                    set_notice("Egress target is no longer available")
                    return None
                if not egress_permitted(state, "add"):
                    set_notice("net egress add is not permitted by the SSH policy")
                    return state
                return TextPrompt(
                    "egress-add",
                    "Add egress override",
                    "Destination (host, host:port, *.domain, IPv4, or CIDR)",
                    target=state.prefix,
                    back=state,
                )

            def mutate_egress(
                state: EgressState, action: Literal["add", "rm"], entry: str | None = None
            ) -> EgressState | None:
                """Reauthorize and start one scoped mutation, detached.

                ``add`` takes its destination from the inline prompt
                (:func:`begin_egress_add`); ``rm`` acts on the selected row.
                The panel stays up while the change runs. When it ends,
                ``finish`` closes the panel *and* the menu it was opened from
                after a success; a failure keeps the panel up, reloaded, for
                a retry.
                """
                target = egress_target(state)
                if target is None:
                    set_notice("Egress target is no longer available")
                    return None
                if action == "rm":
                    entry = removable_entry(state)
                    if not entry:
                        set_notice("Select a removable override first")
                        return state
                assert entry, "add needs the destination from the inline prompt"
                # Rechecked at submit: the policy may have changed while the
                # destination question was open.
                if not egress_permitted(state, action):
                    set_notice(f"net egress {action} is not permitted by the SSH policy")
                    return state
                argv = [
                    *egress_argv(state, action, entry),
                    *(target.flags() if not over_ssh else []),
                ]
                try:
                    check_dashboard_command(argv, ssh_policy, over_ssh=over_ssh)
                except RouteError as exc:
                    set_notice(str(exc))
                    return state

                key = f"egress:{state.prefix}:{state.container or ''}"
                if jobs.busy(key):
                    set_notice("An egress change is still running here")
                    return state

                def finish(result: JobResult) -> None:
                    """Report the change and refresh the panel, if it is still open."""
                    nonlocal overlay
                    if result.returncode != 0:
                        reason = result.failure_line() or f"exited {result.returncode}"
                        set_notice(
                            f"net egress {action} {entry} failed: {reason}",
                            seconds=_FAILURE_NOTICE_SECONDS,
                        )
                    else:
                        client.refresh()
                        set_notice(f"net egress {action} {entry}: done")
                    panel = _egress_panel(overlay)
                    if panel is None or (panel.prefix, panel.container) != (
                        state.prefix,
                        state.container,
                    ):
                        return
                    if result.returncode == 0:
                        # Done is done: the panel and the menu it was opened
                        # from close, as every other menu action does.
                        overlay = None
                        return
                    try:
                        rows = load_egress_rows(target.repo_root, incus, state.container)
                    except Exception as exc:
                        set_notice(f"could not refresh egress entries: {exc}")
                        return
                    overlay = _with_egress_panel(overlay, replace_egress_rows(panel, rows))

                try:
                    jobs.start(
                        key,
                        f"egress {action} {entry}…",
                        ["jailbee", *argv],
                        target.cwd(),
                        finish,
                    )
                except OSError:
                    _report_vanished_repo(target)
                    return None
                return state

            def start_new_container(*, from_pr: bool = False) -> TextPrompt | None:
                """Open the first question of `jailbee new`, or explain why not.

                The questions are inline overlays. The final `jailbee new` runs
                detached (`JobRunner`), so the dashboard stays usable through
                its foreground pre-flight (egress DNS, fetch, ref resolution).
                `jailbee new` asks its own questions: confirming reuse of an
                existing branch, and the branch-autostart escalation gate.
                The argv carries `--background`, which does not avoid those
                questions — the escalation question is asked by the foreground
                parent before it detaches (`lifecycle._autostart_approved`) —
                and a detached run has no terminal to ask on. When it stops
                for that reason the command is re-run through `foreground`
                (`needs_terminal`). The only other option is `--yes`, i.e.
                accepting a network-widening branch config unseen.
                """
                try:
                    check_dashboard_command(["new"], ssh_policy, over_ssh=over_ssh)
                except RouteError as exc:
                    set_notice(str(exc))
                    return None
                note = new_container_reject_note(groups, selected)
                if note is not None:
                    set_notice(note)
                    return None
                group = new_container_target(groups, selected)
                assert group is not None  # guaranteed by the note being None
                if from_pr:
                    return TextPrompt(
                        "new-pr", "New container from a PR", "PR number", target=group.prefix
                    )
                base_default = new_container_base_default(group.repo_root)
                return TextPrompt(
                    "new-branch",
                    "New container",
                    "New branch",
                    target=group.prefix,
                    carry=(base_default or "",),
                )

            def run_new_container(
                prefix: str, what: str, build_argv: Callable[[RepoTarget], list[str]]
            ) -> None:
                """Re-resolve the repo (it may have vanished while the prompt was open) and run."""
                group = next((g for g in groups if g.prefix == prefix), None)
                repo = RepoTarget.of(group) if group is not None else None
                if repo is None:
                    set_notice(f"'{prefix}' is no longer listed")
                    return
                argv = build_argv(repo)
                try:
                    check_dashboard_command(argv[1:], ssh_policy, over_ssh=over_ssh)
                except RouteError as exc:
                    set_notice(str(exc))
                    return

                def spawn() -> int:
                    rc = subprocess.run(argv, check=False, cwd=repo.cwd()).returncode
                    _wait_for_return()
                    return rc

                def run_in_foreground() -> None:
                    try:
                        rc = foreground(spawn)
                    except OSError:
                        _report_vanished_repo(repo)
                        return
                    if rc != 0:
                        set_notice(f"'jailbee new' exited {rc}")
                    client.refresh()  # the new container should appear on the next frame

                def finish(result: JobResult) -> None:
                    if needs_terminal(result):
                        # It stopped to ask something; the question needs the
                        # real terminal, so ask it there, as before.
                        run_in_foreground()
                        return
                    if result.returncode != 0:
                        reason = result.failure_line() or f"exited {result.returncode}"
                        set_notice(f"jailbee new failed: {reason}", seconds=_FAILURE_NOTICE_SECONDS)
                    client.refresh()

                try:
                    jobs.start(
                        f"new:{prefix}:{what}", f"creating {what}…", argv, repo.cwd(), finish
                    )
                except ValueError:
                    set_notice("That container is already being created")
                except OSError:
                    _report_vanished_repo(repo)

            def run_dashboard_command(
                target: str,
                kind: Literal["repo", "container"],
                argv: list[str],
                *,
                style: DispatchStyle = "output",
            ) -> None:
                """Hand the terminal to one dashboard-built `jailbee` command; notice a failure.

                ``target`` is re-resolved here because the row may have vanished
                while a picker was open. The policy is checked before `foreground`
                blanks the screen, and again by `_run_cli_foreground` right
                before the spawn.
                """
                repo = repo_for(target, kind)
                if repo is None:
                    set_notice(f"'{target}' is gone")
                    return
                try:
                    check_dashboard_command(argv, ssh_policy, over_ssh=over_ssh)
                except RouteError as exc:
                    set_notice(str(exc), seconds=_FAILURE_NOTICE_SECONDS)
                    return
                try:
                    rc = foreground(
                        lambda: _run_cli_foreground(
                            repo,
                            argv,
                            style=style,
                            remote=remote,
                            over_ssh=over_ssh,
                            ssh_policy=ssh_policy,
                        )
                    )
                except RouteError as exc:
                    set_notice(str(exc), seconds=_FAILURE_NOTICE_SECONDS)
                    return
                except OSError:
                    _report_vanished_repo(repo)
                    return
                if rc != 0:
                    set_notice(f"'jailbee {dact.command_label(argv)}' exited {rc}")
                client.refresh()  # the command likely changed state: refresh now

            def open_container_entry(container: str, verb: str) -> Overlay | None:
                """The first step of a terminal-only container entry; None once it has run."""
                if verb == dact.AUTOSTART_STATUS:
                    run_dashboard_command(
                        container, "container", dact.autostart_status_argv(container)
                    )
                    return None
                if verb == dact.AUTOSTART_CANCEL:
                    return dact.autostart_cancel_picker(container)
                if verb == dact.SNAPSHOTS:
                    return open_snapshots(container)
                if verb in (dact.MOUNT_ADD, dact.MOUNT_REMOVE):
                    return open_mount_picker(container, remove=verb == dact.MOUNT_REMOVE)
                return None

            def open_retarget(container: str) -> TextPrompt | None:
                """Ask for the new base inline; the CLI's own picker would blank the screen."""
                if dispatchable(container, "git retarget") is None:
                    return None
                group = _find_group(groups, container)
                info = (
                    next((c for c in group.containers if c.name == container), None)
                    if group is not None
                    else None
                )
                if group is None or info is None:
                    set_notice(f"'{container}' is gone")
                    return None
                current = info.base_branch
                return TextPrompt(
                    "container-retarget",
                    f"Retarget '{container}' (base: {current or 'unset'})",
                    "Base branch",
                    target=container,
                    suggestions=host_branches(group.repo_root, exclude=current),
                    require_suggestion=True,
                )

            def open_mount_picker(container: str, *, remove: bool) -> Picker | None:
                """The kinds Mount… (Unmount…) can act on right now, or a notice."""
                group = _find_group(groups, container)
                info = (
                    next((c for c in group.containers if c.name == container), None)
                    if group is not None
                    else None
                )
                if group is None or info is None:
                    set_notice(f"'{container}' is gone")
                    return None
                kinds = dact.mount_choices(info, group.optional_mounts, remove=remove)
                if not kinds:
                    set_notice(
                        "No optional mount to remove" if remove else "No optional mount to add"
                    )
                    return None
                return dact.mount_picker(container, kinds, remove=remove)

            def open_snapshots(container: str) -> Picker | None:
                """List the container's snapshots quietly and offer them, or notice why not.

                Each entry is gated on its own argv: over SSH an allowlist may
                permit the listing and not the create.
                """
                repo = repo_for(container, "container")
                if repo is None:
                    set_notice(f"'{container}' is gone")
                    return None
                argv = dact.addressed(
                    dact.snapshot_ls_argv(container), repo.flags(), over_ssh=over_ssh
                )
                try:
                    check_dashboard_command(argv, ssh_policy, over_ssh=over_ssh)
                    result = da.run_cli_quiet(argv, cwd=repo.cwd())
                    if not result.ok:
                        raise dact.SnapshotLoadError(result.message)
                    rows = dact.parse_snapshot_rows(result.stdout)
                except (RouteError, dact.SnapshotLoadError) as exc:
                    set_notice(f"could not list snapshots: {exc}", seconds=_FAILURE_NOTICE_SECONDS)
                    return None
                picker = dact.snapshot_picker(
                    container,
                    rows,
                    can_create=permitted(
                        dact.snapshot_create_argv(container, None), ssh_policy, over_ssh=over_ssh
                    ),
                )
                if not picker.entries:
                    set_notice(f"No snapshots of '{container}'")
                    return None
                return picker

            # The last listing per container, so a proposal step needs no second one.
            outbox_rows: dict[str, tuple[dob.ProposalRow, ...]] = {}

            def open_outbox(container: str) -> Picker | None:
                """List the container's staged proposals quietly and offer them, or notice why not.

                Each entry is gated on its own argv, as for snapshots.
                """
                repo = repo_for(container, "container")
                if repo is None:
                    set_notice(f"'{container}' is gone")
                    return None
                argv = dact.addressed(
                    dob.outbox_ls_argv(container), repo.flags(), over_ssh=over_ssh
                )
                try:
                    check_dashboard_command(argv, ssh_policy, over_ssh=over_ssh)
                    # `outbox ls` exits 2 for an unavailable container but still
                    # prints the listing, whose error says why; parse it first.
                    result = da.run_cli_quiet(argv, cwd=repo.cwd())
                    try:
                        listing = dob.parse_outbox_listing(result.stdout, container)
                    except dob.OutboxLoadError:
                        if result.ok:
                            raise
                        raise dob.OutboxLoadError(result.message) from None
                except (RouteError, dob.OutboxLoadError) as exc:
                    set_notice(f"could not list the outbox: {exc}", seconds=_FAILURE_NOTICE_SECONDS)
                    return None
                if listing.error is not None:
                    set_notice(
                        f"could not read the outbox: {listing.error}",
                        seconds=_FAILURE_NOTICE_SECONDS,
                    )
                    return None
                if not listing.rows:
                    set_notice(
                        listing.warnings[0]
                        if listing.warnings
                        else f"Outbox of '{container}' is empty",
                        seconds=_FAILURE_NOTICE_SECONDS if listing.warnings else _NOTICE_SECONDS,
                    )
                    return None
                outbox_rows[container] = listing.rows
                return dob.outbox_picker(
                    container,
                    listing.rows,
                    can_browse=permitted(
                        dob.outbox_browse_argv(container), ssh_policy, over_ssh=over_ssh
                    ),
                )

            def submit_outbox_picker(picker: Picker, entry: PickerEntry) -> Overlay | None:
                """The `container-outbox*` steps. Show and Publish run in the terminal.

                Publishing talks to GitHub and may outlast the quiet runner's
                60 s cutoff; a delete is local and runs quietly.
                """
                container = picker.target
                if picker.purpose == "container-outbox":
                    if entry.value == dob.BROWSE:
                        run_dashboard_command(
                            container, "container", dob.outbox_browse_argv(container), style="plain"
                        )
                        return None
                    pid = dob.proposal_id(entry.value)
                    row = next((r for r in outbox_rows.get(container, ()) if r.id == pid), None)
                    if row is None:
                        return None
                    actions = dob.proposal_picker(
                        container,
                        row,
                        can_show=permitted(
                            dob.outbox_show_argv(container, row.id), ssh_policy, over_ssh=over_ssh
                        ),
                        can_publish=permitted(
                            dob.outbox_apply_argv(container, row.id, row.revision),
                            ssh_policy,
                            over_ssh=over_ssh,
                        ),
                        can_delete=permitted(
                            dob.outbox_drop_argv(container, row.id, row.revision),
                            ssh_policy,
                            over_ssh=over_ssh,
                        ),
                    )
                    if not actions.entries:
                        set_notice(f"Nothing can be done to {row.id} here")
                        return None
                    return actions
                if picker.purpose == "container-outbox-proposal":
                    pid, revision, count = picker.carry
                    if entry.value == dob.SHOW:
                        run_dashboard_command(
                            container,
                            "container",
                            dob.outbox_show_argv(container, pid),
                            style="paged",
                        )
                        return None
                    if entry.value in (dob.PUBLISH, dob.DELETE):
                        return dob.outbox_confirm_picker(
                            container, entry.value, pid, revision, int(count)
                        )
                    return None
                if picker.purpose == "container-outbox-confirm":
                    if entry.value != "yes":
                        set_notice("Cancelled")
                        return None
                    action, pid, revision = picker.carry
                    if action == dob.PUBLISH:
                        run_dashboard_command(
                            container, "container", dob.outbox_apply_argv(container, pid, revision)
                        )
                    elif action == dob.DELETE:
                        repo = repo_for(container, "container")
                        if repo is None:
                            set_notice(f"'{container}' is gone")
                        elif run_quiet_cli(repo, dob.outbox_drop_argv(container, pid, revision)):
                            client.refresh()
                    return None
                return None

            def submit_snapshot_picker(picker: Picker, entry: PickerEntry) -> Overlay | None:
                """The `container-snapshot*` steps. Every change runs in the terminal.

                Not quietly: `run_cli_quiet` kills its child after 60 s, and an
                `incus snapshot` of a large container can outlast that.
                """
                container = picker.target
                if picker.purpose == "container-snapshots":
                    if entry.value == dact.CREATE_TIMESTAMP:
                        run_dashboard_command(
                            container, "container", dact.snapshot_create_argv(container, None)
                        )
                        return None
                    if entry.value == dact.CREATE_NAMED:
                        return dact.snapshot_tag_prompt(container)
                    tag = dact.snapshot_tag(entry.value)
                    if tag is None:
                        return None
                    # Each verb is gated on its own argv: over SSH an allowlist
                    # may permit a restore and not a delete, or the reverse.
                    actions = dact.snapshot_action_picker(
                        container,
                        tag,
                        can_restore=permitted(
                            dact.snapshot_restore_argv(container, tag),
                            ssh_policy,
                            over_ssh=over_ssh,
                        ),
                        can_delete=permitted(
                            dact.snapshot_delete_argv(container, tag),
                            ssh_policy,
                            over_ssh=over_ssh,
                        ),
                    )
                    if not actions.entries:
                        set_notice(f"No change to snapshot {tag} is permitted here")
                        return None
                    return actions
                if picker.purpose == "container-snapshot-action":
                    if entry.value not in (dact.RESTORE, dact.DELETE):
                        return None
                    return dact.snapshot_confirm_picker(container, entry.value, picker.carry[0])
                if picker.purpose == "container-snapshot-confirm":
                    if entry.value != "yes":
                        set_notice("Cancelled")
                        return None
                    action, tag = picker.carry
                    if action == dact.RESTORE:
                        build = dact.snapshot_restore_argv
                    elif action == dact.DELETE:
                        build = dact.snapshot_delete_argv
                    else:
                        return None  # never default to a destructive verb
                    # Foreground, like the create: an incus restore can outlast
                    # the 60 s cutoff of the quiet runner.
                    run_dashboard_command(container, "container", build(container, tag))
                    return None
                return None

            def repo_for(
                target: str, kind: Literal["repo", "container"] = "repo"
            ) -> RepoTarget | None:
                """The listed repo of a prefix, or of a container name with ``kind``."""
                group = target_group(groups, target, kind)
                return RepoTarget.of(group) if group is not None else None

            def run_quiet_cli(repo: RepoTarget, argv: list[str]) -> bool:
                """Run one short `jailbee` change off-screen (an account or a mount).

                The outcome is reported as a notice.

                Quiet rather than `foreground`: the command asks nothing, so
                handing it the terminal would only blank the dashboard. A
                refusal — typically an agent still running, which the CLI's
                own message answers with `--force` — stays up long enough to
                read. There is no automatic retry with `--force`.
                """
                full = dact.addressed(argv, repo.flags(), over_ssh=over_ssh)
                try:
                    check_dashboard_command(full, ssh_policy, over_ssh=over_ssh)
                except RouteError as exc:
                    set_notice(str(exc), seconds=_FAILURE_NOTICE_SECONDS)
                    return False
                result = da.run_cli_quiet(full, cwd=repo.cwd())
                set_notice(
                    result.message,
                    seconds=_NOTICE_SECONDS if result.ok else _FAILURE_NOTICE_SECONDS,
                )
                client.refresh()  # a group or mount change shows in the next gather
                return result.ok

            def load_listing(
                repo: RepoTarget, listing_argv: list[str], what: str
            ) -> tuple[da.AccountRow, ...] | None:
                """Rows of one `jailbee account … ls`, or None after noticing why not."""
                argv = [*listing_argv, *(repo.flags() if not over_ssh else [])]
                try:
                    check_dashboard_command(argv, ssh_policy, over_ssh=over_ssh)
                    result = da.run_cli_quiet(argv, cwd=repo.cwd())
                    if not result.ok:
                        raise da.AccountLoadError(result.message)
                    return da.parse_account_rows(result.stdout)
                except (RouteError, da.AccountLoadError) as exc:
                    set_notice(f"could not list {what}: {exc}", seconds=_FAILURE_NOTICE_SECONDS)
                    return None

            def load_group_rows(repo: RepoTarget) -> tuple[da.AccountRow, ...] | None:
                """The host's credential groups, or None after noticing why not."""
                return load_listing(repo, da.group_ls_argv(), "credential groups")

            def group_picker(
                purpose: Literal["repo-group", "container-group"],
                target: str,
                rows: Sequence[da.AccountRow],
            ) -> Picker:
                """The groups to choose from, plus the choices that are not a group.

                Those are always offered, so a host with no group yet can
                still opt out or create the first one. A legacy group named
                like a reserved word (`none`) is left out: choosing it would
                send the very word that means "no group".
                """
                owner = "repo" if purpose == "repo-group" else "container"
                fallback = (
                    PickerEntry("Use the host default", "__unset__")
                    if purpose == "repo-group"
                    else PickerEntry("Follow the repo's group", "__reset__")
                )
                entries = (
                    *(
                        PickerEntry(name, name)
                        for name in da.group_names(rows)
                        if name not in RESERVED_GROUP_NAMES
                    ),
                    PickerEntry(f"none (this {owner} keeps its own login)", "none"),
                    fallback,
                    PickerEntry("New group…", "__new__"),
                )
                return Picker(purpose, f"Credential group — {target}", entries, target=target)

            def open_group_picker(
                purpose: Literal["repo-group", "container-group"], target: str
            ) -> Picker | None:
                """List the groups for ``target``'s repo and offer them, or notice why not."""
                repo = repo_for(target, prompt_target_kind(purpose))
                if repo is None:
                    set_notice(f"'{target}' is no longer listed")
                    return None
                rows = load_group_rows(repo)
                return group_picker(purpose, target, rows) if rows is not None else None

            def change_group(overlay: TextPrompt | Picker, argv: list[str]) -> None:
                """Re-resolve the overlay's target (it may have vanished) and run one change."""
                target = overlay.target
                repo = repo_for(target, prompt_target_kind(overlay.purpose))
                if repo is None:
                    set_notice(f"'{target}' is gone")
                    return
                run_quiet_cli(repo, argv)

            def accounts_target() -> str | None:
                """The repo prefix the Accounts panel runs its `jailbee account …` in.

                The listing is host-wide, so any real repo would answer it; the
                selected row's repo is preferred because that is the config a
                user expects `--config` to name. Falls back to the first repo
                with a root, so `A` also works from an orphan row.
                """
                prefix = fold_target(groups, selected)
                if prefix is not None and repo_for(prefix) is not None:
                    return prefix
                return next((g.prefix for g in groups if RepoTarget.of(g) is not None), None)

            def load_accounts(prefix: str) -> da.AccountsState | None:
                """The Accounts panel for ``prefix``'s repo."""
                repo = repo_for(prefix)
                if repo is None:
                    set_notice(f"'{prefix}' is gone", seconds=_FAILURE_NOTICE_SECONDS)
                    return None
                rows = load_listing(repo, da.account_ls_argv(), "accounts")
                if rows is None:
                    return None
                return da.AccountsState(rows, 0, prefix)

            def open_accounts() -> da.AccountsState | None:
                """Open the Accounts panel, or notice why not."""
                prefix = accounts_target()
                if prefix is None:
                    set_notice("No repo to address account commands at")
                    return None
                return load_accounts(prefix)

            def account_actions_picker(state: da.AccountsState) -> Overlay:
                """What can be done with the highlighted row, or the panel with a notice."""
                row = da.selected_account(state)
                actions = da.account_actions(row, state.rows) if row is not None else ()
                if row is None or not actions:
                    set_notice("No actions for this row")
                    return state
                title = (
                    f"Login {row.account} ({row.agent})"
                    if row.state == "parked"
                    else f"Group {row.group} ({row.agent})"
                )
                return Picker(
                    "acct-action",
                    title,
                    tuple(PickerEntry(label, action) for label, action in actions),
                    target=state.prefix,
                    carry=(row.agent, row.group or "", row.account or ""),
                    back=state,
                )

            def run_account_change(state: da.AccountsState, argv: list[str]) -> Overlay | None:
                """Run one change from the panel; a change that worked closes it.

                Done is done: the CLI's own message stays up as the notice, so
                there is nothing left to Esc out of. The repo is re-resolved
                first — it may have vanished while a picker was open. A refused
                change keeps the listing up under its notice, for a retry.
                """
                repo = repo_for(state.prefix)
                if repo is None:
                    set_notice(f"'{state.prefix}' is gone", seconds=_FAILURE_NOTICE_SECONDS)
                    return None
                return None if run_quiet_cli(repo, argv) else state

            def submit_account_picker(
                picker: Picker, entry: PickerEntry, state: da.AccountsState
            ) -> Overlay | None:
                """The `acct-*` steps: a cancel or a refusal lands back on the panel ``state``."""
                if picker.purpose == "acct-action":
                    agent, group, ref = picker.carry
                    if entry.value == "use":
                        logins = da.parked_for(state.rows, agent)
                        return Picker(
                            "acct-use",
                            "Use which login?",
                            tuple(
                                PickerEntry(r.account, r.account)
                                for r in logins
                                if r.account is not None
                            ),
                            target=picker.target,
                            carry=picker.carry,
                            back=state,
                        )
                    if entry.value == "park":
                        return run_account_change(state, da.park_argv(agent, group or None))
                    if entry.value == "use-in":
                        return Picker(
                            "acct-use-in",
                            "Use in which group?",
                            tuple(PickerEntry(name, name) for name in da.group_names(state.rows)),
                            target=picker.target,
                            carry=(agent, "", ref),
                            back=state,
                        )
                    if entry.value in ("delete", "group-rm"):
                        question = (
                            f"Really delete login {ref}?"
                            if entry.value == "delete"
                            else f"Really remove group {group}?"
                        )
                        # "No" first: a stray Enter must not delete anything.
                        return Picker(
                            "acct-confirm",
                            question,
                            (PickerEntry("No", "no"), PickerEntry("Yes, delete", "yes")),
                            target=picker.target,
                            carry=(entry.value, agent, group, ref),
                            back=state,
                        )
                    return state
                if picker.purpose == "acct-use":
                    agent, group, _ref = picker.carry
                    return run_account_change(state, da.use_argv(agent, group or None, entry.value))
                if picker.purpose == "acct-use-in":
                    agent, _group, ref = picker.carry
                    return run_account_change(state, da.use_argv(agent, entry.value, ref))
                if picker.purpose == "acct-confirm":
                    if entry.value != "yes":
                        return state
                    action, agent, group, ref = picker.carry
                    return run_account_change(
                        state,
                        da.rm_login_argv(agent, ref)
                        if action == "delete"
                        else da.group_rm_argv(group),
                    )
                return state

            def submit_prompt(prompt: TextPrompt) -> Overlay | None:
                """Act on a confirmed answer; return the overlay to show next.

                None closes the overlay. Every purpose returns explicitly: the
                caller shows exactly what this returns, with no fallback.
                """
                answer = prompt.text.strip()
                if prompt.purpose == "new-pr":
                    number = parse_pr_number(answer)
                    assert number is not None  # validate_answer guaranteed it

                    def pr_argv(repo: RepoTarget) -> list[str]:
                        if over_ssh:
                            # Remote sessions address their selected repo by
                            # cwd, not by an explicit host config path.
                            return ["jailbee", "new", "--background", "--pr", str(number)]
                        return new_pr_container_argv(repo, number)

                    run_new_container(prompt.target, f"PR #{number}", pr_argv)
                    return None
                if prompt.purpose == "new-branch":
                    return TextPrompt(
                        "new-base",
                        prompt.title,
                        "Base branch",
                        text=prompt.carry[0],
                        target=prompt.target,
                        carry=(answer,),
                        suggestions=host_branches(
                            repo.repo_root
                            if (repo := target_group(groups, prompt.target, "repo"))
                            else None
                        ),
                    )
                if prompt.purpose == "new-base":
                    branch = prompt.carry[0]

                    def branch_argv(repo: RepoTarget) -> list[str]:
                        if over_ssh:
                            return ["jailbee", "new", "--background", "--", branch, answer]
                        return new_container_argv(repo, branch, answer)

                    run_new_container(prompt.target, branch, branch_argv)
                    return None
                if prompt.purpose == "container-retarget":
                    run_dashboard_command(
                        prompt.target, "container", dact.retarget_argv(prompt.target, answer)
                    )
                    return None
                if prompt.purpose == "egress-add":
                    # begin_egress_add always sets it
                    assert isinstance(prompt.back, EgressState)
                    return mutate_egress(prompt.back, "add", answer)
                if prompt.purpose == "repo-group-name":
                    change_group(prompt, da.repo_group_set_argv(answer))
                    return None
                if prompt.purpose == "container-group-name":
                    change_group(prompt, da.container_group_use_argv(answer, prompt.target))
                    return None
                if prompt.purpose == "container-snapshot-tag":
                    run_dashboard_command(
                        prompt.target, "container", dact.snapshot_create_argv(prompt.target, answer)
                    )
                    return None
                if prompt.purpose == "acct-group-new":
                    # asked only from the Accounts panel, which it returns to
                    assert isinstance(prompt.back, da.AccountsState)
                    return run_account_change(prompt.back, da.group_create_argv(answer))
                return None

            def submit_picker(picker: Picker, entry: PickerEntry) -> Overlay | None:
                """Act on a chosen entry; return the overlay to show next.

                None closes the overlay, as in :func:`submit_prompt`.
                """
                if picker.purpose == "repo-group":
                    if entry.value == "__new__":
                        return TextPrompt(
                            "repo-group-name", picker.title, "Group name", target=picker.target
                        )
                    change_group(
                        picker,
                        da.repo_group_unset_argv()
                        if entry.value == "__unset__"
                        else da.repo_group_set_argv(entry.value),
                    )
                    return None
                if picker.purpose == "container-group":
                    if entry.value == "__new__":
                        return TextPrompt(
                            "container-group-name",
                            picker.title,
                            "Group name",
                            target=picker.target,
                        )
                    change_group(
                        picker,
                        da.container_group_reset_argv(picker.target)
                        if entry.value == "__reset__"
                        else da.container_group_use_argv(entry.value, picker.target),
                    )
                    return None
                if picker.purpose == "repo-apply":
                    run_dashboard_command(
                        picker.target,
                        "repo",
                        dact.apply_argv(no_restart=entry.value == dact.APPLY_NO_RESTART),
                    )
                    return None
                if picker.purpose == "container-autostart-cancel":
                    if entry.value == "yes":
                        run_dashboard_command(
                            picker.target, "container", dact.autostart_cancel_argv(picker.target)
                        )
                    else:
                        set_notice("Cancelled")
                    return None
                if picker.purpose.startswith("container-snapshot"):
                    return submit_snapshot_picker(picker, entry)
                if picker.purpose.startswith("container-outbox"):
                    return submit_outbox_picker(picker, entry)
                if picker.purpose in ("container-mount-add", "container-mount-remove"):
                    build = (
                        dact.unmount_argv
                        if picker.purpose == "container-mount-remove"
                        else dact.mount_argv
                    )
                    repo = repo_for(picker.target, "container")
                    if repo is None:
                        set_notice(f"'{picker.target}' is gone")
                    else:
                        run_quiet_cli(repo, build(entry.value, picker.target))
                    return None
                if picker.purpose.startswith("acct-"):
                    # every account picker is opened from the Accounts panel
                    assert isinstance(picker.back, da.AccountsState)
                    return submit_account_picker(picker, entry, picker.back)
                return picker.back

            def edit_config(*, global_layer: bool) -> None:
                """Hand the terminal to `jailbee config edit` for the selected repo.

                A foreground dispatch, not a detached spawn: it is a full-screen
                TUI and needs the real terminal, exactly like `shell` and `tmux`.

                The global layer needs a repo too — `config_edit.layers.validate`
                loads the repo config even for a global-layer edit, because a
                global change only means anything through its effect on some
                repo's merged config.
                """
                prefix = fold_target(groups, selected) or ""
                note = config_edit_reject_note_for_prefix(groups, prefix, global_layer=global_layer)
                if note is not None:
                    set_notice(note)
                    return
                group = next(g for g in groups if g.prefix == prefix)
                repo = RepoTarget.of(group)
                assert repo is not None  # the note rejects a rootless group
                argv = ["jailbee", "config", "edit", *repo.flags()]
                if global_layer:
                    argv.append("--global")
                try:
                    rc = foreground(
                        lambda: subprocess.run(argv, check=False, cwd=repo.cwd()).returncode
                    )
                except OSError:
                    _report_vanished_repo(repo)
                    return
                if rc != 0:
                    set_notice(f"'jailbee config edit' exited {rc}")
                client.refresh()  # config may have changed under every row

            def run_command(command: CommandState) -> None:
                """Authorize and run the edited argv in the selected repo."""
                name = container_of(selected)
                if selected is None:
                    set_notice("Select a repo or a container first")
                    return
                group = (
                    _find_group(groups, name)
                    if name is not None
                    else next((g for g in groups if g.prefix == selected.key), None)
                )
                if group is None:
                    set_notice("Selected repo is no longer listed")
                    return
                repo = RepoTarget.of(group)
                if repo is None:
                    set_notice(
                        view_only_note(groups, name)
                        or f"No repo found for '{group.prefix}' — this row is view-only"
                    )
                    return
                try:
                    argv = command_argv(command.text, name)
                    if not over_ssh:
                        argv = insert_options_before_separator(argv, repo.flags())
                    check_dashboard_command(argv, ssh_policy, over_ssh=over_ssh)
                except (ValueError, RouteError) as exc:
                    set_notice(str(exc))
                    return
                try:

                    def execute_command() -> int:
                        result = subprocess.run(["jailbee", *argv], cwd=repo.cwd(), check=False)
                        try:
                            typed, _leaf = ssh_router.command_leaf(argv)
                        except RouteError:
                            typed = ""
                        if command_needs_pause(typed):
                            _wait_for_return()
                        return result.returncode

                    rc = foreground(execute_command)
                except OSError:
                    _report_vanished_repo(repo)
                    return
                if rc != 0:
                    set_notice(f"'jailbee {' '.join(argv)}' exited {rc}")
                client.refresh()

            # Before the loop too: the closures above read `all_groups`.
            snapshot = client.latest()
            assert snapshot is not None  # `wait_first_snapshot` returned
            all_groups: list[RepoGroup] = present(snapshot.groups, cwd_root, scope)
            git_enabled = snapshot.git_enabled
            groups: list[RepoGroup] = visible_repo_groups(
                all_groups, show_empty_repos=show_empty_repos, hidden_repos=hidden_repos
            )
            shown_columns = nonempty_columns(groups, now=now(), enabled=enabled, folded=folded)

            def clamped(offset: int) -> int:
                return clamp_column_offset(
                    groups,
                    offset,
                    now=now(),
                    enabled=enabled,
                    folded=folded,
                    column_widths=column_widths,
                    shown_columns=shown_columns,
                    width=console.width,
                )

            while True:
                jobs.poll()
                snapshot = client.latest()
                assert snapshot is not None  # `wait_first_snapshot` returned
                all_groups = present(snapshot.groups, cwd_root, scope)
                git_enabled = snapshot.git_enabled
                groups = visible_repo_groups(
                    all_groups, show_empty_repos=show_empty_repos, hidden_repos=hidden_repos
                )
                rows = selectable_rows(groups, folded)
                if (
                    isinstance(overlay, MenuState)
                    and Row("container", overlay.container) not in rows
                ):
                    # The menu's container vanished under it (destroyed, or its
                    # repo dropped out of the registry) — close rather than
                    # dispatch at a name that is no longer there.
                    set_notice(f"'{overlay.container}' is gone — menu closed")
                    overlay = None
                if isinstance(overlay, RepoMenuState) and Row("repo", overlay.repo) not in rows:
                    set_notice(f"'{overlay.repo}' is gone — menu closed")
                    overlay = None
                if isinstance(overlay, EgressState) and (
                    not any(g.prefix == overlay.prefix for g in groups)
                    or (
                        overlay.container is not None
                        and not any(
                            c.name == overlay.container
                            for g in groups
                            if g.prefix == overlay.prefix
                            for c in g.containers
                        )
                    )
                ):
                    set_notice("Egress target is gone — panel closed")
                    overlay = None
                if isinstance(overlay, da.AccountsState) and repo_for(overlay.prefix) is None:
                    # The repo its account commands run in is gone; a reopen
                    # (`A`) picks another one.
                    set_notice(f"'{overlay.prefix}' is gone — accounts closed")
                    overlay = None
                if (
                    isinstance(overlay, (TextPrompt, Picker))
                    and overlay.target
                    and target_group(groups, overlay.target, prompt_target_kind(overlay.purpose))
                    is None
                ):
                    # The prompt's repo or container vanished while it was
                    # open — close rather than ask a question about nothing.
                    set_notice(f"'{overlay.target}' is gone — prompt closed")
                    overlay = None
                # The Egress panel on screen, itself or behind its question.
                egress_panel = (
                    overlay
                    if isinstance(overlay, EgressState)
                    else overlay.back
                    if isinstance(overlay, (TextPrompt, Picker))
                    and isinstance(overlay.back, EgressState)
                    else None
                )
                if egress_panel is None:
                    # The menu an Egress panel's Esc returns to outlives the
                    # panel only while it (or its question) is open — however
                    # it closed: a vanished target, a failed change, `q`.
                    egress_parent = None
                if isinstance(overlay, MenuState):
                    selected = Row("container", overlay.container)  # pinned while the menu is open
                elif isinstance(overlay, RepoMenuState):
                    selected = Row("repo", overlay.repo)
                elif egress_panel is not None:
                    # A question asked from the Egress panel keeps the panel's
                    # row, so the cursor does not jump to the repo header.
                    selected = (
                        Row("container", egress_panel.container)
                        if egress_panel.container is not None
                        else Row("repo", egress_panel.prefix)
                    )
                elif isinstance(overlay, da.AccountsState) or (
                    isinstance(overlay, (TextPrompt, Picker))
                    and isinstance(overlay.back, da.AccountsState)
                ):
                    # Host-wide: its questions target a repo only to run the
                    # CLI there, so the cursor stays where `A` was pressed
                    # instead of jumping to that repo's header.
                    selected = reconcile_selection(rows, selected, sel_index)
                elif (
                    isinstance(overlay, (TextPrompt, Picker))
                    and not overlay.purpose.startswith("new-")
                    and target_group(groups, overlay.target, prompt_target_kind(overlay.purpose))
                    is not None
                ):
                    # A question about one repo or container keeps its row,
                    # like its menu does. `n`'s questions are left out: they
                    # ask about the highlighted row's repo, and pinning its
                    # header would strand the cursor there after Esc.
                    selected = Row(prompt_target_kind(overlay.purpose), overlay.target)
                else:
                    selected = reconcile_selection(rows, selected, sel_index)
                if selected in rows:
                    sel_index = rows.index(selected)
                if notice is not None and time.monotonic() >= notice_until:
                    notice = None
                # Only on change: an OSC 2 write on every frame makes some
                # terminals redraw their title bar continuously.
                title = terminal_title(groups, selected)
                if title != last_title:
                    set_terminal_title(title, stream=sys.stdout)
                    last_title = title
                tracking = tracking_notices([c for g in all_groups for c in g.containers])
                tracking.extend(dashboard_group_notices(all_groups))
                column_offset = clamped(column_offset)
                live.update(
                    render(
                        groups,
                        selected,
                        now=now(),
                        git_enabled=git_enabled,
                        enabled=enabled,
                        overlay=overlay,
                        notice=notice
                        or client.status()
                        or "; ".join(jobs.active())
                        or ("; ".join(tracking) if tracking else None),
                        folded=folded,
                        hidden_by_preferences=bool(all_groups) and not groups,
                        height=console.height,
                        show_details=show_details,
                        column_widths=column_widths,
                        column_offset=column_offset,
                        shown_columns=shown_columns,
                    ),
                    refresh=True,
                )
                try:
                    ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                    if not ready:
                        continue
                    data = os.read(fd, _KEY_READ_BYTES)
                except KeyboardInterrupt:
                    # cbreak mode leaves ISIG on, so on a real terminal Ctrl-C
                    # arrives as SIGINT here, never as a b"\x03" byte. Turn it
                    # into that byte so the key handling below is the one place
                    # that decides what Ctrl-C means: a text input (prompt,
                    # command line) cancels just itself, anything else quits.
                    data = b"\x03"
                if isinstance(overlay, CommandState):
                    if data in (b"\x1b", b"\x03", b""):
                        overlay = None
                    elif data in (b"\r", b"\n"):
                        command = overlay
                        overlay = None
                        run_command(command)
                    else:
                        selected_group = (
                            _find_group(groups, container_of(selected))
                            if container_of(selected) is not None
                            else next(
                                (
                                    group
                                    for group in groups
                                    if selected and group.prefix == selected.key
                                ),
                                None,
                            )
                        )
                        allowed_paths: frozenset[str] | None = None
                        if over_ssh:
                            if ssh_policy is None:
                                allowed_paths = frozenset()
                            else:
                                allowed_paths = ssh_router.allowed_command_paths(
                                    ssh_policy.commands,
                                    restrict_host=ssh_policy.restrict_host,
                                    scope=scope,
                                    unlocks=ssh_router.RemoteUnlocks.of(ssh_policy),
                                )
                        candidates = completion_candidates(
                            overlay.text,
                            tuple(c.name for c in selected_group.containers)
                            if selected_group is not None
                            else (),
                            allowed_paths,
                            restrict_host=bool(
                                over_ssh
                                and ssh_policy is not None
                                and host_restricted(ssh_policy.restrict_host)
                            ),
                            unlocks=ssh_router.RemoteUnlocks.of(ssh_policy if over_ssh else None),
                        )
                        overlay = edit_command(replace(overlay, suggestions=candidates), data)
                    continue
                if isinstance(overlay, TextPrompt):
                    # Raw bytes, like the command line: every key is text
                    # here, so none of the table's shortcuts may fire.
                    prompt, outcome = handle_prompt_key(overlay, data)
                    if outcome == "cancel":
                        # Esc/Ctrl-C answer the prompt, never the dashboard.
                        overlay = prompt.back
                        set_notice(
                            "Egress change cancelled"
                            if prompt.purpose == "egress-add"
                            else "Cancelled"
                        )
                    elif outcome == "submit":
                        overlay = submit_prompt(prompt)
                    else:
                        overlay = prompt
                    continue
                if isinstance(overlay, Picker) and (
                    data == b"\x03" or parse_key(data) in ("cancel", "quit")
                ):
                    # A picker is one step of a question flow, like the prompt
                    # it can lead to: Ctrl-C, Esc and `q` all cancel the step
                    # — a nested picker returns to the panel it was opened
                    # from — never the dashboard. EOF (b"") still quits — a
                    # closed stdin must not spin here.
                    overlay = overlay.back
                    set_notice("Cancelled")
                    continue
                key = parse_key(data)
                if key == "interrupt":
                    break
                if overlay is not None:
                    if key == "quit":
                        overlay = None
                    elif key == "cancel":
                        if isinstance(overlay, (MenuState, RepoMenuState)):
                            overlay = back_menu(overlay)
                        elif isinstance(overlay, EgressState):
                            overlay = egress_parent
                            egress_parent = None
                        else:
                            overlay = None
                    elif key == "help":
                        # One slot, so help replaces the menu rather than
                        # stacking on it — and toggles itself shut.
                        overlay = None if overlay == "help" else "help"
                    elif key == "settings":
                        # Mirrors help's own toggle, one line up: F2/S
                        # switches to settings from any other overlay (the
                        # action menu, help) instead of just closing it, and
                        # toggles itself shut when settings is already open.
                        if isinstance(overlay, SettingsState):
                            overlay = None
                        else:
                            overlay = open_settings_overlay()
                    elif isinstance(overlay, SettingsState):
                        if key in ("up", "down"):
                            overlay = move_settings(overlay, -1 if key == "up" else 1)
                        elif key == "tab":
                            overlay = switch_tab(overlay)
                        elif key == "space":
                            overlay = toggle_current(overlay)
                            enabled = enabled_names(overlay)
                            folded = overlay.folded
                            show_empty_repos = overlay.show_empty_repos
                            hidden_repos = overlay.hidden_repos
                            groups = visible_repo_groups(
                                all_groups,
                                show_empty_repos=show_empty_repos,
                                hidden_repos=hidden_repos,
                            )
                            shown_columns = nonempty_columns(
                                groups, now=now(), enabled=enabled, folded=folded
                            )
                            column_widths = None
                            column_offset = 0
                            persist_view_state(
                                ViewState(
                                    columns=enabled,
                                    folded=folded,
                                    show_empty_repos=show_empty_repos,
                                    hidden_repos=hidden_repos,
                                    show_details=show_details,
                                )
                            )
                    elif isinstance(overlay, EgressState):
                        if key in ("up", "down"):
                            overlay = move_egress(overlay, -1 if key == "up" else 1)
                        elif data == b"a":
                            overlay = begin_egress_add(overlay)
                        elif data == b"r":
                            overlay = mutate_egress(overlay, "rm")
                    elif isinstance(overlay, da.AccountsState):
                        if key in ("up", "down"):
                            overlay = da.move_accounts(overlay, -1 if key == "up" else 1)
                        elif key == "enter":
                            overlay = account_actions_picker(overlay)
                        elif data == b"n":
                            overlay = TextPrompt(
                                "acct-group-new",
                                "New credential group",
                                "Group name",
                                target=overlay.prefix,
                                back=overlay,
                            )
                    elif isinstance(overlay, Picker):
                        if key in ("up", "down"):
                            overlay = move_picker(overlay, -1 if key == "up" else 1)
                        elif key == "enter":
                            done = overlay
                            chosen = picked(done)
                            overlay = done.back
                            if chosen is not None:
                                overlay = submit_picker(done, chosen)
                    elif isinstance(overlay, (MenuState, RepoMenuState)):
                        if key in ("up", "down"):
                            overlay = move_menu(overlay, -1 if key == "up" else 1)
                        # An entry's own key is Enter on that entry, so both
                        # take this one path to its group or verb.
                        elif (
                            chosen_menu := overlay if key == "enter" else hotkey_menu(overlay, data)
                        ) is not None:
                            next_menu, verb = enter_menu(chosen_menu)
                            if verb is None:
                                overlay = next_menu
                                continue
                            if isinstance(overlay, RepoMenuState):
                                target = overlay.repo
                                repo_parent = next_menu
                                overlay = None
                                if verb == "new":
                                    overlay = start_new_container()
                                elif verb == "new-pr":
                                    overlay = start_new_container(from_pr=True)
                                elif verb == "credential-group":
                                    overlay = open_group_picker("repo-group", target)
                                elif verb == "accounts":
                                    overlay = load_accounts(target)
                                elif verb == dact.REPO_APPLY:
                                    overlay = dact.apply_picker(target)
                                elif verb == dact.REPO_DOCTOR:
                                    run_dashboard_command(
                                        target, "repo", dact.doctor_argv(), style="paged"
                                    )
                                elif verb == dact.REPO_DISK_USAGE:
                                    run_dashboard_command(target, "repo", dact.disk_usage_argv())
                                elif verb == dact.REPO_PRUNE:
                                    run_dashboard_command(target, "repo", dact.prune_argv())
                                elif verb == "fold":
                                    folded = toggle_folded(folded, target)
                                    shown_columns = nonempty_columns(
                                        groups, now=now(), enabled=enabled, folded=folded
                                    )
                                    persist_view_state(
                                        ViewState(
                                            columns=enabled,
                                            folded=folded,
                                            show_empty_repos=show_empty_repos,
                                            hidden_repos=hidden_repos,
                                            show_details=show_details,
                                        )
                                    )
                                elif verb == "net egress ls":
                                    egress_parent = repo_parent
                                    overlay = open_egress(target, None)
                            else:
                                target = overlay.container
                                assert isinstance(next_menu, MenuState)
                                container_parent = next_menu
                                overlay = None
                                if verb == "net egress ls":
                                    group = _find_group(groups, target)
                                    egress_parent = container_parent
                                    overlay = open_egress(group.prefix, target) if group else None
                                elif verb == "credential-group":
                                    # Handled here: it is not a CLI verb to dispatch.
                                    overlay = open_group_picker("container-group", target)
                                elif verb in dact.CONTAINER_VERBS:
                                    overlay = open_container_entry(target, verb)
                                elif verb == "outbox browse":
                                    # Qt hands the terminal to the browser; here
                                    # it is the dashboard's own picker panels.
                                    overlay = open_outbox(target)
                                elif verb == "git retarget":
                                    overlay = open_retarget(target)
                                else:
                                    dispatch(target, verb)
                    continue
                if key == "quit":
                    break
                if key in ("up", "down"):
                    selected = move_selection(rows, selected, -1 if key == "up" else 1)
                    if selected in rows:
                        sel_index = rows.index(selected)
                elif key in ("scroll-left", "scroll-right"):
                    # Clamp before stepping too: a resize since the last frame
                    # may have reduced the scrollable range.
                    column_offset = clamped(
                        clamped(column_offset) + (1 if key == "scroll-right" else -1)
                    )
                elif key == "enter":
                    if selected is not None and selected.kind == "repo":
                        overlay = open_repo_menu(
                            groups,
                            selected.key,
                            folded,
                            ssh_policy=ssh_policy,
                            over_ssh=over_ssh,
                        )
                    else:
                        container = container_of(selected)
                        overlay = open_menu(
                            groups,
                            container,
                            remote=remote,
                            ssh_policy=ssh_policy,
                            over_ssh=over_ssh,
                        )
                        if overlay is None and container is not None:
                            note = view_only_note(groups, container)
                            set_notice(note or f"No actions available for '{container}'")
                elif key == "help":
                    overlay = "help"
                elif key == "command":
                    overlay = CommandState("")
                elif key == "settings":
                    overlay = open_settings_overlay()
                elif key.startswith("action:"):
                    container = container_of(selected)
                    verb = quick_verb(
                        groups,
                        container,
                        key,
                        remote=remote,
                        ssh_policy=ssh_policy,
                        over_ssh=over_ssh,
                    )
                    if verb is not None and container is not None:
                        dispatch(container, verb)
                    else:
                        set_notice(
                            quick_reject_note(
                                groups,
                                container,
                                key,
                                remote=remote,
                                ssh_policy=ssh_policy,
                                over_ssh=over_ssh,
                            )
                        )
                elif key == "new":
                    overlay = start_new_container()
                elif key == "accounts":
                    overlay = open_accounts()
                elif key in ("config-edit", "config-edit-global") and remote:
                    set_notice(REMOTE_CONFIG_EDIT_NOTE)
                elif key in ("config-edit", "config-edit-global"):
                    edit_config(global_layer=key == "config-edit-global")
                elif key == "optimize":
                    shown_columns = nonempty_columns(
                        groups, now=now(), enabled=enabled, folded=folded
                    )
                    column_widths = optimize_column_widths(
                        groups, now=now(), enabled=enabled, folded=folded
                    )
                    column_offset = 0
                elif key == "refresh":
                    client.refresh()
                elif key == "details":
                    show_details = not show_details
                    persist_view_state(
                        ViewState(
                            columns=enabled,
                            folded=folded,
                            show_empty_repos=show_empty_repos,
                            hidden_repos=hidden_repos,
                            show_details=show_details,
                        )
                    )
                elif key == "space":
                    prefix = fold_target(groups, selected)
                    if prefix is not None:
                        folded = toggle_folded(folded, prefix)
                        shown_columns = nonempty_columns(
                            groups, now=now(), enabled=enabled, folded=folded
                        )
                        # The container rows just vanished under the cursor;
                        # park it on the header rather than letting
                        # reconcile_selection pick a neighbour repo.
                        selected = Row("repo", prefix)
                        persist_view_state(
                            ViewState(
                                columns=enabled,
                                folded=folded,
                                show_empty_repos=show_empty_repos,
                                hidden_repos=hidden_repos,
                                show_details=show_details,
                            )
                        )
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
    return 0
