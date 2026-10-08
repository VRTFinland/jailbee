"""The terminal dashboard's state and behaviour, independent of how it is drawn.

One :class:`DashboardSession` per open dashboard: the snapshot it shows, the
selection, the open overlay, and everything a key does. It never touches the
terminal itself — a frontend implements :class:`Terminal` (its width and how
to hand the terminal to a child) and renders :meth:`DashboardSession.view`.
Container state is read ONLY through the shared state service.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

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
    removable_entry,
)
from jailbee.dashboard.egress_data import load_egress_rows
from jailbee.dashboard.hit import Hit
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
    global_config_or_defaults,
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
    parse_pr_number,
    validate_answer,
)
from jailbee.dashboard.settings import (
    SettingsState,
    Tab,
    enabled_names,
    open_settings,
    toggle_setting,
)
from jailbee.dashboard.tui.frame import DashboardView
from jailbee.dashboard.tui.keys import quick_reject_note, quick_verb
from jailbee.dashboard.tui.menu_state import (
    MenuState,
    RepoMenuState,
    open_menu,
    open_repo_menu,
)
from jailbee.dashboard.tui.overlay import (
    CommandState,
    Overlay,
    _egress_panel,
    _with_egress_panel,
)
from jailbee.dashboard.tui.terminal import terminal_title
from jailbee.dashboard.visibility import visible_repo_groups
from jailbee.db.view_prefs import FRONTEND_TUI, ViewState, save_view_state
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
    from sqlalchemy.engine import Engine

    from jailbee.egress_scope import EntryRow
    from jailbee.incus import Incus
    from jailbee.state_service.client import StateClient


log = logging.getLogger(__name__)

NOTICE_SECONDS = 2.5  # how long a transient subtitle message stays up
FAILURE_NOTICE_SECONDS = 8.0  # a refused account command's reason, long enough to read
STARTUP_NOTICE_SECONDS = 10.0  # a config-migration notice shown at launch

Outcome = Literal["quit", "toggle-mouse"] | None

# Hits whose second click of a double-click means Enter there. Any other hit
# already acted on the first click (a menu entry ran, a fold toggled).
DOUBLE_CLICK_KINDS: frozenset[str] = frozenset({"row", "repo"})

# With a native overlay focused, only these keys are the dashboard's own; every
# other key belongs to the overlay (see `DashboardApp.on_key`).
OVERLAY_GLOBAL_TOKENS: frozenset[str] = frozenset({"quit", "help", "settings", "interrupt"})


def _now() -> datetime:
    return datetime.now().astimezone()


def open_state_client(cwd_root: Path | None) -> StateClient:
    """A started `StateClient` for this dashboard (the tests' seam)."""
    from jailbee.state_service.client import StateClient

    client = StateClient(cwd_root)
    client.start()
    return client


def _interactive() -> bool:
    """Whether the dashboard has a terminal to draw on and read from.

    stderr too: Textual draws on it, so a redirected stderr would receive the
    whole screen.
    """
    return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()


class Terminal(Protocol):
    """What a session needs from the frontend drawing it."""

    @property
    def table_width(self) -> int:
        """Cells the table has, scrollbar excluded."""
        ...

    def hand_off(self, fn: Callable[[], int]) -> int:
        """Give the real terminal to ``fn`` (a child command), take it back, return its result."""
        ...


@dataclass(frozen=True)
class Startup:
    """What :func:`open_dashboard` resolved before any screen is taken."""

    engine: Engine
    client: StateClient
    view_state: ViewState
    notice: str | None
    mouse: bool = True


def open_dashboard(cwd_root: Path | None, *, scope: RemoteRepoScope | None = None) -> Startup | int:
    """Everything that happens before the dashboard takes the screen; an int is an exit code.

    The first snapshot is waited for here, so the first frame is already
    populated — and "the state service is unreachable" is said on the
    user's own terminal rather than by taking the screen only to hand it back.
    """
    if not _interactive():
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

    # Resolved once for the whole run — a live-refreshing dashboard must not
    # re-merge config on every frame.
    engine = get_engine()
    column_notices: list[str] = []
    view_state = seed_view_state(engine, FRONTEND_TUI, on_migration=column_notices.append)
    column_notice = "; ".join(column_notices) if column_notices else None
    config_notice = dashboard_config_migration_notice()
    if config_notice:
        column_notice = "; ".join(filter(None, (column_notice, config_notice)))

    client = open_state_client(cwd_root)
    try:
        with console.status("⏳ Surveying containers…"):
            client.wait_first_snapshot(STARTUP_TIMEOUT_SECONDS)
    except StateServiceUnavailable as exc:
        client.close()
        error(f"dashboard refresh failed: {exc}")
        return 1
    return Startup(
        engine, client, view_state, column_notice, mouse=global_config_or_defaults().dashboard.mouse
    )


class DashboardSession:
    """One open terminal dashboard (see the module docstring).

    ``remote`` is a remote SSH session, whose user may reach containers and
    the repos' git bridge but not the host itself. Everything here that runs
    on the host beyond that is withheld: the config editor (a config decides
    host mounts and the SSH policy itself), the pager (which can start a
    shell) and GUI app launches (which open on the host's display).
    """

    def __init__(
        self,
        startup: Startup,
        *,
        incus: Incus,
        cwd_root: Path | None,
        terminal: Terminal,
        remote: bool = False,
        over_ssh: bool = False,
        ssh_policy: RemoteSSHConfig | None = None,
        scope: RemoteRepoScope | None = None,
    ) -> None:
        self.incus = incus
        self.cwd_root = cwd_root
        self.terminal = terminal
        self.remote = remote
        self.over_ssh = over_ssh
        self.ssh_policy = ssh_policy
        self.scope = scope
        self.engine = startup.engine
        self.client = startup.client
        self.jobs = JobRunner()
        view_state = startup.view_state
        self.enabled: tuple[str, ...] | None = view_state.columns
        self.folded: frozenset[str] = view_state.folded
        self.show_empty_repos = view_state.show_empty_repos
        self.hidden_repos = view_state.hidden_repos
        self.show_details = view_state.show_details
        self.column_widths: dict[str, int] | None = None
        self.column_offset = 0
        self.selected: Row | None = None
        self.sel_index = 0
        self.overlay: Overlay | None = None
        self.egress_parent: MenuState | RepoMenuState | None = None
        self.notice: str | None = startup.notice
        self.notice_until = time.monotonic() + STARTUP_NOTICE_SECONDS if startup.notice else 0.0
        # The last listing per container, so a proposal step needs no second one.
        self.outbox_rows: dict[str, tuple[dob.ProposalRow, ...]] = {}
        snapshot = self.client.latest()
        assert snapshot is not None  # `wait_first_snapshot` returned
        self.all_groups: list[RepoGroup] = present(snapshot.groups, cwd_root, scope)
        self.git_enabled = snapshot.git_enabled
        self.groups: list[RepoGroup] = visible_repo_groups(
            self.all_groups, show_empty_repos=self.show_empty_repos, hidden_repos=self.hidden_repos
        )
        self.rows: list[Row] = selectable_rows(self.groups, self.folded)
        self.shown_columns = nonempty_columns(
            self.groups, now=_now(), enabled=self.enabled, folded=self.folded
        )

    def tick(self) -> None:
        """One refresh: finished jobs, the latest snapshot, overlays and cursor kept honest."""
        self.jobs.poll()
        snapshot = self.client.latest()
        assert snapshot is not None  # `wait_first_snapshot` returned
        self.all_groups = present(snapshot.groups, self.cwd_root, self.scope)
        self.git_enabled = snapshot.git_enabled
        self.groups = visible_repo_groups(
            self.all_groups, show_empty_repos=self.show_empty_repos, hidden_repos=self.hidden_repos
        )
        self.rows = selectable_rows(self.groups, self.folded)
        self._close_vanished_overlay()
        self._pin_selection()
        if self.notice is not None and time.monotonic() >= self.notice_until:
            self.notice = None
        self.column_offset = self._clamped(self.column_offset)

    def view(self, hover: Hit | None = None) -> DashboardView:
        """What to draw now. ``now`` is whole seconds, so an idle view compares equal."""
        tracking = tracking_notices([c for g in self.all_groups for c in g.containers])
        tracking.extend(dashboard_group_notices(self.all_groups))
        return DashboardView(
            groups=self.groups,
            selected=self.selected,
            now=_now().replace(microsecond=0),
            git_enabled=self.git_enabled,
            enabled=self.enabled,
            overlay=self.overlay,
            notice=self.notice
            or self.client.status()
            or "; ".join(self.jobs.active())
            or ("; ".join(tracking) if tracking else None),
            folded=self.folded,
            column_offset=self.column_offset,
            hidden_by_preferences=bool(self.all_groups) and not self.groups,
            show_details=self.show_details,
            column_widths=self.column_widths,
            shown_columns=self.shown_columns,
            hover=hover,
        )

    def title(self) -> str:
        """The terminal window title for the current selection."""
        return terminal_title(self.groups, self.selected)

    def save_view(self) -> None:
        """Persist the current view preferences (see :meth:`persist_view_state`)."""
        self.persist_view_state(
            ViewState(
                columns=self.enabled,
                folded=self.folded,
                show_empty_repos=self.show_empty_repos,
                hidden_repos=self.hidden_repos,
                show_details=self.show_details,
            )
        )

    def set_notice(self, text: str, seconds: float = NOTICE_SECONDS) -> None:
        """Show ``text`` in the panel subtitle for ``seconds``.

        The dashboard owns the whole screen, so a rejected key or a view-only
        row has nowhere to print — but staying silent is indistinguishable
        from being broken, hence this. A failure worth reading (a refused
        account command) is kept up longer.
        """
        self.notice = text
        self.notice_until = time.monotonic() + seconds

    def persist_view_state(self, state: ViewState) -> None:
        """Write ``state`` to ``view_prefs``, degrading instead of crashing.

        The repo menu and settings overlay commit to SQLite straight from a
        keypress (fold and setting toggles). A write failure here (``database
        is locked`` against a concurrent background worker, a read-only state
        dir) would otherwise end the whole session with a traceback. The
        fold/toggle already took effect on screen by the time this runs —
        only persistence is lost.
        """
        try:
            save_view_state(self.engine, FRONTEND_TUI, state)
        except Exception:
            log.debug("failed to save dashboard view state", exc_info=True)
            self.set_notice("could not save view settings")

    def open_settings_overlay(self) -> SettingsState:
        """A fresh settings overlay over the current ``groups``/``folded``.

        A small closure rather than inlining twice: opening from the plain
        table and switching in from another overlay (see the `F2` handling
        below) both need it.
        """
        return open_settings(
            field_names=all_column_names(),
            enabled=frozenset(self.enabled if self.enabled is not None else default_columns()),
            repo_prefixes=settings_repo_prefixes(self.all_groups, self.folded),
            folded=self.folded,
            visibility_repo_prefixes=tuple(dict.fromkeys(g.prefix for g in self.all_groups)),
            show_empty_repos=self.show_empty_repos,
            hidden_repos=self.hidden_repos,
        )

    def _report_vanished_repo(self, repo: RepoTarget) -> None:
        """Notice-and-refresh for an `OSError` from a repo-rooted dispatch.

        Shared by `dispatch` and `run_new_container`: both hand a real
        repo root to a child process as `cwd`, and both can have that
        directory vanish between a refresh and the keypress that
        dispatches — `subprocess`/`Popen` raise, not exit non-zero,
        for a missing `cwd`. Previously uncaught on either path, this
        took the whole TUI down.
        """
        self.set_notice(f"'{repo.repo_root}' no longer exists")
        self.client.refresh()

    def dispatchable(self, target: str, verb: str) -> RepoTarget | None:
        """The repo to run ``verb`` on ``target`` in, or None after noticing why not.

        The SSH policy and the action's current availability, checked
        before anything is shown or run.
        """
        group = _find_group(self.groups, target)
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
                self.ssh_policy,
                over_ssh=self.over_ssh,
            )
        except RouteError as exc:
            self.set_notice(str(exc))
            return None
        if verb not in {
            current_verb
            for _label, current_verb in actions_for_container(
                self.groups,
                target,
                remote=self.remote,
                ssh_policy=self.ssh_policy,
                over_ssh=self.over_ssh,
            )
        }:
            self.set_notice(f"Action '{verb}' is no longer available for '{target}'")
            return None
        return repo

    def dispatch(self, target: str, verb: str) -> None:
        repo = self.dispatchable(target, verb)
        if repo is None:
            return
        try:
            rc = self.terminal.hand_off(
                lambda: _dispatch_action(
                    repo,
                    verb,
                    target,
                    remote=self.remote,
                    over_ssh=self.over_ssh,
                    ssh_policy=self.ssh_policy,
                )
            )
        except RouteError as exc:
            self.set_notice(str(exc))
            return
        except OSError:
            self._report_vanished_repo(repo)
            return
        if rc != 0:
            self.set_notice(f"'jailbee {verb} {target}' exited {rc}")
        self.client.refresh()  # an action likely changed state — refresh ASAP

    def open_egress(self, prefix: str, container: str | None) -> EgressState | None:
        """Load one scoped view after checking the read permission."""
        group = next((item for item in self.groups if item.prefix == prefix), None)
        if group is None or RepoTarget.of(group) is None:
            self.set_notice(f"'{prefix}' is no longer listed or has no repo directory")
            return None
        if container is not None and not any(c.name == container for c in group.containers):
            self.set_notice(f"'{container}' is no longer listed")
            return None
        argv = ["net", "egress", "ls", *([container] if container else ["--repo"])]
        try:
            check_dashboard_command(argv, self.ssh_policy, over_ssh=self.over_ssh)
            rows = load_egress_rows(Path(group.repo_root or ""), self.incus, container)
        except Exception as exc:
            self.set_notice(f"could not load egress entries: {exc}")
            return None
        state = EgressState(prefix, container, rows)
        return replace(
            state,
            can_add=self.egress_permitted(state, "add"),
            can_rm=self.egress_permitted(state, "rm"),
        )

    def egress_permitted(self, state: EgressState, action: Literal["add", "rm"]) -> bool:
        """Check the current remote policy for the scoped mutation."""
        try:
            check_dashboard_command(
                egress_argv(state, action, "example.com"),
                self.ssh_policy,
                over_ssh=self.over_ssh,
            )
        except RouteError:
            return False
        return True

    def egress_target(self, state: EgressState) -> RepoTarget | None:
        """The panel's repo, or None once it or its container is gone."""
        group = next((item for item in self.groups if item.prefix == state.prefix), None)
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

    def begin_egress_add(self, state: EgressState) -> TextPrompt | EgressState | None:
        """Open the destination question, or explain why not."""
        if self.egress_target(state) is None:
            self.set_notice("Egress target is no longer available")
            return None
        if not self.egress_permitted(state, "add"):
            self.set_notice("net egress add is not permitted by the SSH policy")
            return state
        return TextPrompt(
            "egress-add",
            "Add egress override",
            "Destination (host, host:port, *.domain, IPv4, or CIDR)",
            target=state.prefix,
            back=state,
        )

    def egress_add(self, index: int) -> None:
        """`a` on the Egress panel: the destination question; Esc lands back on row ``index``."""
        state = self.overlay
        assert isinstance(state, EgressState)
        self.overlay = self.begin_egress_add(replace(state, start_index=index))

    def egress_remove(self, row: EntryRow) -> None:
        """`r` on the Egress panel: remove ``row``'s override at this scope."""
        state = self.overlay
        assert isinstance(state, EgressState)
        self.overlay = self.mutate_egress(state, "rm", row=row)

    def mutate_egress(
        self,
        state: EgressState,
        action: Literal["add", "rm"],
        entry: str | None = None,
        *,
        row: EntryRow | None = None,
    ) -> EgressState | None:
        """Reauthorize and start one scoped mutation, detached.

        ``add`` takes its destination from the inline prompt
        (:func:`begin_egress_add`); ``rm`` acts on the selected row.
        The panel stays up while the change runs. When it ends,
        ``finish`` closes the panel *and* the menu it was opened from
        after a success; a failure keeps the panel up, reloaded, for
        a retry.
        """
        target = self.egress_target(state)
        if target is None:
            self.set_notice("Egress target is no longer available")
            return None
        if action == "rm":
            entry = removable_entry(state, row) if row is not None else None
            if not entry:
                self.set_notice("Select a removable override first")
                return state
        assert entry, "add needs the destination from the inline prompt"
        # Rechecked at submit: the policy may have changed while the
        # destination question was open.
        if not self.egress_permitted(state, action):
            self.set_notice(f"net egress {action} is not permitted by the SSH policy")
            return state
        argv = [
            *egress_argv(state, action, entry),
            *(target.flags() if not self.over_ssh else []),
        ]
        try:
            check_dashboard_command(argv, self.ssh_policy, over_ssh=self.over_ssh)
        except RouteError as exc:
            self.set_notice(str(exc))
            return state

        key = f"egress:{state.prefix}:{state.container or ''}"
        if self.jobs.busy(key):
            self.set_notice("An egress change is still running here")
            return state

        def finish(result: JobResult) -> None:
            """Report the change and refresh the panel, if it is still open."""
            if result.returncode != 0:
                reason = result.failure_line() or f"exited {result.returncode}"
                self.set_notice(
                    f"net egress {action} {entry} failed: {reason}",
                    seconds=FAILURE_NOTICE_SECONDS,
                )
            else:
                self.client.refresh()
                self.set_notice(f"net egress {action} {entry}: done")
            panel = _egress_panel(self.overlay)
            if panel is None or (panel.prefix, panel.container) != (
                state.prefix,
                state.container,
            ):
                return
            if result.returncode == 0:
                # Done is done: the panel and the menu it was opened
                # from close, as every other menu action does.
                self.overlay = None
                return
            try:
                rows = load_egress_rows(target.repo_root, self.incus, state.container)
            except Exception as exc:
                self.set_notice(f"could not refresh egress entries: {exc}")
                return
            self.overlay = _with_egress_panel(self.overlay, replace(panel, rows=rows))

        try:
            self.jobs.start(
                key,
                f"egress {action} {entry}…",
                ["jailbee", *argv],
                target.cwd(),
                finish,
            )
        except OSError:
            self._report_vanished_repo(target)
            return None
        return state

    def start_new_container(self, *, from_pr: bool = False) -> TextPrompt | None:
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
            check_dashboard_command(["new"], self.ssh_policy, over_ssh=self.over_ssh)
        except RouteError as exc:
            self.set_notice(str(exc))
            return None
        note = new_container_reject_note(self.groups, self.selected)
        if note is not None:
            self.set_notice(note)
            return None
        group = new_container_target(self.groups, self.selected)
        assert group is not None  # guaranteed by the note being None
        if from_pr:
            return TextPrompt("new-pr", "New container from a PR", "PR number", target=group.prefix)
        base_default = new_container_base_default(group.repo_root)
        return TextPrompt(
            "new-branch",
            "New container",
            "New branch",
            target=group.prefix,
            carry=(base_default or "",),
        )

    def run_new_container(
        self, prefix: str, what: str, build_argv: Callable[[RepoTarget], list[str]]
    ) -> None:
        """Re-resolve the repo (it may have vanished while the prompt was open) and run."""
        group = next((g for g in self.groups if g.prefix == prefix), None)
        repo = RepoTarget.of(group) if group is not None else None
        if repo is None:
            self.set_notice(f"'{prefix}' is no longer listed")
            return
        argv = build_argv(repo)
        try:
            check_dashboard_command(argv[1:], self.ssh_policy, over_ssh=self.over_ssh)
        except RouteError as exc:
            self.set_notice(str(exc))
            return

        def spawn() -> int:
            rc = subprocess.run(argv, check=False, cwd=repo.cwd()).returncode
            _wait_for_return()
            return rc

        def run_in_foreground() -> None:
            try:
                rc = self.terminal.hand_off(spawn)
            except OSError:
                self._report_vanished_repo(repo)
                return
            if rc != 0:
                self.set_notice(f"'jailbee new' exited {rc}")
            self.client.refresh()  # the new container should appear on the next frame

        def finish(result: JobResult) -> None:
            if needs_terminal(result):
                # It stopped to ask something; the question needs the
                # real terminal, so ask it there, as before.
                run_in_foreground()
                return
            if result.returncode != 0:
                reason = result.failure_line() or f"exited {result.returncode}"
                self.set_notice(f"jailbee new failed: {reason}", seconds=FAILURE_NOTICE_SECONDS)
            self.client.refresh()

        try:
            self.jobs.start(f"new:{prefix}:{what}", f"creating {what}…", argv, repo.cwd(), finish)
        except ValueError:
            self.set_notice("That container is already being created")
        except OSError:
            self._report_vanished_repo(repo)

    def run_dashboard_command(
        self,
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
        repo = self.repo_for(target, kind)
        if repo is None:
            self.set_notice(f"'{target}' is gone")
            return
        try:
            check_dashboard_command(argv, self.ssh_policy, over_ssh=self.over_ssh)
        except RouteError as exc:
            self.set_notice(str(exc), seconds=FAILURE_NOTICE_SECONDS)
            return
        try:
            rc = self.terminal.hand_off(
                lambda: _run_cli_foreground(
                    repo,
                    argv,
                    style=style,
                    remote=self.remote,
                    over_ssh=self.over_ssh,
                    ssh_policy=self.ssh_policy,
                )
            )
        except RouteError as exc:
            self.set_notice(str(exc), seconds=FAILURE_NOTICE_SECONDS)
            return
        except OSError:
            self._report_vanished_repo(repo)
            return
        if rc != 0:
            self.set_notice(f"'jailbee {dact.command_label(argv)}' exited {rc}")
        self.client.refresh()  # the command likely changed state: refresh now

    def open_container_entry(self, container: str, verb: str) -> Overlay | None:
        """The first step of a terminal-only container entry; None once it has run."""
        if verb == dact.AUTOSTART_STATUS:
            self.run_dashboard_command(
                container, "container", dact.autostart_status_argv(container)
            )
            return None
        if verb == dact.AUTOSTART_CANCEL:
            return dact.autostart_cancel_picker(container)
        if verb == dact.SNAPSHOTS:
            return self.open_snapshots(container)
        if verb in (dact.MOUNT_ADD, dact.MOUNT_REMOVE):
            return self.open_mount_picker(container, remove=verb == dact.MOUNT_REMOVE)
        return None

    def open_retarget(self, container: str) -> TextPrompt | None:
        """Ask for the new base inline; the CLI's own picker would blank the screen."""
        if self.dispatchable(container, "git retarget") is None:
            return None
        group = _find_group(self.groups, container)
        info = (
            next((c for c in group.containers if c.name == container), None)
            if group is not None
            else None
        )
        if group is None or info is None:
            self.set_notice(f"'{container}' is gone")
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

    def open_mount_picker(self, container: str, *, remove: bool) -> Picker | None:
        """The kinds Mount… (Unmount…) can act on right now, or a notice."""
        group = _find_group(self.groups, container)
        info = (
            next((c for c in group.containers if c.name == container), None)
            if group is not None
            else None
        )
        if group is None or info is None:
            self.set_notice(f"'{container}' is gone")
            return None
        kinds = dact.mount_choices(info, group.optional_mounts, remove=remove)
        if not kinds:
            self.set_notice("No optional mount to remove" if remove else "No optional mount to add")
            return None
        return dact.mount_picker(container, kinds, remove=remove)

    def open_snapshots(self, container: str) -> Picker | None:
        """List the container's snapshots quietly and offer them, or notice why not.

        Each entry is gated on its own argv: over SSH an allowlist may
        permit the listing and not the create.
        """
        repo = self.repo_for(container, "container")
        if repo is None:
            self.set_notice(f"'{container}' is gone")
            return None
        argv = dact.addressed(
            dact.snapshot_ls_argv(container), repo.flags(), over_ssh=self.over_ssh
        )
        try:
            check_dashboard_command(argv, self.ssh_policy, over_ssh=self.over_ssh)
            result = da.run_cli_quiet(argv, cwd=repo.cwd())
            if not result.ok:
                raise dact.SnapshotLoadError(result.message)
            rows = dact.parse_snapshot_rows(result.stdout)
        except (RouteError, dact.SnapshotLoadError) as exc:
            self.set_notice(f"could not list snapshots: {exc}", seconds=FAILURE_NOTICE_SECONDS)
            return None
        picker = dact.snapshot_picker(
            container,
            rows,
            can_create=permitted(
                dact.snapshot_create_argv(container, None), self.ssh_policy, over_ssh=self.over_ssh
            ),
        )
        if not picker.entries:
            self.set_notice(f"No snapshots of '{container}'")
            return None
        return picker

    def open_outbox(self, container: str) -> Picker | None:
        """List the container's staged proposals quietly and offer them, or notice why not.

        Each entry is gated on its own argv, as for snapshots.
        """
        repo = self.repo_for(container, "container")
        if repo is None:
            self.set_notice(f"'{container}' is gone")
            return None
        argv = dact.addressed(dob.outbox_ls_argv(container), repo.flags(), over_ssh=self.over_ssh)
        try:
            check_dashboard_command(argv, self.ssh_policy, over_ssh=self.over_ssh)
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
            self.set_notice(f"could not list the outbox: {exc}", seconds=FAILURE_NOTICE_SECONDS)
            return None
        if listing.error is not None:
            self.set_notice(
                f"could not read the outbox: {listing.error}",
                seconds=FAILURE_NOTICE_SECONDS,
            )
            return None
        if not listing.rows:
            self.set_notice(
                listing.warnings[0] if listing.warnings else f"Outbox of '{container}' is empty",
                seconds=FAILURE_NOTICE_SECONDS if listing.warnings else NOTICE_SECONDS,
            )
            return None
        self.outbox_rows[container] = listing.rows
        return dob.outbox_picker(
            container,
            listing.rows,
            can_browse=permitted(
                dob.outbox_browse_argv(container), self.ssh_policy, over_ssh=self.over_ssh
            ),
        )

    def submit_outbox_picker(self, picker: Picker, entry: PickerEntry) -> Overlay | None:
        """The `container-outbox*` steps. Show and Publish run in the terminal.

        Publishing talks to GitHub and may outlast the quiet runner's
        60 s cutoff; a delete is local and runs quietly.
        """
        container = picker.target
        if picker.purpose == "container-outbox":
            if entry.value == dob.BROWSE:
                self.run_dashboard_command(
                    container, "container", dob.outbox_browse_argv(container), style="plain"
                )
                return None
            pid = dob.proposal_id(entry.value)
            row = next((r for r in self.outbox_rows.get(container, ()) if r.id == pid), None)
            if row is None:
                return None
            actions = dob.proposal_picker(
                container,
                row,
                can_show=permitted(
                    dob.outbox_show_argv(container, row.id), self.ssh_policy, over_ssh=self.over_ssh
                ),
                can_publish=permitted(
                    dob.outbox_apply_argv(container, row.id, row.revision),
                    self.ssh_policy,
                    over_ssh=self.over_ssh,
                ),
                can_delete=permitted(
                    dob.outbox_drop_argv(container, row.id, row.revision),
                    self.ssh_policy,
                    over_ssh=self.over_ssh,
                ),
            )
            if not actions.entries:
                self.set_notice(f"Nothing can be done to {row.id} here")
                return None
            return actions
        if picker.purpose == "container-outbox-proposal":
            pid, revision, count = picker.carry
            if entry.value == dob.SHOW:
                self.run_dashboard_command(
                    container,
                    "container",
                    dob.outbox_show_argv(container, pid),
                    style="paged",
                )
                return None
            if entry.value in (dob.PUBLISH, dob.DELETE):
                return dob.outbox_confirm_picker(container, entry.value, pid, revision, int(count))
            return None
        if picker.purpose == "container-outbox-confirm":
            if entry.value != "yes":
                self.set_notice("Cancelled")
                return None
            action, pid, revision = picker.carry
            if action == dob.PUBLISH:
                self.run_dashboard_command(
                    container, "container", dob.outbox_apply_argv(container, pid, revision)
                )
            elif action == dob.DELETE:
                repo = self.repo_for(container, "container")
                if repo is None:
                    self.set_notice(f"'{container}' is gone")
                elif self.run_quiet_cli(repo, dob.outbox_drop_argv(container, pid, revision)):
                    self.client.refresh()
            return None
        return None

    def submit_snapshot_picker(self, picker: Picker, entry: PickerEntry) -> Overlay | None:
        """The `container-snapshot*` steps. Every change runs in the terminal.

        Not quietly: `run_cli_quiet` kills its child after 60 s, and an
        `incus snapshot` of a large container can outlast that.
        """
        container = picker.target
        if picker.purpose == "container-snapshots":
            if entry.value == dact.CREATE_TIMESTAMP:
                self.run_dashboard_command(
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
                    self.ssh_policy,
                    over_ssh=self.over_ssh,
                ),
                can_delete=permitted(
                    dact.snapshot_delete_argv(container, tag),
                    self.ssh_policy,
                    over_ssh=self.over_ssh,
                ),
            )
            if not actions.entries:
                self.set_notice(f"No change to snapshot {tag} is permitted here")
                return None
            return actions
        if picker.purpose == "container-snapshot-action":
            if entry.value not in (dact.RESTORE, dact.DELETE):
                return None
            return dact.snapshot_confirm_picker(container, entry.value, picker.carry[0])
        if picker.purpose == "container-snapshot-confirm":
            if entry.value != "yes":
                self.set_notice("Cancelled")
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
            self.run_dashboard_command(container, "container", build(container, tag))
            return None
        return None

    def repo_for(
        self, target: str, kind: Literal["repo", "container"] = "repo"
    ) -> RepoTarget | None:
        """The listed repo of a prefix, or of a container name with ``kind``."""
        group = target_group(self.groups, target, kind)
        return RepoTarget.of(group) if group is not None else None

    def run_quiet_cli(self, repo: RepoTarget, argv: list[str]) -> bool:
        """Run one short `jailbee` change off-screen (an account or a mount).

        The outcome is reported as a notice.

        Quiet rather than `foreground`: the command asks nothing, so
        handing it the terminal would only blank the dashboard. A
        refusal — typically an agent still running, which the CLI's
        own message answers with `--force` — stays up long enough to
        read. There is no automatic retry with `--force`.
        """
        full = dact.addressed(argv, repo.flags(), over_ssh=self.over_ssh)
        try:
            check_dashboard_command(full, self.ssh_policy, over_ssh=self.over_ssh)
        except RouteError as exc:
            self.set_notice(str(exc), seconds=FAILURE_NOTICE_SECONDS)
            return False
        result = da.run_cli_quiet(full, cwd=repo.cwd())
        self.set_notice(
            result.message,
            seconds=NOTICE_SECONDS if result.ok else FAILURE_NOTICE_SECONDS,
        )
        self.client.refresh()  # a group or mount change shows in the next gather
        return result.ok

    def load_listing(
        self, repo: RepoTarget, listing_argv: list[str], what: str
    ) -> tuple[da.AccountRow, ...] | None:
        """Rows of one `jailbee account … ls`, or None after noticing why not."""
        argv = [*listing_argv, *(repo.flags() if not self.over_ssh else [])]
        try:
            check_dashboard_command(argv, self.ssh_policy, over_ssh=self.over_ssh)
            result = da.run_cli_quiet(argv, cwd=repo.cwd())
            if not result.ok:
                raise da.AccountLoadError(result.message)
            return da.parse_account_rows(result.stdout)
        except (RouteError, da.AccountLoadError) as exc:
            self.set_notice(f"could not list {what}: {exc}", seconds=FAILURE_NOTICE_SECONDS)
            return None

    def load_group_rows(self, repo: RepoTarget) -> tuple[da.AccountRow, ...] | None:
        """The host's credential groups, or None after noticing why not."""
        return self.load_listing(repo, da.group_ls_argv(), "credential groups")

    def group_picker(
        self,
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
        self, purpose: Literal["repo-group", "container-group"], target: str
    ) -> Picker | None:
        """List the groups for ``target``'s repo and offer them, or notice why not."""
        repo = self.repo_for(target, prompt_target_kind(purpose))
        if repo is None:
            self.set_notice(f"'{target}' is no longer listed")
            return None
        rows = self.load_group_rows(repo)
        return self.group_picker(purpose, target, rows) if rows is not None else None

    def change_group(self, overlay: TextPrompt | Picker, argv: list[str]) -> None:
        """Re-resolve the overlay's target (it may have vanished) and run one change."""
        target = overlay.target
        repo = self.repo_for(target, prompt_target_kind(overlay.purpose))
        if repo is None:
            self.set_notice(f"'{target}' is gone")
            return
        self.run_quiet_cli(repo, argv)

    def accounts_target(self) -> str | None:
        """The repo prefix the Accounts panel runs its `jailbee account …` in.

        The listing is host-wide, so any real repo would answer it; the
        selected row's repo is preferred because that is the config a
        user expects `--config` to name. Falls back to the first repo
        with a root, so `A` also works from an orphan row.
        """
        prefix = fold_target(self.groups, self.selected)
        if prefix is not None and self.repo_for(prefix) is not None:
            return prefix
        return next((g.prefix for g in self.groups if RepoTarget.of(g) is not None), None)

    def load_accounts(self, prefix: str) -> da.AccountsState | None:
        """The Accounts panel for ``prefix``'s repo."""
        repo = self.repo_for(prefix)
        if repo is None:
            self.set_notice(f"'{prefix}' is gone", seconds=FAILURE_NOTICE_SECONDS)
            return None
        rows = self.load_listing(repo, da.account_ls_argv(), "accounts")
        if rows is None:
            return None
        return da.AccountsState(rows, 0, prefix)

    def open_accounts(self) -> da.AccountsState | None:
        """Open the Accounts panel, or notice why not."""
        prefix = self.accounts_target()
        if prefix is None:
            self.set_notice("No repo to address account commands at")
            return None
        return self.load_accounts(prefix)

    def account_chosen(self, row: da.AccountRow, index: int) -> None:
        """Enter on an Accounts row: what can be done with it; a cancel lands back on ``index``."""
        state = self.overlay
        assert isinstance(state, da.AccountsState)
        self.overlay = self.account_actions_picker(replace(state, start_index=index), row)

    def account_new_group(self, index: int) -> None:
        """`n` on the Accounts panel: ask for the new group's name."""
        state = self.overlay
        assert isinstance(state, da.AccountsState)
        self.overlay = TextPrompt(
            "acct-group-new",
            "New credential group",
            "Group name",
            target=state.prefix,
            back=replace(state, start_index=index),
        )

    def account_actions_picker(self, state: da.AccountsState, row: da.AccountRow) -> Overlay:
        """What can be done with ``row``, or the panel with a notice."""
        actions = da.account_actions(row, state.rows)
        if not actions:
            self.set_notice("No actions for this row")
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

    def run_account_change(self, state: da.AccountsState, argv: list[str]) -> Overlay | None:
        """Run one change from the panel; a change that worked closes it.

        Done is done: the CLI's own message stays up as the notice, so
        there is nothing left to Esc out of. The repo is re-resolved
        first — it may have vanished while a picker was open. A refused
        change keeps the listing up under its notice, for a retry.
        """
        repo = self.repo_for(state.prefix)
        if repo is None:
            self.set_notice(f"'{state.prefix}' is gone", seconds=FAILURE_NOTICE_SECONDS)
            return None
        return None if self.run_quiet_cli(repo, argv) else state

    def submit_account_picker(
        self, picker: Picker, entry: PickerEntry, state: da.AccountsState
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
                        PickerEntry(r.account, r.account) for r in logins if r.account is not None
                    ),
                    target=picker.target,
                    carry=picker.carry,
                    back=state,
                )
            if entry.value == "park":
                return self.run_account_change(state, da.park_argv(agent, group or None))
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
            return self.run_account_change(state, da.use_argv(agent, group or None, entry.value))
        if picker.purpose == "acct-use-in":
            agent, _group, ref = picker.carry
            return self.run_account_change(state, da.use_argv(agent, entry.value, ref))
        if picker.purpose == "acct-confirm":
            if entry.value != "yes":
                return state
            action, agent, group, ref = picker.carry
            return self.run_account_change(
                state,
                da.rm_login_argv(agent, ref) if action == "delete" else da.group_rm_argv(group),
            )
        return state

    def submit_prompt(self, prompt: TextPrompt, text: str) -> Overlay | None:
        """Act on a confirmed answer; return the overlay to show next.

        None closes the overlay. Every purpose returns explicitly: the
        caller shows exactly what this returns, with no fallback.
        """
        answer = text.strip()
        if prompt.purpose == "new-pr":
            number = parse_pr_number(answer)
            assert number is not None  # validate_answer guaranteed it

            def pr_argv(repo: RepoTarget) -> list[str]:
                if self.over_ssh:
                    # Remote sessions address their selected repo by
                    # cwd, not by an explicit host config path.
                    return ["jailbee", "new", "--background", "--pr", str(number)]
                return new_pr_container_argv(repo, number)

            self.run_new_container(prompt.target, f"PR #{number}", pr_argv)
            return None
        if prompt.purpose == "new-branch":
            return TextPrompt(
                "new-base",
                prompt.title,
                "Base branch",
                initial=prompt.carry[0],
                target=prompt.target,
                carry=(answer,),
                suggestions=host_branches(
                    repo.repo_root
                    if (repo := target_group(self.groups, prompt.target, "repo"))
                    else None
                ),
            )
        if prompt.purpose == "new-base":
            branch = prompt.carry[0]

            def branch_argv(repo: RepoTarget) -> list[str]:
                if self.over_ssh:
                    return ["jailbee", "new", "--background", "--", branch, answer]
                return new_container_argv(repo, branch, answer)

            self.run_new_container(prompt.target, branch, branch_argv)
            return None
        if prompt.purpose == "container-retarget":
            self.run_dashboard_command(
                prompt.target, "container", dact.retarget_argv(prompt.target, answer)
            )
            return None
        if prompt.purpose == "egress-add":
            # begin_egress_add always sets it
            assert isinstance(prompt.back, EgressState)
            return self.mutate_egress(prompt.back, "add", answer)
        if prompt.purpose == "repo-group-name":
            self.change_group(prompt, da.repo_group_set_argv(answer))
            return None
        if prompt.purpose == "container-group-name":
            self.change_group(prompt, da.container_group_use_argv(answer, prompt.target))
            return None
        if prompt.purpose == "container-snapshot-tag":
            self.run_dashboard_command(
                prompt.target, "container", dact.snapshot_create_argv(prompt.target, answer)
            )
            return None
        if prompt.purpose == "acct-group-new":
            # asked only from the Accounts panel, which it returns to
            assert isinstance(prompt.back, da.AccountsState)
            return self.run_account_change(prompt.back, da.group_create_argv(answer))
        return None

    def submit_picker(self, picker: Picker, entry: PickerEntry) -> Overlay | None:
        """Act on a chosen entry; return the overlay to show next.

        None closes the overlay, as in :func:`submit_prompt`.
        """
        if picker.purpose == "repo-group":
            if entry.value == "__new__":
                return TextPrompt(
                    "repo-group-name", picker.title, "Group name", target=picker.target
                )
            self.change_group(
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
            self.change_group(
                picker,
                da.container_group_reset_argv(picker.target)
                if entry.value == "__reset__"
                else da.container_group_use_argv(entry.value, picker.target),
            )
            return None
        if picker.purpose == "repo-apply":
            self.run_dashboard_command(
                picker.target,
                "repo",
                dact.apply_argv(no_restart=entry.value == dact.APPLY_NO_RESTART),
            )
            return None
        if picker.purpose == "container-autostart-cancel":
            if entry.value == "yes":
                self.run_dashboard_command(
                    picker.target, "container", dact.autostart_cancel_argv(picker.target)
                )
            else:
                self.set_notice("Cancelled")
            return None
        if picker.purpose.startswith("container-snapshot"):
            return self.submit_snapshot_picker(picker, entry)
        if picker.purpose.startswith("container-outbox"):
            return self.submit_outbox_picker(picker, entry)
        if picker.purpose in ("container-mount-add", "container-mount-remove"):
            build = (
                dact.unmount_argv if picker.purpose == "container-mount-remove" else dact.mount_argv
            )
            repo = self.repo_for(picker.target, "container")
            if repo is None:
                self.set_notice(f"'{picker.target}' is gone")
            else:
                self.run_quiet_cli(repo, build(entry.value, picker.target))
            return None
        if picker.purpose.startswith("acct-"):
            # every account picker is opened from the Accounts panel
            assert isinstance(picker.back, da.AccountsState)
            return self.submit_account_picker(picker, entry, picker.back)
        return picker.back

    def edit_config(self, *, global_layer: bool) -> None:
        """Hand the terminal to `jailbee config edit` for the selected repo.

        A foreground dispatch, not a detached spawn: it is a full-screen
        TUI and needs the real terminal, exactly like `shell` and `tmux`.

        The global layer needs a repo too — `config_edit.layers.validate`
        loads the repo config even for a global-layer edit, because a
        global change only means anything through its effect on some
        repo's merged config.
        """
        prefix = fold_target(self.groups, self.selected) or ""
        note = config_edit_reject_note_for_prefix(self.groups, prefix, global_layer=global_layer)
        if note is not None:
            self.set_notice(note)
            return
        group = next(g for g in self.groups if g.prefix == prefix)
        repo = RepoTarget.of(group)
        assert repo is not None  # the note rejects a rootless group
        argv = ["jailbee", "config", "edit", *repo.flags()]
        if global_layer:
            argv.append("--global")
        try:
            rc = self.terminal.hand_off(
                lambda: subprocess.run(argv, check=False, cwd=repo.cwd()).returncode
            )
        except OSError:
            self._report_vanished_repo(repo)
            return
        if rc != 0:
            self.set_notice(f"'jailbee config edit' exited {rc}")
        self.client.refresh()  # config may have changed under every row

    def run_command(self, text: str) -> None:
        """Authorize and run the edited argv in the selected repo."""
        name = container_of(self.selected)
        if self.selected is None:
            self.set_notice("Select a repo or a container first")
            return
        group = (
            _find_group(self.groups, name)
            if name is not None
            else next((g for g in self.groups if g.prefix == self.selected.key), None)
        )
        if group is None:
            self.set_notice("Selected repo is no longer listed")
            return
        repo = RepoTarget.of(group)
        if repo is None:
            self.set_notice(
                view_only_note(self.groups, name)
                or f"No repo found for '{group.prefix}' — this row is view-only"
            )
            return
        try:
            argv = command_argv(text, name)
            if not self.over_ssh:
                argv = insert_options_before_separator(argv, repo.flags())
            check_dashboard_command(argv, self.ssh_policy, over_ssh=self.over_ssh)
        except (ValueError, RouteError) as exc:
            self.set_notice(str(exc))
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

            rc = self.terminal.hand_off(execute_command)
        except OSError:
            self._report_vanished_repo(repo)
            return
        if rc != 0:
            self.set_notice(f"'jailbee {' '.join(argv)}' exited {rc}")
        self.client.refresh()

    def _clamped(self, offset: int) -> int:
        return clamp_column_offset(
            self.groups,
            offset,
            now=_now(),
            enabled=self.enabled,
            folded=self.folded,
            column_widths=self.column_widths,
            shown_columns=self.shown_columns,
            available=self.terminal.table_width,
        )

    def _close_vanished_overlay(self) -> None:
        """Close an overlay whose repo or container left the snapshot."""

        if (
            isinstance(self.overlay, MenuState)
            and Row("container", self.overlay.container) not in self.rows
        ):
            # The menu's container vanished under it (destroyed, or its
            # repo dropped out of the registry) — close rather than
            # dispatch at a name that is no longer there.
            self.set_notice(f"'{self.overlay.container}' is gone — menu closed")
            self.overlay = None
        if (
            isinstance(self.overlay, RepoMenuState)
            and Row("repo", self.overlay.repo) not in self.rows
        ):
            self.set_notice(f"'{self.overlay.repo}' is gone — menu closed")
            self.overlay = None
        if isinstance(self.overlay, EgressState) and (
            not any(g.prefix == self.overlay.prefix for g in self.groups)
            or (
                self.overlay.container is not None
                and not any(
                    c.name == self.overlay.container
                    for g in self.groups
                    if g.prefix == self.overlay.prefix
                    for c in g.containers
                )
            )
        ):
            self.set_notice("Egress target is gone — panel closed")
            self.overlay = None
        if (
            isinstance(self.overlay, da.AccountsState)
            and self.repo_for(self.overlay.prefix) is None
        ):
            # The repo its account commands run in is gone; a reopen
            # (`A`) picks another one.
            self.set_notice(f"'{self.overlay.prefix}' is gone — accounts closed")
            self.overlay = None
        if (
            isinstance(self.overlay, (TextPrompt, Picker))
            and self.overlay.target
            and target_group(
                self.groups, self.overlay.target, prompt_target_kind(self.overlay.purpose)
            )
            is None
        ):
            # The prompt's repo or container vanished while it was
            # open — close rather than ask a question about nothing.
            self.set_notice(f"'{self.overlay.target}' is gone — prompt closed")
            self.overlay = None

    def _pin_selection(self) -> None:
        """Keep the cursor honest: pinned while an overlay is about a row, else reconciled."""

        # The Egress panel on screen, itself or behind its question.
        egress_panel = (
            self.overlay
            if isinstance(self.overlay, EgressState)
            else self.overlay.back
            if isinstance(self.overlay, (TextPrompt, Picker))
            and isinstance(self.overlay.back, EgressState)
            else None
        )
        if egress_panel is None:
            # The menu an Egress panel's Esc returns to outlives the
            # panel only while it (or its question) is open — however
            # it closed: a vanished target, a failed change, `q`.
            self.egress_parent = None
        if isinstance(self.overlay, MenuState):
            self.selected = Row(
                "container", self.overlay.container
            )  # pinned while the menu is open
        elif isinstance(self.overlay, RepoMenuState):
            self.selected = Row("repo", self.overlay.repo)
        elif egress_panel is not None:
            # A question asked from the Egress panel keeps the panel's
            # row, so the cursor does not jump to the repo header.
            self.selected = (
                Row("container", egress_panel.container)
                if egress_panel.container is not None
                else Row("repo", egress_panel.prefix)
            )
        elif isinstance(self.overlay, da.AccountsState) or (
            isinstance(self.overlay, (TextPrompt, Picker))
            and isinstance(self.overlay.back, da.AccountsState)
        ):
            # Host-wide: its questions target a repo only to run the
            # CLI there, so the cursor stays where `A` was pressed
            # instead of jumping to that repo's header.
            self.selected = reconcile_selection(self.rows, self.selected, self.sel_index)
        elif (
            isinstance(self.overlay, (TextPrompt, Picker))
            and not self.overlay.purpose.startswith("new-")
            and target_group(
                self.groups, self.overlay.target, prompt_target_kind(self.overlay.purpose)
            )
            is not None
        ):
            # A question about one repo or container keeps its row,
            # like its menu does. `n`'s questions are left out: they
            # ask about the highlighted row's repo, and pinning its
            # header would strand the cursor there after Esc.
            self.selected = Row(prompt_target_kind(self.overlay.purpose), self.overlay.target)
        else:
            self.selected = reconcile_selection(self.rows, self.selected, self.sel_index)
        if self.selected in self.rows:
            self.sel_index = self.rows.index(self.selected)

    def handle_key(self, token: str) -> Outcome:
        """A dashboard key (a :func:`parse_key` token); ``"quit"`` ends the dashboard."""
        if token == "interrupt":
            return "quit"
        if self.overlay is not None:
            return None  # a native box has the focus; its keys never come through here
        if token == "quit":
            return "quit"
        return self._table_key(token)

    def command_candidates(self, text: str) -> tuple[str, ...]:
        """Completions for the `!` line's ``text``, filtered by this session's SSH policy."""
        selected_group = (
            _find_group(self.groups, container_of(self.selected))
            if container_of(self.selected) is not None
            else next(
                (
                    group
                    for group in self.groups
                    if self.selected and group.prefix == self.selected.key
                ),
                None,
            )
        )
        allowed_paths: frozenset[str] | None = None
        if self.over_ssh:
            if self.ssh_policy is None:
                allowed_paths = frozenset()
            else:
                allowed_paths = ssh_router.allowed_command_paths(
                    self.ssh_policy.commands,
                    restrict_host=self.ssh_policy.restrict_host,
                    scope=self.scope,
                    unlocks=ssh_router.RemoteUnlocks.of(self.ssh_policy),
                )
        return completion_candidates(
            text,
            tuple(c.name for c in selected_group.containers) if selected_group is not None else (),
            allowed_paths,
            restrict_host=bool(
                self.over_ssh
                and self.ssh_policy is not None
                and host_restricted(self.ssh_policy.restrict_host)
            ),
            unlocks=ssh_router.RemoteUnlocks.of(self.ssh_policy if self.over_ssh else None),
        )

    def command_submitted(self, text: str) -> None:
        """Enter on the `!` line: close it and run ``text``."""
        self.overlay = None
        self.run_command(text)

    def prompt_submitted(self, text: str) -> str | None:
        """Enter on the prompt: why ``text`` cannot answer it, or None once it was acted on.

        The check runs here, not in the box, so it sees the session's current
        prompt; the box shows a refusal until the text changes.
        """
        prompt = self.overlay
        assert isinstance(prompt, TextPrompt)
        error = validate_answer(prompt, text)
        if error is not None:
            return error
        self.overlay = self.submit_prompt(prompt, text)
        return None

    def picker_chosen(self, entry: PickerEntry) -> None:
        """A picker entry was chosen: run its step and show what comes next."""
        picker = self.overlay
        assert isinstance(picker, Picker)
        self.overlay = picker.back
        self.overlay = self.submit_picker(picker, entry)

    def overlay_cancel(self) -> None:
        """Esc on the open overlay: one level back, or closed."""
        overlay = self.overlay
        if isinstance(overlay, EgressState):
            self.overlay = self.egress_parent
            self.egress_parent = None
        elif isinstance(overlay, Picker):
            self.overlay = overlay.back
            self.set_notice("Cancelled")
        elif isinstance(overlay, TextPrompt):
            # Esc/Ctrl-C answer the prompt, never the dashboard.
            self.overlay = overlay.back
            self.set_notice(
                "Egress change cancelled" if overlay.purpose == "egress-add" else "Cancelled"
            )
        else:
            self.overlay = None

    def close_overlay(self) -> None:
        """Close the overlay and everything behind it (a click outside it)."""
        self.overlay = None
        self.egress_parent = None

    def overlay_global_key(self, token: str) -> Outcome:
        """A dashboard-wide key (:data:`OVERLAY_GLOBAL_TOKENS`) while a native overlay is open.

        Ctrl-C quits, `q` closes the overlay and everything behind it, `h`
        toggles help and F2/`S` settings — exactly as with a drawn overlay. A
        picker answers Ctrl-C and `q` itself (they cancel the step), so they
        never get here from one.
        """
        if token == "interrupt":
            return "quit"
        if token == "quit":
            self.close_overlay()
        elif token == "help":
            self.overlay = None if self.overlay == "help" else "help"
        elif token == "settings":
            self.overlay = (
                None if isinstance(self.overlay, SettingsState) else self.open_settings_overlay()
            )
        return None

    def menu_chosen(self, verb: str, group: str | None, index: int) -> None:
        """A menu leaf was chosen at ``group``/``index``: a panel, a question, or a verb to run."""
        menu = self.overlay
        assert isinstance(menu, (MenuState, RepoMenuState))
        # Where the menu reopens if the panel this opens is backed out of.
        parent = replace(menu, start_group=group, start_index=index)
        if isinstance(menu, RepoMenuState):
            target = menu.repo
            repo_parent = parent
            self.overlay = None
            if verb == "new":
                self.overlay = self.start_new_container()
            elif verb == "new-pr":
                self.overlay = self.start_new_container(from_pr=True)
            elif verb == "credential-group":
                self.overlay = self.open_group_picker("repo-group", target)
            elif verb == "accounts":
                self.overlay = self.load_accounts(target)
            elif verb == dact.REPO_APPLY:
                self.overlay = dact.apply_picker(target)
            elif verb == dact.REPO_DOCTOR:
                self.run_dashboard_command(target, "repo", dact.doctor_argv(), style="paged")
            elif verb == dact.REPO_DISK_USAGE:
                self.run_dashboard_command(target, "repo", dact.disk_usage_argv())
            elif verb == dact.REPO_PRUNE:
                self.run_dashboard_command(target, "repo", dact.prune_argv())
            elif verb == "fold":
                self.folded = toggle_folded(self.folded, target)
                self.shown_columns = nonempty_columns(
                    self.groups, now=_now(), enabled=self.enabled, folded=self.folded
                )
                self.save_view()
            elif verb == "net egress ls":
                self.egress_parent = repo_parent
                self.overlay = self.open_egress(target, None)
        else:
            target = menu.container
            assert isinstance(parent, MenuState)
            container_parent = parent
            self.overlay = None
            if verb == "net egress ls":
                owner = _find_group(self.groups, target)
                self.egress_parent = container_parent
                self.overlay = self.open_egress(owner.prefix, target) if owner else None
            elif verb == "credential-group":
                # Handled here: it is not a CLI verb to dispatch.
                self.overlay = self.open_group_picker("container-group", target)
            elif verb in dact.CONTAINER_VERBS:
                self.overlay = self.open_container_entry(target, verb)
            elif verb == "outbox browse":
                # Qt hands the terminal to the browser; here
                # it is the dashboard's own picker panels.
                self.overlay = self.open_outbox(target)
            elif verb == "git retarget":
                self.overlay = self.open_retarget(target)
            else:
                self.dispatch(target, verb)

    def setting_toggled(self, tab: Tab, key: str) -> None:
        """A settings row was toggled: apply it to the view and persist it."""
        overlay = self.overlay
        assert isinstance(overlay, SettingsState)
        overlay = toggle_setting(overlay, tab, key)
        self.overlay = overlay
        self.enabled = enabled_names(overlay)
        self.folded = overlay.folded
        self.show_empty_repos = overlay.show_empty_repos
        self.hidden_repos = overlay.hidden_repos
        self.groups = visible_repo_groups(
            self.all_groups,
            show_empty_repos=self.show_empty_repos,
            hidden_repos=self.hidden_repos,
        )
        self.shown_columns = nonempty_columns(
            self.groups, now=_now(), enabled=self.enabled, folded=self.folded
        )
        self.column_widths = None
        self.column_offset = 0
        self.save_view()

    def _table_key(self, key: str) -> Outcome:
        """A key with no overlay open; ``"toggle-mouse"`` asks the frontend to flip the mouse."""
        if key in ("up", "down"):
            self.move(-1 if key == "up" else 1)
        elif key in ("scroll-left", "scroll-right"):
            self.scroll_columns(1 if key == "scroll-right" else -1)
        elif key == "enter":
            self.activate()
        elif key == "help":
            self.overlay = "help"
        elif key == "command":
            self.overlay = CommandState()
        elif key == "settings":
            self.overlay = self.open_settings_overlay()
        elif key.startswith("action:"):
            container = container_of(self.selected)
            verb = quick_verb(
                self.groups,
                container,
                key,
                remote=self.remote,
                ssh_policy=self.ssh_policy,
                over_ssh=self.over_ssh,
            )
            if verb is not None and container is not None:
                self.dispatch(container, verb)
            else:
                self.set_notice(
                    quick_reject_note(
                        self.groups,
                        container,
                        key,
                        remote=self.remote,
                        ssh_policy=self.ssh_policy,
                        over_ssh=self.over_ssh,
                    )
                )
        elif key == "new":
            self.overlay = self.start_new_container()
        elif key == "accounts":
            self.overlay = self.open_accounts()
        elif key in ("config-edit", "config-edit-global") and self.remote:
            self.set_notice(REMOTE_CONFIG_EDIT_NOTE)
        elif key in ("config-edit", "config-edit-global"):
            self.edit_config(global_layer=key == "config-edit-global")
        elif key == "optimize":
            self.shown_columns = nonempty_columns(
                self.groups, now=_now(), enabled=self.enabled, folded=self.folded
            )
            self.column_widths = optimize_column_widths(
                self.groups, now=_now(), enabled=self.enabled, folded=self.folded
            )
            self.column_offset = 0
        elif key == "refresh":
            self.client.refresh()
        elif key == "details":
            self.show_details = not self.show_details
            self.save_view()
        elif key == "mouse":
            return "toggle-mouse"
        elif key == "space":
            self.toggle_fold()
        return None

    def select(self, row: Row) -> None:
        """Put the cursor on ``row`` if it is on screen."""
        if row in self.rows:
            self.selected = row
            self.sel_index = self.rows.index(row)

    def move(self, step: int) -> None:
        """Move the cursor ``step`` rows."""
        self.selected = move_selection(self.rows, self.selected, step)
        if self.selected in self.rows:
            self.sel_index = self.rows.index(self.selected)

    def scroll_columns(self, step: int) -> None:
        """Scroll the columns after the first by ``step``."""
        # Clamp before stepping too: a resize since the last frame
        # may have reduced the scrollable range.
        self.column_offset = self._clamped(self._clamped(self.column_offset) + step)

    def activate(self) -> None:
        """Enter on the selected row: its repo menu or its container menu."""
        if self.selected is not None and self.selected.kind == "repo":
            self.overlay = open_repo_menu(
                self.groups,
                self.selected.key,
                self.folded,
                ssh_policy=self.ssh_policy,
                over_ssh=self.over_ssh,
            )
        else:
            container = container_of(self.selected)
            self.overlay = open_menu(
                self.groups,
                container,
                remote=self.remote,
                ssh_policy=self.ssh_policy,
                over_ssh=self.over_ssh,
            )
            if self.overlay is None and container is not None:
                note = view_only_note(self.groups, container)
                self.set_notice(note or f"No actions available for '{container}'")

    def click(self, hit: Hit | None, *, double: bool = False, right: bool = False) -> None:
        """A click on ``hit`` (None: on nothing clickable). See the module's mouse rules."""
        overlay = self.overlay
        if overlay is not None and not (
            isinstance(overlay, (MenuState, RepoMenuState, Picker)) or overlay == "help"
        ):
            return  # a prompt, the command line, settings, egress or accounts keep the focus
        if hit is not None and hit.kind != "scroll" and not self._listed(hit):
            return  # stale: the row or repo left the listing since the frame was painted
        if overlay is not None:
            self.close_overlay()
        if hit is None:
            return
        if hit.kind == "scroll":
            if overlay is None:
                self.scroll_columns(int(hit.args[0]))
        elif hit.kind == "fold":
            if overlay is None:
                self.toggle_fold(str(hit.args[0]))
            else:
                self.select(Row("repo", str(hit.args[0])))
        else:
            self.select(Row("container" if hit.kind == "row" else "repo", str(hit.args[0])))
            if double or right:
                self.activate()

    def _listed(self, hit: Hit) -> bool:
        """Whether a row, heading or fold marker still names something on screen."""
        name = str(hit.args[0]) if hit.args else ""
        if hit.kind == "row":
            return Row("container", name) in self.rows
        return any(group.prefix == name for group in self.groups)

    def wheel_columns(self, step: int) -> None:
        """Shift+wheel or a sideways wheel over the table: columns, while no overlay is open."""
        if self.overlay is None:
            self.scroll_columns(step)

    def toggle_fold(self, prefix: str | None = None) -> None:
        """Fold or unfold ``prefix`` (default: the selected repo) and park the cursor on it."""
        prefix = prefix if prefix is not None else fold_target(self.groups, self.selected)
        if prefix is None:
            return
        self.folded = toggle_folded(self.folded, prefix)
        self.shown_columns = nonempty_columns(
            self.groups, now=_now(), enabled=self.enabled, folded=self.folded
        )
        # The container rows just vanished under the cursor; park it on the
        # header rather than letting reconcile_selection pick a neighbour repo.
        self.selected = Row("repo", prefix)
        self.save_view()
