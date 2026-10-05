"""jailbee dashboard — live, auto-refreshing cross-repo container view.

Container state is read ONLY through the :class:`Incus` wrapper. The single
``subprocess`` use is dispatching ``jailbee <subcommand>`` for the action menu —
a NON-incus subprocess (it spawns jailbee's own CLI), in the same spirit as
``gui.py`` launching GUI processes, so each action reuses the real command's
behaviour and the target repo's own config.
"""

from __future__ import annotations

import logging
import os
import select
import shlex
import shutil
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TextIO

from rich import box
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.measure import Measurement
from rich.panel import Panel
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

from jailbee import agent_status, table_format
from jailbee import dashboard_accounts as da
from jailbee import dashboard_actions as dact
from jailbee.accounts.groups import RESERVED_GROUP_NAMES
from jailbee.config import (
    DASHBOARD_DEFAULT_HIDE,
    ColumnConfig,
    format_loose_after,
    load_repo_config,
)
from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard_commands import (
    apply_completion,
    check_dashboard_command,
    command_argv,
    completion_candidates,
    dashboard_action_argv,
    insert_options_before_separator,
    permitted,
)
from jailbee.dashboard_details import (
    DETAILS_MAX_ROWS,
    DETAILS_PAIR_WIDTH,
    DetailsView,
    details_for,
    render_details,
)
from jailbee.dashboard_egress import (
    EgressState,
    egress_argv,
    move_egress,
    removable_entry,
    render_egress,
    replace_egress_rows,
)
from jailbee.dashboard_egress_data import load_egress_rows
from jailbee.dashboard_jobs import JobResult, JobRunner, needs_terminal
from jailbee.dashboard_overlays import (
    MIN_LIST_ROWS,
    PICKER_HINT,
    PROMPT_HINT,
    Picker,
    PickerEntry,
    TextPrompt,
    decode_input,
    handle_prompt_key,
    move_picker,
    parse_pr_number,
    picked,
    render_picker,
    render_prompt,
    window_lines,
)
from jailbee.dashboard_settings import (
    CURSOR_STYLE,
    SettingsState,
    enabled_names,
    move_settings,
    open_settings,
    render_settings,
    switch_tab,
    toggle_current,
)
from jailbee.dashboard_visibility import visible_repo_groups
from jailbee.db.view_prefs import ViewState, load_view_state, save_view_state
from jailbee.global_config import (
    GlobalConfig,
    default_global_config_path,
    load_global_config,
)
from jailbee.lifecycle import (
    ContainerInfo,
    agent_homes,
    annotate_activity,
    annotate_agent_status,
    format_duration_short,
    list_containers,
    ls_field_specs,
    tracking_notices,
)
from jailbee.paths import repo_config_path
from jailbee.remote_ssh import router as ssh_router
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.remote_ssh.router import RouteError
from jailbee.remote_ssh.session import host_restricted, waypipe_session
from jailbee.state_service import StateServiceUnavailable
from jailbee.tui import console, error

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from sqlalchemy.engine import Engine

    from jailbee.apps import AppSpec
    from jailbee.config import Config
    from jailbee.git_status import GitStatus
    from jailbee.incus import Incus
    from jailbee.procstat import ActivitySampler
    from jailbee.state_service.client import StateClient

log = logging.getLogger(__name__)

FieldSpecCI = table_format.FieldSpec[ContainerInfo]

NOTHING_TO_SHOW = (
    "No repos registered, and no jailbee config could be loaded for the current "
    "directory. Run `jailbee config init` here, `jailbee config validate` if a "
    "config file already exists, or start the dashboard from a registered repo."
)
"""Launch-time guard message shared by the TUI, the Qt window and `cli`.

Deliberately does not name a cause: the current directory resolves to nothing
whether it has no `.jailbee/config.yaml` and `scratch.enabled` is false, or it
is `$HOME`/the filesystem root (which scratch refuses), or its name slugifies
to nothing, or its config file exists but does not parse.

The remedy is cause-neutral for the same reason: both branches out of it work
whichever of those fired, and naming no cause must not also mean leaving the
user with no next step — which is what the earlier, single-cause wording
carried by implication and this one has to say outright.
"""

# How long a dashboard waits for its first snapshot — a cold service's first
# gather included — before giving up on the user's own terminal.
STARTUP_TIMEOUT_SECONDS = 30.0


def open_state_client(cwd_root: Path | None) -> StateClient:
    """A started `StateClient` for this dashboard (the tests' seam)."""
    from jailbee.state_service.client import StateClient

    client = StateClient(cwd_root)
    client.start()
    return client


class AppMenuEntry(NamedTuple):
    """One registry app as the action menu needs it: a dispatch verb plus
    the text to show for it.

    ``verb`` is what :func:`_dispatch_action` splits and inserts the
    container name after — see :func:`_app_menu_verb` for why it is the bare
    `AppSpec.name` for a builtin (``ide``, ``chrome``, ``firefox``: each a
    real top-level ``jailbee`` command taking the container as a plain
    positional) but ``"apps run <name> --container"`` for a config-sourced
    `apps:` entry. ``label`` is `AppSpec.description` when the repo's config
    set one (JetBrains sets ``"JetBrains idea"``, a browser sets ``"Chrome
    (host)"``); it falls back to the bare app name for a user's ``apps:``
    entry that left ``description`` empty, so the menu never renders a blank
    label.
    """

    verb: str
    label: str


def _app_menu_verb(spec: AppSpec) -> str:
    """The :func:`_dispatch_action` verb for one registry app.

    A builtin (``chrome``, ``firefox``, ``ide``) is a real top-level
    ``jailbee`` command that takes the container as a plain positional, so
    its bare name dispatches correctly, keeps ``action:ide``/``action:chrome``
    quick keys matching (:data:`KEY_BINDINGS`), and keeps `--force` working
    through the unmodified :data:`ATTACH_VERBS` check.

    A config-sourced ``apps:`` entry has no such command — dispatching its
    bare name only works at all when the entry set ``top_level: true``
    (`entry._top_level_app_names`), and even then `entry.rewrite_app_argv`
    turns ``jailbee <app> <name>`` into ``apps run <app> <name>``, where
    Ruling 24 made the container an *option* (``--container``), not a second
    positional — ``<name>`` would land in the app's own variadic ``args``
    instead of naming the container, silently launching in the default one.
    Routing explicitly through ``apps run <name> --container`` here — rather
    than through the top-level command and its rewrite — dispatches
    correctly whether or not the entry declared ``top_level``, and works for
    one that did not (which the bare-name form cannot reach at all: Typer has
    no such command to rewrite).
    """
    if spec.source == "builtin":
        return spec.name
    return f"apps run {spec.name} --container"


@dataclass
class RepoGroup:
    """One repo's containers. ``repo_root`` is None for orphan groups
    (jailbee-managed containers whose repo config could not be loaded) and is
    what distinguishes them. ``config_path`` is None for those *and* for a repo
    that has no ``.jailbee/config.yaml`` of its own, whose config is
    synthesized — so it gates nothing on its own: it says only whether a child
    is addressed with ``--config`` or by its cwd (see :class:`RepoTarget`).
    ``apps`` mirrors the repo's GUI app registry (`apps.resolve_apps`,
    builtins and `apps:` entries alike) in registry order, and drives the
    corresponding action-menu entries; orphan groups keep it empty. It
    replaces what used to be two separate booleans (``ide_enabled``,
    ``chrome_enabled``) — the registry can hold any number of apps, not just
    those two, and each carries its own display label (see
    :class:`AppMenuEntry`).
    ``loose_ttl_default`` is the repo's effective ``loose_auto_revert.after``
    as prompt-ready text — what the GUI's duration dialog pre-selects — or
    None when auto-revert is disabled, which tells the GUI not to ask at all
    (there is no TTL to schedule). Orphan groups keep None.
    ``push_action_default``/``push_source_default`` mirror the repo's effective
    ``push.default_action``/``default_source``, so a front-end can tell whether
    `jailbee git push` would stop to ask a question its own child process
    cannot answer. Orphan groups keep ``PushConfig``'s defaults.
    ``agent_homes`` are ``(container, agent, session home)`` for this group's
    containers and the repo's pooled agents (``lifecycle.agent_homes``);
    `sample_activity` matches each container against its own. Orphan groups
    keep ``()``.
    ``optional_mounts`` lists the repo config's `optional_mounts:` kinds, which
    the terminal menu's Mount…/Unmount… pickers choose from. Orphan groups keep
    it empty."""

    prefix: str
    repo_root: str | None
    config_path: Path | None
    containers: list[ContainerInfo]
    apps: list[AppMenuEntry] = field(default_factory=list)
    loose_ttl_default: str | None = None
    push_action_default: str = "ask"
    push_source_default: str = "base"
    column_notice: str | None = None
    agent_homes: tuple[tuple[str, str, Path], ...] = ()
    optional_mounts: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepoTarget:
    """How a spawned `jailbee` child is pointed at one repo.

    A configured repo is addressed with ``--config <path>``, which works from
    any cwd and is what both front-ends have always done. A repo with no config
    file has no path to point at, so it is addressed by running the child in
    the repo root and letting the ordinary cwd resolution synthesize the config
    (`config.load_repo_config`). Setting the cwd for both cases keeps one code
    path; only the flag differs, and an explicit ``--config`` wins over cwd
    resolution anyway, so a configured repo's behaviour is unchanged.
    """

    repo_root: Path
    config_path: Path | None

    @classmethod
    def of(cls, group: RepoGroup) -> RepoTarget | None:
        """The target for ``group``, or None for an orphan group (no repo root).

        This is the actionability test both front-ends gate on. It is *not*
        ``config_path is not None``: that would refuse a repo whose config was
        synthesized rather than read, which is a real repo with real containers
        and a root to run in.
        """
        if group.repo_root is None:
            return None
        return cls(Path(group.repo_root), group.config_path)

    def flags(self) -> list[str]:
        """The ``--config`` argv fragment, empty when there is no file."""
        return ["--config", str(self.config_path)] if self.config_path is not None else []

    def cwd(self) -> Path:
        """The directory to run the child in — always the repo root."""
        return self.repo_root


def registered_repo_roots(*, scope: RemoteRepoScope | None = None) -> list[Path]:
    """Repo roots for all ``RegisteredRepo`` rows whose directory still exists.

    This used to return config-file paths, which silently dropped a repo that
    has no config file — the scratch case, which is a real repo with real
    containers. A row whose directory is gone is skipped; the dashboard never
    prunes the registry (``refresh_all`` does).
    """
    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    out: list[Path] = []
    with Session(get_engine()) as session:
        for repo in session.exec(select(RegisteredRepo)).all():
            if scope is not None and not scope.allows(repo.container_prefix):
                continue
            root = Path(repo.repo_root)
            if root.is_dir():
                out.append(root)
    return out


def _dedupe_roots(candidates: Iterable[Path]) -> list[Path]:
    """Dedupe by resolved path (symlinks, relative forms), keeping the caller's
    original Path objects and their order."""
    seen: set[Path] = set()
    ordered: list[Path] = []
    for p in candidates:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        ordered.append(p)
    return ordered


def collect_repo_roots(
    cwd_root: Path | None, *, scope: RemoteRepoScope | None = None
) -> list[Path]:
    """Registered repo roots plus the cwd's, deduped, cwd first."""
    registered = registered_repo_roots() if scope is None else registered_repo_roots(scope=scope)
    return _dedupe_roots(([cwd_root] if cwd_root is not None else []) + registered)


def _loose_ttl_default(cfg: Config, gcfg: GlobalConfig) -> str | None:
    """The repo's effective loose TTL as prompt text, None when disabled."""
    policy = cfg.effective_loose_auto_revert(gcfg)
    return format_loose_after(policy.after) if policy is not None else None


def global_config_or_defaults() -> GlobalConfig:
    """Load the global config, falling back to defaults on any error.

    The dashboard is a read-only viewer refreshed on a timer; an unreadable
    or invalid ``global.yaml`` must degrade to defaults rather than abort the
    gather (the CLI, which can report and exit, is stricter). A typo'd
    column name is no longer one of those errors — `load_global_config`
    recovers from it and hands back the sanitized config (valid names
    honoured, invalid ones dropped) rather than the whole block being lost;
    this only still degrades to `GlobalConfig()` on a genuine host-level
    schema problem. The dropped names are logged rather than surfaced in
    the UI on every refresh tick; retired-column warnings are instead shown
    once at startup or on the grouped repo's status line.
    """
    try:
        gcfg, dropped = load_global_config(default_global_config_path())
    except Exception:  # ConfigError, OSError — any of them means "use defaults"
        return GlobalConfig()
    if dropped:
        log.debug("global config: %s", "; ".join(dropped))
    return gcfg


def dashboard_config_migration_notice() -> str | None:
    """Load-time migration warning for the retired global dashboard column."""
    try:
        _, warnings = load_global_config(default_global_config_path())
    except Exception:
        warnings = []  # the ordinary loader reports unrelated config failures
    retired = [warning for warning in warnings if "ahead_diff" in warning]
    return "; ".join(retired) if retired else None


def dashboard_group_notices(groups: Sequence[RepoGroup]) -> list[str]:
    """Config migration notices from the already gathered repo configurations."""
    return [f"{group.prefix}: {group.column_notice}" for group in groups if group.column_notice]


def seed_view_state(
    engine: Engine, frontend: str, *, on_migration: Callable[[str], None] | None = None
) -> ViewState:
    """``frontend``'s view state, seeding its columns on first use.

    The ``dashboard:`` config block is deprecated. It is read exactly once
    per front-end — here — so that upgrading changes nobody's columns, and is
    inert afterwards: a later edit to the YAML must not reach back into a
    front-end the user has since configured through its own UI.

    Only the **global** layer is consulted. The seeded value becomes a
    personal setting that applies in every repo, so seeding it from whichever
    repo the user happened to launch from first would let one repo's block
    silently define their view everywhere. A repo-level block is reported as
    deprecated *and* as not seeded by ``Config.validate_runtime``.

    A stored column set is filtered against :func:`all_column_names` on the
    way out, falling back to :func:`default_columns` if nothing survives —
    ``decode_names`` only validates JSON shape, not column vocabulary, so a
    renamed or removed column would otherwise reach both front-ends raw. The
    retired ``ahead_diff`` is migrated to ``target_diff`` with a visible notice
    before this filter. Each
    front-end's own last-column guard (``dashboard_settings.toggle_current``
    here, ``MainWindow._toggle_column`` in the Qt window) counts the *stored*
    length, so a phantom name inflates that count without ever being a real,
    keepable column — reaching zero real columns from a single ordinary
    toggle. Filtering here, before either guard sees the set, is what keeps
    that count honest.

    This function itself never writes: the filtered value is only returned,
    not saved back over the stored row. That does **not** mean an unknown
    name survives in storage, though — the filtered value becomes the
    long-lived ``enabled`` / ``self._enabled_columns`` each front-end holds
    for the rest of the session, and *unrelated* actions save that same
    value verbatim (folding a repo group, in both the TUI and the Qt
    window, saves a `ViewState` built from it). So the first save triggered
    by anything, not just a columns edit, drops the unknown name from
    storage for good. A column removed in one release and reintroduced in
    a later one will not come back for a user who reopens the dashboard and
    triggers any such save in between. This is accepted, not an oversight:
    preserving it would mean threading an unfiltered set through both
    front-ends' save sites, or teaching :mod:`jailbee.db.view_prefs` the
    column vocabulary it deliberately knows nothing about, for a narrow
    scenario not judged worth that machinery.
    """
    state = load_view_state(engine, frontend)
    if state.columns is not None:
        notice = stored_column_migration_notice(state.columns)
        if notice is not None and on_migration is not None:
            on_migration(notice)
        # Canonicalized *before* the filter: a stored set predating the
        # `claude_group` -> `group` rename holds a name `all_column_names` no
        # longer knows, and per this function's own contract the first save
        # after that drops it for good — so a user who had the column on would
        # silently and permanently lose it. The config-block half of the same
        # rename is handled in the loaders (`sanitize_column_blocks`); this is
        # the half that lives in the front-end's saved state instead.
        from jailbee.config.models_columns import canonical_ls_field

        known = frozenset(all_column_names())
        filtered = tuple(
            dict.fromkeys(
                c
                for n in state.columns
                if (c := "target_diff" if n == "ahead_diff" else canonical_ls_field(n)) in known
            )
        )
        return replace(state, columns=filtered or default_columns())
    gcfg = global_config_or_defaults()
    seeded = replace(state, columns=enabled_from_column_config(gcfg.dashboard))
    save_view_state(engine, frontend, seeded)
    return seeded


def stored_column_migration_notice(columns: Sequence[str] | None) -> str | None:
    """Human-facing notice before a stored view's retired column is migrated."""
    if columns is not None and "ahead_diff" in columns:
        from jailbee.config.models_columns import RETIRED_DIFF_FIELD_NOTICE

        return f"Saved dashboard column: {RETIRED_DIFF_FIELD_NOTICE}; showing target_diff instead"
    return None


def gather_rows(
    incus: Incus,
    repo_roots: list[Path],
    *,
    with_git: bool,
) -> list[RepoGroup]:
    """Build per-repo groups, then append orphan groups.

    Unscoped and unpinned: named repos first, alphabetically, orphans last.
    `present` applies a client's scope and cwd pin.

    Each root's own config drives accurate git-status/base/background-jobs.
    Repos are identified by their root, not by their config file, so a repo
    with no ``.jailbee/config.yaml`` — whose config ``load_repo_config``
    synthesizes — groups its own containers instead of falling through to the
    orphan bucket. An unloadable config is skipped (read-only — the registry
    is never pruned here). A final ``all_repos=True`` scan surfaces
    jailbee-managed containers whose repo we could not load, as view-only
    orphan groups.
    """
    from jailbee.apps import resolve_apps

    groups: list[RepoGroup] = []
    covered: set[str] = set()
    base_cfg = None
    gcfg = global_config_or_defaults()
    # One `incus list` per gather, shared by every repo and the orphan scan:
    # each listing makes the daemon build every instance's full state, and
    # the dashboards gather every few seconds. Fetched on first use, so a
    # gather with no loadable repo still never calls Incus.
    instances: list[dict[str, Any]] | None = None
    for root in repo_roots:
        try:
            cfg = load_repo_config(root)
        except Exception:  # OSError, YAML parse, Pydantic validation, no scratch
            continue
        if base_cfg is None:
            base_cfg = cfg
        if instances is None:
            instances = incus.list_containers()
        containers = list_containers(
            cfg,
            incus,
            all_repos=False,
            with_git_status=with_git,
            with_background=True,
            instances=instances,
        )
        covered.add(cfg.container_prefix)
        groups.append(
            RepoGroup(
                cfg.container_prefix,
                str(cfg.repo_root),
                repo_config_path(root),
                containers,
                apps=[
                    AppMenuEntry(_app_menu_verb(spec), spec.description or spec.name)
                    for spec in resolve_apps(cfg)
                ],
                loose_ttl_default=_loose_ttl_default(cfg, gcfg),
                push_action_default=cfg.push.default_action,
                push_source_default=cfg.push.default_source,
                column_notice="; ".join(
                    warning for warning in cfg.column_warnings() if "ahead_diff" in warning
                )
                or None,
                agent_homes=agent_homes(cfg, [c.name for c in containers]),
                optional_mounts=tuple(cfg.optional_mounts),
            )
        )

    if base_cfg is not None:
        all_rows = list_containers(
            base_cfg,
            incus,
            all_repos=True,
            with_git_status=False,
            with_background=False,
            instances=instances,
        )
        orphans: dict[str, list[ContainerInfo]] = {}
        for c in all_rows:
            # `c.repo is None` is defensive AND narrows str|None -> str for
            # the dict key below (list_containers in practice always sets it).
            if c.repo is None or c.repo in covered:
                continue
            orphans.setdefault(c.repo, []).append(c)
        for prefix in sorted(orphans):
            groups.append(RepoGroup(prefix, None, None, orphans[prefix]))

    def _sort_key(g: RepoGroup) -> tuple[bool, str]:
        # Orphan groups are the ones with no repo root — `config_path` is no
        # longer the discriminator, since a scratch repo has a root but no file.
        return (g.repo_root is None, g.prefix)

    groups.sort(key=_sort_key)
    return groups


def gather_live(incus: Incus, extra_roots: Sequence[Path], *, with_git: bool) -> list[RepoGroup]:
    """One snapshot for the state service: repo roots re-resolved per gather.

    Both dashboards refresh on a timer, and the set of registered repos moves
    underneath them: `jailbee new` registers a repo the first time it is used
    (`cli.py`), and `egress_pool.refresh_all` unregisters — then a later
    command re-registers — a repo whose config file momentarily disappeared.
    A root list captured at launch therefore goes stale, and a repo missing
    from it is not merely absent: `gather_rows`'s ``all_repos`` scan still
    finds its containers and files them under a view-only orphan group, where
    ``actions_for_container`` yields no actions and the right-click menu never
    opens. Re-resolving here is what keeps that self-healing instead of
    requiring a dashboard restart.

    The registry read is a single indexed SQLite select against a WAL
    database — cheap next to the `incus list` (and git probes) in the gather
    it precedes.

    ``extra_roots`` are the connected dashboards' cwd repos, which may not be
    registered. The result is unscoped and unpinned; each dashboard applies
    its own `present`.
    """
    return gather_rows(
        incus, _dedupe_roots([*extra_roots, *registered_repo_roots()]), with_git=with_git
    )


def present(
    groups: Sequence[RepoGroup],
    cwd_root: Path | None,
    scope: RemoteRepoScope | None = None,
) -> list[RepoGroup]:
    """A snapshot as one dashboard shows it: its scope applied, its cwd repo first.

    The state service gathers for every dashboard at once, so neither can be
    baked into the snapshot. Filtering by prefix matches what `gather_rows`
    used to do with a scope: excluded registered repos and orphan groups are
    both keyed by their container prefix.
    """
    shown = [g for g in groups if scope is None or scope.allows(g.prefix)]
    if cwd_root is not None:
        shown.sort(key=lambda g: g.repo_root != str(cwd_root))
    return shown


def carry_forward_git_status(new_groups: list[RepoGroup], prev_groups: list[RepoGroup]) -> None:
    """Copy last-known git_status into a fresh base-refresh snapshot.

    A base (non-git) gather leaves every ContainerInfo.git_status None. To
    avoid the git columns flickering blank between git-tier refreshes, fill
    each still-None git_status from the container of the same name in the
    previous snapshot (if it had one). Mutates new_groups in place.
    """
    prev_status = {
        c.name: c.git_status for g in prev_groups for c in g.containers if c.git_status is not None
    }
    for g in new_groups:
        for c in g.containers:
            if c.git_status is None and c.name in prev_status:
                c.git_status = prev_status[c.name]


def sample_activity(groups: list[RepoGroup], sampler: ActivitySampler) -> None:
    """Fill every container's CPU/DOING/AGENT fields from one sampler reading.

    One reading per screen, not one per repo group: the sampler stamps the
    elapsed time itself, so splitting a frame across several calls would
    measure several different windows.

    AGENT is then read per group from its containers' own session homes,
    from the same reading. One group's failure clears that group only.

    Shared with the Qt worker, which owns its own sampler.
    """
    annotate_activity([c for g in groups for c in g.containers], sampler)
    for g in groups:
        try:
            annotate_agent_status(g.containers, agent_status.read_sessions(g.agent_homes), sampler)
        except Exception:  # one group's reading must not end the tick for the rest
            log.debug("failed to read agent state for %s", g.prefix, exc_info=True)
            for c in g.containers:
                c.agent_status = ()


@dataclass(frozen=True)
class Row:
    """One cursor stop in the dashboard: a repo header or a container.

    Headers are selectable so a folded group can be reached and unfolded, and
    so the cursor behaves like the tree it is drawing. ``key`` is the repo
    prefix for a header and the container name for a container — the two
    namespaces are kept apart by ``kind`` rather than by a sentinel prefix,
    which would break the moment a container name looked like a repo one.
    """

    kind: Literal["repo", "container"]
    key: str


def selectable_rows(groups: list[RepoGroup], folded: frozenset[str] = frozenset()) -> list[Row]:
    """Cursor stops in display order.

    Every group contributes its header, folded or not; a folded group
    contributes none of its containers.
    """
    rows: list[Row] = []
    for g in groups:
        rows.append(Row("repo", g.prefix))
        if g.prefix in folded:
            continue
        rows += [Row("container", c.name) for c in g.containers]
    return rows


def move_selection(rows: list[Row], current: Row | None, delta: int) -> Row | None:
    """Move the highlight by ``delta`` rows, clamped at both ends."""
    if not rows:
        return None
    if current not in rows:
        return rows[0]
    idx = rows.index(current)
    return rows[max(0, min(len(rows) - 1, idx + delta))]


def reconcile_selection(rows: list[Row], current: Row | None, last_index: int) -> Row | None:
    """Keep ``current`` if still present; else clamp ``last_index`` into the
    refreshed list (nearest remaining row). None when the list is empty."""
    if not rows:
        return None
    if current in rows:
        return current
    return rows[min(last_index, len(rows) - 1)]


def container_of(row: Row | None) -> str | None:
    """The container a row acts on, or None for a header or no selection.

    Every action path (``open_menu``, ``quick_verb``, ``view_only_note``,
    ``dispatch``) takes a container name, so a header row narrows to None
    here and falls into their existing "nothing selected" handling rather
    than each of them learning about rows.
    """
    return row.key if row is not None and row.kind == "container" else None


def fold_target(groups: list[RepoGroup], row: Row | None) -> str | None:
    """The repo prefix a fold key should act on for ``row``, else None.

    Accepts either kind of row so callers can resolve a group's prefix from
    its header or one of its container rows. The live-table fold action is
    currently triggered from a repo header; settings provide the other route.
    """
    if row is None:
        return None
    if row.kind == "repo":
        return row.key
    group = _find_group(groups, row.key)
    return group.prefix if group is not None else None


def toggle_folded(folded: frozenset[str], prefix: str) -> frozenset[str]:
    """``folded`` with ``prefix`` flipped."""
    return folded - {prefix} if prefix in folded else folded | {prefix}


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

_PR_MENU_VERBS = frozenset({"pr --open", "pr", "review apply"})
_GIT_MENU_VERBS = frozenset(
    {"merge", "git pull", "git push", "git push --pr", "git retarget", "git diff"}
)
# Legacy apply leaves only ever exist with pending work: the terminal hoists
# them. "Outbox" is always offered, so `menu_actions` places it by its count.
_PENDING_APPLY_VERBS = frozenset({"review apply", "issue apply"})


def group_menu_actions(
    actions: Sequence[tuple[str, str]],
    *,
    include_network: bool = False,
    terminal_order: bool = False,
) -> list[MenuItem]:
    """Group filtered Launch, PR and Git leaves; optionally group Network for the TUI.

    Relative order within each submenu and among ungrouped leaves is retained;
    this function never changes eligibility or adds executable verbs.

    ``terminal_order`` is the terminal dashboard's presentation: pending
    apply leaves lead the menu and ``Git →`` sits above ``PR →``. It is
    opt-in because the Qt dashboard shares this function and keeps its order.
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
    if not terminal_order:
        return result
    pending = [i for i in result if isinstance(i, tuple) and i[1] in _PENDING_APPLY_VERBS]
    rest = [i for i in result if not (isinstance(i, tuple) and i[1] in _PENDING_APPLY_VERBS)]
    labels = [i.label if isinstance(i, MenuGroup) else None for i in rest]
    if "Git →" in labels and "PR →" in labels:
        git_at, pr_at = labels.index("Git →"), labels.index("PR →")
        if git_at > pr_at:
            rest[git_at], rest[pr_at] = rest[pr_at], rest[git_at]
    return [*pending, *rest]


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
    job diagnostics, PR leaves, Git leaves, network modes and lifecycle actions.
    Git pull and diff are hidden when status proves they would do nothing;
    unknown status still offers them. Stopped rows lead with Start, followed
    by eligible diagnostics and Open PR, then Destroy.

    A review container — one carrying a PR that jailbee did not open from its
    own branch (``pr_number`` set, ``pr_author`` false) — gains "Refresh from
    PR head" beside the base update: the same `git push`, sourced from the PR
    instead of the base branch. It is withheld from an authored PR, whose head
    the container's branch is upstream of, so the refresh could only be a
    no-op.

    "Outbox" (``outbox browse``) is always available on addressable running
    containers, including mount mode and unknown/empty counts. Its fixed stores
    do not require a clone or an existing PR; publication stays in the browser.
    With manifests pending in the PR or issue outbox (read from
    ``ctx.git_status``) it leads the menu and
    carries the count; otherwise it follows "Open shell".

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


def default_columns() -> tuple[str, ...]:
    """The built-in dashboard column set, in canonical field-spec order.

    What a front-end renders before anyone has touched its settings, and the
    reset target. `DASHBOARD_DEFAULT_HIDE` names the columns the dashboards
    drop from the `ls` set: REPO is redundant under per-repo grouping, the
    wide GIT STATUS combo and the JSON-only full_name add noise, and TTL is
    folded into the NETWORK cell.
    """
    specs = ls_field_specs(now=datetime.now(UTC), all_repos=False)
    return tuple(
        f.name
        for f in specs
        if table_format.shows_by_default_in_dashboard(f) and f.name not in DASHBOARD_DEFAULT_HIDE
    )


def enabled_from_column_config(columns: ColumnConfig) -> tuple[str, ...]:
    """Resolve a legacy ``dashboard:`` block into an enabled-name tuple.

    The one remaining dashboard use of ``table_format.apply_column_config``,
    confined to seeding a front-end's `view_prefs` row from the deprecated
    config block (see ``seed_view_state``). Going through the old resolver is
    what guarantees the seeded set is *exactly* what that block used to
    render, including its two quirks: an explicit ``fields`` list wins
    outright, and ``hide`` replaces the built-in list rather than extending
    it.

    That guarantee holds fully for a ``fields:`` block — naming a column
    forces ``default_dashboard=True`` on it (see
    ``table_format.apply_column_config``), overriding whatever the current
    built-in default says. It does **not** hold for a ``hide:``-shaped
    block (``fields`` empty/absent): a column *not* named in ``hide``
    passes through with its current spec unchanged, so its inclusion here
    is decided by :func:`table_format.shows_by_default_in_dashboard` as it
    stands *today* — not as it stood when the block was written. IP left
    the dashboard defaults in this same release (Part 1), so a ``hide:``
    block that never mentioned ``ip`` seeds a set without it, even though
    that block used to render IP for its user.
    """
    resolved = table_format.apply_column_config(
        ls_field_specs(now=datetime.now(UTC), all_repos=False),
        fields=columns.fields,
        hide=columns.hide,
    )
    return tuple(f.name for f in resolved if table_format.shows_by_default_in_dashboard(f))


def all_column_names() -> tuple[str, ...]:
    """Every real column name, in canonical order — the Fields tab's list.

    The same vocabulary ``jailbee ls --fields`` accepts, including columns off
    by default in both views (``full_name``, ``git_status``, ``ip``, …): an
    enabled set decides inclusion by membership, so any of them can be turned
    on. ``repo`` is redundant under per-repo grouping but is not special-cased
    — the user may want it.
    """
    return tuple(f.name for f in ls_field_specs(now=datetime.now(UTC), all_repos=False))


def dynamic_column_names() -> frozenset[str]:
    """Columns whose ``show_if`` can prune them even when enabled.

    The settings overlay marks these so that an enabled column which does not
    appear reads as the emptiness heuristic working, not as a bug.
    """
    specs = ls_field_specs(now=datetime.now(UTC), all_repos=False)
    return frozenset(f.name for f in specs if f.show_if is not None)


def settings_repo_prefixes(groups: list[RepoGroup], folded: frozenset[str]) -> tuple[str, ...]:
    """The Repos tab's list: what is on screen, plus what is folded away.

    A folded repo whose containers have since gone draws no group at all, so
    listing only ``groups`` would leave it folded forever with no way back.
    Deduped, on-screen groups first, absent folded prefixes sorted after them.

    This is a snapshot taken once, when the overlay opens (see
    ``open_settings_overlay`` in ``run()``) — a repo registered or a
    container created/destroyed while the Repos tab is open does not appear
    or disappear from the list until the overlay is closed and reopened.
    """
    on_screen = [g.prefix for g in groups]
    return tuple(dict.fromkeys(on_screen + sorted(folded)))


def visible_fields(
    now: datetime,
    all_containers: list[ContainerInfo],
    enabled: Sequence[str] | None = None,
) -> list[FieldSpecCI]:
    """The dashboard's visible columns, honouring each field's ``show_if``.

    ``enabled`` is the front-end's enabled-name set; ``None`` means
    :func:`default_columns`. Membership decides inclusion — not
    ``default_table``, which is why a column off by default everywhere can
    be turned on here — and the field-spec list's own order decides
    rendering order, so a stored list's order is not significant.

    ``show_if`` applies to every column, enabled or not. This is the
    deliberate difference from ``jailbee ls --fields``, where naming a column
    clears its ``show_if`` (see ``table_format.apply_column_config``): there,
    a name is a one-shot request; here it is a standing preference, and the
    four dynamic columns (``job``, ``ttl``, ``pr``, ``mode``) would otherwise
    render permanently empty for anyone who enabled them. The settings UI
    marks those rows so the pruning does not read as a bug.

    Unknown names are skipped rather than rejected — a stored set can outlive
    a renamed column, and view state must not break the view.

    The ``network`` field is swapped for a dashboard-specific one whose cell
    folds the loose TTL inline (e.g. ``"loose (12m)"``); that is why the
    standalone TTL column is not in the default set.

    Shared by the TUI ``render`` and both Qt views, so all three show the
    same columns for the same enabled set.
    """

    def _network_cell(c: ContainerInfo) -> str:
        if c.network != "loose":
            return c.network or "-"
        if c.loose_until is None:
            return f"{c.network} (—)"
        return f"{c.network} ({format_duration_short(c.loose_until - now)})"

    wanted = frozenset(default_columns() if enabled is None else enabled)
    fields = [
        f
        for f in ls_field_specs(now=now, all_repos=False)
        if f.name in wanted and (f.show_if is None or f.show_if(all_containers))
    ]
    return [replace(f, cell=_network_cell) if f.name == "network" else f for f in fields]


_KEY_READ_BYTES = 8  # covers all standard arrow/function-key CSI sequences
_NOTICE_SECONDS = 2.5  # how long a transient subtitle message stays up
_FAILURE_NOTICE_SECONDS = 8.0  # a refused account command's reason, long enough to read
_INLINE_NOTICE_MAX = 80  # longer notices wrap below the table instead of the border


@dataclass(frozen=True)
class KeyBinding:
    """One dashboard key: how it is typed, what it does, how it is described.

    :data:`KEY_BINDINGS` is the single source for all three — :func:`parse_key`
    is built from ``keys``, the quick-action gate from ``verb``, the help
    overlay from ``hint``/``label``/``group``. Three hand-maintained lists
    would drift.

    ``hint`` is empty for a token whose sibling documents it (``down`` is
    covered by ``up``'s "↑/↓ (j/k)"). ``brief`` is retained as optional
    concise key metadata; keys without one remain documented in the help
    overlay through their ``hint``/``label`` fields.
    """

    token: str
    keys: tuple[bytes, ...]
    hint: str
    label: str
    group: str
    verb: str | None = None
    brief: str | None = None


KEY_BINDINGS: tuple[KeyBinding, ...] = (
    KeyBinding(
        "up", (b"\x1b[A", b"k"), "↑/↓ (j/k)", "move the highlight", "Navigate", brief="move"
    ),
    KeyBinding("down", (b"\x1b[B", b"j"), "", "", "Navigate"),
    KeyBinding(
        "enter", (b"\r", b"\n"), "Enter", "open a container or repo menu (fold there)", "Navigate"
    ),
    KeyBinding(
        "cancel", (b"\x1b",), "Esc", "close a menu, panel or help; cancel a question", "Navigate"
    ),
    KeyBinding(
        "space",
        (b" ",),
        "Space",
        "fold/unfold the selected repo (Settings: toggle)",
        "Navigate",
    ),
    KeyBinding("action:tmux", (b"t",), "t", "attach tmux", "Actions", verb="tmux", brief="tmux"),
    KeyBinding(
        "action:shell", (b"s",), "s", "open a shell", "Actions", verb="shell", brief="shell"
    ),
    KeyBinding("action:ide", (b"i",), "i", "launch the IDE", "Actions", verb="ide"),
    KeyBinding("action:chrome", (b"c",), "c", "launch Chrome", "Actions", verb="chrome"),
    KeyBinding("action:pr", (b"p",), "p", "open the PR", "Actions", verb="pr --open"),
    KeyBinding("action:pr-update", (b"P",), "P", "create or update the PR", "Actions", verb="pr"),
    KeyBinding("action:push", (b"u",), "u", "update from base", "Actions", verb="git push"),
    KeyBinding("action:diff", (b"d",), "d", "show the diff", "Actions", verb="git diff"),
    # Capital, so a stray `d` (diff) can never reach it. The confirmation is the
    # CLI's own `destroy` prompt, run in the terminal exactly as the menu entry.
    KeyBinding(
        "action:destroy",
        (b"D",),
        "D",
        "destroy the container (asks to confirm)",
        "Actions",
        verb="destroy",
    ),
    # Repo-scoped, not container-scoped: no `verb`, so it never reaches
    # `quick_verb`/`actions_for_container` (those gate on a container's state).
    # `run`'s dispatch handles it directly, with its own guard.
    KeyBinding("new", (b"n",), "n", "create a container in this repo", "Actions", brief="new"),
    # Repo-scoped like `new`: no `verb`, so neither reaches `quick_verb` — the
    # config being edited belongs to the repo, not to the highlighted container.
    KeyBinding(
        "config-edit",
        (b"e",),
        "e / E",
        "edit this repo's config (E: the global one)",
        "Actions",
        brief="config",
    ),
    KeyBinding("config-edit-global", (b"E",), "", "", "Actions"),
    # Host-wide, not row-scoped: the selected row only picks which repo the
    # `jailbee account …` children are run in.
    KeyBinding(
        "accounts",
        (b"A",),
        "A",
        "credential groups and stored logins",
        "Actions",
        brief="accounts",
    ),
    KeyBinding("refresh", (b"r",), "r", "force a full refresh", "View", brief="refresh"),
    KeyBinding("details", (b"v",), "v", "show/hide the details panel", "View", brief="details"),
    KeyBinding(
        "settings",
        (b"\x1bOQ", b"\x1b[12~", b"S"),
        "F2 / S",
        "columns and repo folding",
        "View",
        brief="settings",
    ),
    KeyBinding("tab", (b"\t",), "", "", "View"),
    KeyBinding("help", (b"h", b"?"), "h / ?", "this help", "View", brief="help"),
    KeyBinding("command", (b"!",), "!", "run a jailbee command", "Actions", brief="command"),
    KeyBinding("quit", (b"q",), "q", "quit (closes an overlay first)", "View"),
    # b"" is a zero-length read: stdin hit EOF, so there is nothing left to quit to.
    KeyBinding("interrupt", (b"\x03", b""), "Ctrl-C", "quit immediately", "View"),
)

_KEY_TOKENS: dict[bytes, str] = {k: b.token for b in KEY_BINDINGS for k in b.keys}

_GATE_NOTE = (
    "Action keys only fire when that action is offered for the highlighted "
    "container: a stopped container has no tmux or shell, the IDE and Chrome "
    "need the repo's own jetbrains/chrome config, the PR key needs a known PR, "
    "the workflow keys need a running clone-mode container (and the diff key "
    "needs something to show), and orphan rows are view-only."
)


def binding_for_token(token: str) -> KeyBinding | None:
    """The binding a :func:`parse_key` token came from (None if unmapped)."""
    return next((b for b in KEY_BINDINGS if b.token == token), None)


def quick_verb(
    groups: list[RepoGroup],
    name: str | None,
    token: str,
    *,
    remote: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
) -> str | None:
    """The verb a quick-action key should dispatch for ``name``, else None.

    None covers both "not an action key" and "that action isn't offered here".
    The gate is :func:`actions_for_container`, so ``menu_actions`` stays the
    only place that decides what a container allows — a quick key can never
    reach an action its own menu would not show.
    """
    binding = binding_for_token(token)
    if binding is None or binding.verb is None:
        return None
    offered = {
        verb
        for _label, verb in actions_for_container(
            groups, name, remote=remote, ssh_policy=ssh_policy, over_ssh=over_ssh
        )
    }
    return binding.verb if binding.verb in offered else None


@dataclass
class MenuState:
    """An open action menu, rendered inline under the dashboard table.

    ``actions`` is captured when the menu opens rather than recomputed per
    frame: the dashboard keeps refreshing behind the menu, and a list that
    re-derived itself from live state would reorder rows under the cursor
    mid-keystroke. The staleness that buys is bounded — dispatching a verb
    the container has since outgrown just lets the real ``jailbee`` command
    report the problem, exactly as the previous questionary menu did.
    """

    container: str
    actions: list[tuple[str, str]]
    index: int = 0
    active_group: str | None = None
    parent_index: int = 0


@dataclass
class RepoMenuState:
    """Repo-scoped actions for a selected header, distinct from container verbs."""

    repo: str
    actions: list[MenuItem]
    index: int = 0
    active_group: str | None = None
    parent_index: int = 0


# What occupies the slot under the table. Overlays are mutually exclusive by
# construction — no combination of them is a representable state.
@dataclass(frozen=True)
class CommandState:
    """Inline command editor state, independent of terminal/input handling."""

    text: str
    suggestions: tuple[str, ...] = ()
    index: int = -1
    pending_utf8: bytes = b""


def edit_command(state: CommandState, key: bytes) -> CommandState:
    """Apply one editor key, keeping ordinary dashboard shortcuts as text."""
    if key in (b"\x7f", b"\x08"):
        if state.pending_utf8:
            return replace(state, pending_utf8=b"")
        return replace(state, text=state.text[:-1], index=-1)
    if key == b"\t":
        if not state.suggestions:
            return state
        index = (state.index + 1) % len(state.suggestions)
        return replace(
            state,
            text=apply_completion(state.text, state.suggestions[index]),
            index=index,
        )
    if key in (b"\r", b"\n", b"\x1b", b"\x03", b""):
        return state
    appended, pending = decode_input(state.pending_utf8, key)
    if not appended:
        return replace(state, pending_utf8=pending)
    return replace(state, text=state.text + appended, index=-1, pending_utf8=pending)


Overlay = (
    MenuState
    | RepoMenuState
    | EgressState
    | SettingsState
    | CommandState
    | TextPrompt
    | Picker
    | da.AccountsState
    | Literal["help"]
)


def open_menu(
    groups: list[RepoGroup],
    name: str | None,
    *,
    remote: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
) -> MenuState | None:
    """The menu for ``name``, or None when there is nothing to show.

    None covers every no-actions case — unknown container, nothing selected,
    or a view-only (orphan) group. Callers surface :func:`view_only_note`
    instead, because an empty menu frame is indistinguishable from a broken one.

    The terminal menu also offers ``Credential group…`` and the
    :mod:`jailbee.dashboard_actions` entries (autostart, snapshots, mounts),
    which the dashboard handles itself rather than dispatching. They are added
    here, not in :func:`menu_actions`, because the Qt dashboard shares that list.
    """
    actions = actions_for_container(
        groups, name, remote=remote, ssh_policy=ssh_policy, over_ssh=over_ssh
    )
    if name is None:
        return None
    group = _find_group(groups, name)
    container = (
        next((c for c in group.containers if c.name == name), None) if group is not None else None
    )
    if group is None or container is None:
        return None
    # An orphan group is view-only: no shared action and no terminal extra either.
    if not actions and RepoTarget.of(group) is None:
        return None
    extras = dact.container_extras(container, group.optional_mounts, ssh_policy, over_ssh=over_ssh)
    actions = _insert_after_job(actions, extras.after_job)
    if extras.before_network:
        actions = _insert_before_network(actions, extras.before_network)
    # Probed with placeholders: the policy judges the command, not its values.
    if permitted(["account", "group", "use", "x", "y"], ssh_policy, over_ssh=over_ssh):
        actions = _with_credential_group(actions)
    # The shared list can be empty (an SSH allowlist naming no lifecycle or
    # shell verb) while a terminal-only entry is still permitted; nothing at
    # all means no menu.
    if not actions:
        return None
    return MenuState(name, actions)


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


def open_repo_menu(
    groups: list[RepoGroup],
    prefix: str,
    folded: frozenset[str],
    *,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
) -> RepoMenuState | None:
    """Offer creation, the credential group, egress and repo-level CLI entries, folding for all.

    Everything but folding is offered for actionable repos only.

    The credential group and egress entries are hidden when the SSH policy
    refuses them, so a session never sees an entry that can only fail.
    """
    group = next((g for g in groups if g.prefix == prefix), None)
    if group is None:
        return None
    actions: list[MenuItem] = []
    if RepoTarget.of(group) is not None:
        actions.append(("New container…", "new"))
        actions.append(("New from PR…", "new-pr"))
        # Probed with a placeholder group: the policy judges the command, not its value.
        if permitted(["account", "group", "set", "x"], ssh_policy, over_ssh=over_ssh):
            actions.append(("Credential group…", "credential-group"))
        if permitted(["account", "ls"], ssh_policy, over_ssh=over_ssh):
            actions.append(("Accounts…", "accounts"))
        if permitted(["net", "egress", "ls", "--repo"], ssh_policy, over_ssh=over_ssh):
            actions.append(MenuGroup("Network →", (("Egress…", "net egress ls"),)))
        extras = dact.repo_extras(ssh_policy, over_ssh=over_ssh)
        if extras.apply is not None:
            actions.append(extras.apply)
        if extras.diagnostics:
            actions.append(MenuGroup(dact.DIAGNOSTICS_LABEL, extras.diagnostics))
        if extras.prune is not None:
            actions.append(extras.prune)
    actions.append(("Unfold" if prefix in folded else "Fold", "fold"))
    return RepoMenuState(prefix, actions)


def _menu_entries(menu: MenuState | RepoMenuState) -> Sequence[MenuItem]:
    """Visible entries at this level, derived only from captured leaves."""
    items = (
        menu.actions
        if isinstance(menu, RepoMenuState)
        else group_menu_actions(menu.actions, include_network=True, terminal_order=True)
    )
    if menu.active_group is None:
        return items
    return next(
        (
            item.actions
            for item in items
            if isinstance(item, MenuGroup) and item.label == menu.active_group
        ),
        (),
    )


def enter_menu(menu: MenuState | RepoMenuState) -> tuple[MenuState | RepoMenuState, str | None]:
    """Enter a selected group or return its selected executable verb."""
    entries = _menu_entries(menu)
    if not 0 <= menu.index < len(entries):
        return menu, None
    selected = entries[menu.index]
    if isinstance(selected, MenuGroup):
        return replace(menu, active_group=selected.label, parent_index=menu.index, index=0), None
    return menu, selected[1]


def back_menu(menu: MenuState | RepoMenuState) -> MenuState | RepoMenuState | None:
    """Go back to the highlighted parent group; close at the root."""
    if menu.active_group is None:
        return None
    return replace(menu, active_group=None, index=menu.parent_index)


def move_menu(menu: MenuState | RepoMenuState, delta: int) -> MenuState | RepoMenuState:
    """Move the cursor within the visible level, clamped at both ends."""
    last = max(0, len(_menu_entries(menu)) - 1)
    return replace(menu, index=max(0, min(last, menu.index + delta)))


def menu_verb(menu: MenuState | RepoMenuState) -> str | None:
    """Selected leaf verb, or None for a group or an empty menu."""
    entries = _menu_entries(menu)
    if not 0 <= menu.index < len(entries):
        return None
    entry = entries[menu.index]
    return None if isinstance(entry, MenuGroup) else entry[1]


def _render_menu(menu: MenuState | RepoMenuState, max_rows: int | None = None) -> RenderableType:
    """The action menu as a bordered panel: one row per action, cursor on the
    highlighted one, windowed to ``max_rows`` around the cursor."""
    lines = [
        f"[bold cyan]▸[/] [{CURSOR_STYLE}]{label}[/]" if i == menu.index else f"  {label}"
        for i, item in enumerate(_menu_entries(menu))
        for label in [item.label if isinstance(item, MenuGroup) else item[0]]
    ]
    if isinstance(menu, RepoMenuState):
        title = (
            f"{menu.repo} → {menu.active_group.removesuffix(' →')}"
            if menu.active_group
            else f"{menu.repo} →"
        )
    elif menu.active_group:
        title = f"{menu.container} → {menu.active_group.removesuffix(' →')}"
    else:
        title = f"{menu.container} →"
    return Panel(
        "\n".join(window_lines(lines, menu.index, max_rows)),
        title=f"[bold]{title}[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
        expand=False,
    )


def _render_help() -> RenderableType:
    """The keybinding help as a bordered panel, grouped as the table declares.

    Rows come from :data:`KEY_BINDINGS`, so a new key documents itself. The
    closing note explains why an action key can decline to fire — without it
    a correctly-gated key looks broken.
    """
    width = max((len(b.hint) for b in KEY_BINDINGS if b.hint), default=0)
    lines: list[str] = []
    for group in dict.fromkeys(b.group for b in KEY_BINDINGS):
        if lines:
            lines.append("")
        lines.append(f"[bold cyan]{group}[/]")
        lines += [
            f"  [bold]{b.hint:<{width}}[/]  {b.label}"
            for b in KEY_BINDINGS
            if b.group == group and b.hint
        ]
    lines += [
        "",
        "Egress panel: a adds, r removes a scoped override; Esc backs to its menu.",
        "Accounts panel: Enter acts on a login or group, n creates a group.",
        "Repo menu: Apply config…, Diagnostics →, Prune stale containers…",
        "Container menu: Snapshots…, Mount…/Unmount…, autostart status/cancel.",
        "",
        f"[dim]{_GATE_NOTE}[/dim]",
    ]
    return Panel(
        "\n".join(lines),
        title="[bold]keys[/]",
        title_align="left",
        box=box.ROUNDED,
        padding=(0, 1),
        width=72,
    )


def quick_reject_note(
    groups: list[RepoGroup],
    name: str | None,
    token: str,
    *,
    remote: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
    over_ssh: bool = False,
) -> str:
    """Why a quick-action key did nothing, as one user-facing sentence.

    A key that silently declines is indistinguishable from a broken one, and
    the reason matters: a view-only row explains itself differently from a
    stopped container or a repo with the IDE turned off — and from a remote
    session, which never launches GUI apps (see :attr:`MenuContext.remote`).
    """
    if name is None:
        return "No container is selected"
    note = view_only_note(groups, name)
    if note is not None:
        return note
    binding = binding_for_token(token)
    gui = ssh_policy is not None and ssh_policy.gui
    if remote and binding is not None and binding.verb in _GUI_VERBS and not gui:
        return "GUI apps are not available over remote SSH"
    if over_ssh and binding is not None and binding.verb is not None:
        eligible = {
            verb
            for _label, verb in actions_for_container(
                groups, name, remote=remote, ssh_policy=ssh_policy
            )
        }
        if binding.verb in eligible:
            try:
                check_dashboard_command(
                    dashboard_action_argv(
                        binding.verb,
                        name,
                        force=binding.verb in ATTACH_VERBS
                        or binding.verb.startswith(APPS_RUN_PREFIX),
                    ),
                    ssh_policy,
                    over_ssh=True,
                )
            except RouteError as exc:
                return str(exc)
    what = f"'{binding.hint}' ({binding.label})" if binding is not None else f"'{token}'"
    return f"{what} is not available for '{name}'"


def _hint_line(overlay: Overlay | None) -> str:
    """Contextual controls shown only while an overlay is open."""
    if isinstance(overlay, MenuState):
        if overlay.active_group is not None:
            return (
                "[bold]↑/↓[/bold] move  ·  [bold]Enter[/bold] run  ·  "
                "[bold]Esc[/bold] back  ·  [bold]q[/bold] close"
            )
        return "[bold]↑/↓[/bold] move  ·  [bold]Enter[/bold] open/run  ·  [bold]Esc[/bold] cancel"
    if isinstance(overlay, RepoMenuState):
        return "[bold]↑/↓[/bold] move  ·  [bold]Enter[/bold] run  ·  [bold]Esc[/bold] cancel"
    if isinstance(overlay, EgressState):
        return (
            "[bold]↑/↓[/bold] move  ·  [bold]a[/bold] add  ·  "
            "[bold]r[/bold] remove  ·  [bold]Esc[/bold] back"
        )
    if isinstance(overlay, SettingsState):
        return (
            "[bold]↑/↓[/bold] move  ·  [bold]Space[/bold] toggle  ·  "
            "[bold]Tab[/bold] switch  ·  [bold]Esc[/bold] close"
        )
    if isinstance(overlay, CommandState):
        return "[bold]Enter[/bold] run  ·  [bold]Tab[/bold] complete  ·  [bold]Esc[/bold] cancel"
    if isinstance(overlay, TextPrompt):
        return PROMPT_HINT
    if isinstance(overlay, Picker):
        return PICKER_HINT
    if isinstance(overlay, da.AccountsState):
        return da.ACCOUNTS_HINT
    if overlay is not None:  # "help"
        return "[bold]Esc[/bold] / [bold]h[/bold] close"
    return ""


def repo_heading(group: RepoGroup, selected: Row | None, folded: frozenset[str]) -> Text:
    """Render a repo heading independently of the table's data columns.

    The cursor heading is marked by :data:`CURSOR_STYLE` alone, like a
    container row; an inserted marker would shift the whole line whenever
    the cursor landed on it.
    """
    marker = "▸" if group.prefix in folded else "▾"
    label = f"{marker} {group.prefix}  ({len(group.containers)})"
    if group.repo_root is None:
        label += "  (orphan)"
    if selected == Row("repo", group.prefix):
        style = CURSOR_STYLE
    else:
        style = "bold yellow" if group.repo_root is None else "bold cyan"
    return Text(label, style=style)


def _aligned_table(
    fields: list[FieldSpecCI], widths: tuple[int, ...], *, show_header: bool
) -> Table:
    """An empty table with the dashboard's shared, fixed column geometry.

    The first title carries the same two-cell indent that
    :func:`repo_table` puts in front of every first-column cell.
    """
    table = Table(
        box=None,
        pad_edge=False,
        expand=False,
        show_edge=False,
        show_header=show_header,
        padding=(0, 1),
    )
    for index, (field_spec, width) in enumerate(zip(fields, widths, strict=True)):
        title = ("  " if index == 0 else "") + field_spec.header
        table.add_column(
            title if show_header else "",
            justify=field_spec.justify,
            width=width,
            min_width=1,
            no_wrap=False,
        )
    return table


def column_header(fields: list[FieldSpecCI], widths: tuple[int, ...]) -> Table:
    """The column titles, drawn once above every repo section."""
    return _aligned_table(fields, widths, show_header=True)


def _container_cells(
    group: RepoGroup, container: ContainerInfo, fields: list[FieldSpecCI]
) -> list[str]:
    cells: list[str] = []
    for index, field_spec in enumerate(fields):
        value = (
            container.name
            if field_spec.name == "name" and group.repo_root is None
            else field_spec.cell(container)
        )
        if index == 0:
            value = "  " + value  # indent under the repo heading
        cells.append(value)
    return cells


def repo_table(
    group: RepoGroup,
    fields: list[FieldSpecCI],
    widths: tuple[int, ...],
    selected: Row | None,
) -> Table:
    """Render one repo's rows, headerless, aligned with :func:`column_header`."""
    table = _aligned_table(fields, widths, show_header=False)
    for container in group.containers:
        is_selected = selected == Row("container", container.name)
        table.add_row(
            *_container_cells(group, container, fields),
            style=CURSOR_STYLE if is_selected else None,
        )
    return table


def container_row(
    group: RepoGroup,
    container: ContainerInfo,
    fields: list[FieldSpecCI],
    widths: tuple[int, ...],
    selected: Row | None,
) -> Table:
    """One container as a single-row table, so the table can be windowed by row."""
    return repo_table(replace(group, containers=[container]), fields, widths, selected)


@dataclass(frozen=True)
class TableWindow:
    """Rows ``[start, stop)`` are drawn; the counts feed the "more" markers."""

    start: int
    stop: int
    hidden_above: int
    hidden_below: int


def window_rows(heights: Sequence[int], cursor: int | None, budget: int) -> TableWindow:
    """The rows to draw in ``budget`` lines so that row ``cursor`` is visible.

    ``heights`` are each row's rendered line count (a wrapped row is taller
    than one). A hidden end costs one marker line. Like
    :func:`dashboard_overlays.window_lines`, the window is derived from the
    cursor alone: pinned to the top while the cursor fits there, to the
    bottom near the end, centred otherwise. A cursor row taller than the
    whole budget is still drawn; the frame clips it.
    """
    count = len(heights)
    if sum(heights) <= budget:
        return TableWindow(0, count, 0, 0)
    anchor = 0 if cursor is None else cursor

    stop, used = 0, 0
    while stop < count and used + heights[stop] <= budget - 1:
        used += heights[stop]
        stop += 1
    if anchor < stop:
        return TableWindow(0, stop, 0, count - stop)

    start, used = count, 0
    while start > 0 and used + heights[start - 1] <= budget - 1:
        start -= 1
        used += heights[start]
    if anchor >= start:
        return TableWindow(start, count, start, 0)

    inner = budget - 2
    start, stop, used = anchor, anchor + 1, heights[anchor]
    grew = True
    while grew:
        grew = False
        if stop < count and used + heights[stop] <= inner:
            used += heights[stop]
            stop += 1
            grew = True
        if start > 0 and used + heights[start - 1] <= inner:
            start -= 1
            used += heights[start]
            grew = True
    return TableWindow(start, stop, start, count - stop)


def _dashboard_column_widths(
    fields: list[FieldSpecCI], rows: list[tuple[RepoGroup, ContainerInfo]]
) -> tuple[int, ...]:
    """Measure visible headers and cells once for cross-repo consistency."""
    widths: list[int] = []
    for index, field_spec in enumerate(fields):
        values = [field_spec.header]
        for group, container in rows:
            value = (
                container.name
                if field_spec.name == "name" and group.repo_root is None
                else field_spec.cell(container)
            )
            values.append(value)
        measured = max(Text.from_markup(value).cell_len for value in values)
        widths.append(measured + (2 if index == 0 else 0))
    return tuple(widths)


def _fit_dashboard_column_widths(widths: tuple[int, ...], available_width: int) -> tuple[int, ...]:
    """Fit measured columns to Rich's current content width, retaining minima."""
    if not widths:
        return widths
    # Each table column has one cell of horizontal padding on either side.
    budget = max(len(widths), available_width - 2 * len(widths))
    if sum(widths) <= budget:
        return widths
    scale = budget / sum(widths)
    fitted = [max(1, int(width * scale)) for width in widths]
    while sum(fitted) > budget:
        largest = max(range(len(fitted)), key=fitted.__getitem__)
        if fitted[largest] == 1:
            break
        fitted[largest] -= 1
    while sum(fitted) < budget:
        smallest_ratio = min(range(len(fitted)), key=lambda i: fitted[i] / widths[i])
        fitted[smallest_ratio] += 1
    return tuple(fitted)


# First to go when space is tight. A personal hide_first list precedes this
# order; NAME is the last resort even when listed there.
_AUTO_HIDE_ORDER = (
    "full_name",
    "git_status",
    "loose_until",
    "ip",
    "doing",
    "repo",
    "created",
    "memory_limit",
    "local_diff",
    "local_count",
    "base",
    "mem",
    "cpu",
    "target_diff",
    "behind_count",
    "group",
    "issues",
    "pr",
    "ttl",
    "mode",
    "ahead_count",
    "agent_compact",
    "agent",
    "wt",
    "conflict",
    "job",
    "network",
    "state",
    "name",
)


def _fit_dashboard_fields(
    fields: list[FieldSpecCI],
    widths: tuple[int, ...],
    available_width: int,
    hide_first: Sequence[str],
) -> tuple[list[FieldSpecCI], tuple[int, ...]]:
    """Temporarily omit low-priority fields until their readable widths fit."""
    kept = list(range(len(fields)))
    priorities = tuple(dict.fromkeys((*hide_first, *_AUTO_HIDE_ORDER)))
    order = {name: index for index, name in enumerate(priorities)}

    def required_width() -> int:
        # The row indent moves to the first *remaining* column.
        indent = 2 if kept and kept[0] != 0 else 0
        return sum(widths[i] + 2 for i in kept) + indent

    while len(kept) > 1 and required_width() > available_width:
        discard = min(
            kept,
            key=lambda i: (
                1 if fields[i].name == "name" else 0,
                order.get(fields[i].name, len(order)),
                -widths[i],
            ),
        )
        kept.remove(discard)
    fitted = tuple(widths[i] + (2 if pos == 0 and i != 0 else 0) for pos, i in enumerate(kept))
    return [fields[i] for i in kept], fitted


@dataclass(frozen=True)
class _RepoSections:
    groups: list[RepoGroup]
    fields: list[FieldSpecCI]
    widths: tuple[int, ...]
    selected: Row | None
    folded: frozenset[str]
    empty: bool
    hidden_by_preferences: bool = False
    hide_first: Sequence[str] = ()
    max_rows: int | None = None
    """Line budget including the column header and the "more" markers; None draws every row."""

    def line_count_floor(self) -> int:
        """A lower bound on the drawn line count, without rendering anything.

        One line per heading and container row, plus the column header; a
        wrapped row draws more, so the true count is never smaller.
        """
        if self.empty:
            return 1
        expanded = [g for g in self.groups if g.containers and g.prefix not in self.folded]
        return (1 if expanded else 0) + len(self.groups) + sum(len(g.containers) for g in expanded)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        fields, measured = _fit_dashboard_fields(
            self.fields, self.widths, options.max_width, self.hide_first
        )
        widths = _fit_dashboard_column_widths(measured, options.max_width)
        if self.empty:
            yield (
                "All repositories are hidden — open Settings > Visibility to show them"
                if self.hidden_by_preferences
                else "(no containers found)"
            )
            return
        expanded = {g.prefix for g in self.groups if g.containers and g.prefix not in self.folded}
        header = column_header(fields, widths) if expanded else None
        blocks: list[tuple[Row, RenderableType]] = []
        for group in self.groups:
            blocks.append(
                (Row("repo", group.prefix), repo_heading(group, self.selected, self.folded))
            )
            if group.prefix in expanded:
                blocks += [
                    (
                        Row("container", c.name),
                        container_row(group, c, fields, widths, self.selected),
                    )
                    for c in group.containers
                ]
        if self.max_rows is None:
            yield Group(*([header] if header is not None else []), *(b for _, b in blocks))
            return
        free = options.update(height=None)
        head = console.render_lines(header, free, pad=False) if header is not None else []
        rendered = [console.render_lines(b, free, pad=False) for _, b in blocks]
        rows = [row for row, _ in blocks]
        cursor = rows.index(self.selected) if self.selected in rows else None
        window = window_rows([len(r) for r in rendered], cursor, max(1, self.max_rows - len(head)))
        lines = list(head)
        if window.hidden_above:
            lines += console.render_lines(
                Text(f"  ↑ {window.hidden_above} more", style="dim"), free, pad=False
            )
        for block in rendered[window.start : window.stop]:
            lines += block
        if window.hidden_below:
            lines += console.render_lines(
                Text(f"  ↓ {window.hidden_below} more", style="dim"), free, pad=False
            )
        if len(lines) > self.max_rows and cursor is not None:
            # A cursor row taller than the budget overran its window: show that
            # row alone (its top lines) rather than let the frame cut it.
            lines = [*head, *rendered[cursor]][: self.max_rows]
        for index, line in enumerate(lines):
            if index:
                yield Segment.line()
            yield from line


def _render_overlay(overlay: Overlay, max_rows: int | None = None) -> RenderableType:
    """The overlay's panel; ``max_rows`` windows the scrollable list overlays."""
    if isinstance(overlay, EgressState):
        return render_egress(
            overlay,
            can_add=overlay.can_add,
            can_rm=overlay.can_rm and removable_entry(overlay) is not None,
        )
    if isinstance(overlay, (MenuState, RepoMenuState)):
        return _render_menu(overlay, max_rows)
    if isinstance(overlay, CommandState):
        lines = [f"> {overlay.text}▏"]
        if overlay.suggestions:
            lines.append("  " + "   ".join(overlay.suggestions))
        return Panel("\n".join(lines), title="command", box=box.ROUNDED, expand=False)
    if isinstance(overlay, SettingsState):
        return render_settings(overlay, dynamic=dynamic_column_names())
    if isinstance(overlay, TextPrompt):
        return render_prompt(overlay)
    if isinstance(overlay, Picker):
        return render_picker(overlay, max_rows)
    if isinstance(overlay, da.AccountsState):
        return da.render_accounts(overlay)
    return _render_help()


# Panel border rows: around a windowed overlay's list, and around the frame.
_OVERLAY_BORDER_ROWS = 2
_FRAME_BORDER_ROWS = 2
# Content rows a details panel needs to say anything; with fewer it is left out.
_MIN_DETAILS_ROWS = 2
# Table rows (column header not counted) kept on screen under the bottom area.
MIN_TABLE_ROWS = 5


@dataclass(frozen=True)
class _FrameBody:
    """The dashboard body, fitted to the screen.

    Top to bottom: the table window, the inline notice, a blank gap, the
    bottom area, then the hint. The bottom area is the details panel, an
    overlay, or — for an action menu — the details with the menu on the right.

    Without ``max_height`` (a plain ``console.print``) everything is drawn
    whole. Under the full-screen ``Live`` anything past the terminal's height
    would be clipped by the screen, so the table keeps :data:`MIN_TABLE_ROWS`
    before the bottom area may grow; a menu or picker is windowed to what is
    left, never below :data:`MIN_LIST_ROWS`; if even that does not fit, lines
    are dropped from the top so the bottom border and hint stay.

    ``max_height`` is passed in rather than read from ``options.height``:
    Rich's ``Screen`` wraps its renderable in a ``Group``, which resets the
    height before anything below it renders.
    """

    sections: _RepoSections
    notice: RenderableType | None
    overlay: Overlay | None
    details: DetailsView | None = None
    max_height: int | None = None

    def _bottom(
        self, list_rows: int | None, details_rows: int | None, *, fixed: bool = False
    ) -> RenderableType | None:
        """Details, an overlay, or both side by side with the menu on the right.

        ``details_rows`` caps the panel's content rows; None leaves the panel
        out. ``fixed`` pads it to exactly that many, so it keeps one shape.

        Only the action menus share the row: every other overlay is a task of
        its own (a picker, a prompt, a settings page) and gets the full width.
        """
        menu = isinstance(self.overlay, (MenuState, RepoMenuState))
        details = (
            render_details(self.details, details_rows, fixed=fixed)
            if self.details is not None
            and details_rows is not None
            and (self.overlay is None or menu)
            else None
        )
        if self.overlay is None:
            return details
        panel = _render_overlay(self.overlay, list_rows)
        if details is None:
            return panel
        row = Table.grid(expand=True)
        row.add_column(ratio=1)
        row.add_column()
        row.add_row(details, panel)
        return row

    def _details_fit_beside_menu(self, console: Console, options: ConsoleOptions) -> bool:
        """Whether the details keep a column wide enough to read next to a menu."""
        if not isinstance(self.overlay, (MenuState, RepoMenuState)):
            return True
        menu = Measurement.get(console, options, _render_overlay(self.overlay)).maximum
        return options.max_width - menu >= DETAILS_PAIR_WIDTH

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        hint = _hint_line(self.overlay) if self.overlay is not None else None
        extras: list[RenderableType] = [] if self.notice is None else [self.notice]
        details_fit = self._details_fit_beside_menu(console, options)
        if self.max_height is None:
            bottom = self._bottom(None, DETAILS_MAX_ROWS if details_fit else None)
            tail: list[RenderableType] = [] if bottom is None else ["", bottom]
            if hint is not None:
                tail.append(hint)
            yield Group(self.sections, *extras, *tail)
            return

        free = options.update(height=None)

        def lines_of(renderable: RenderableType) -> list[list[Segment]]:
            return console.render_lines(renderable, free, pad=False)

        notice_lines = lines_of(Group(*extras)) if extras else []
        hint_lines = lines_of(hint) if hint is not None else []
        rest = self.max_height - len(notice_lines) - len(hint_lines)
        has_bottom = self.overlay is not None or self.details is not None
        gap_lines = lines_of(Text("")) if has_bottom else []
        bottom_lines: list[list[Segment]] = []
        if has_bottom:
            # The table's line count is only needed against thresholds, and a
            # table with more rows than the floor has at least that many lines.
            count = self.sections.line_count_floor()
            full = None if count > MIN_TABLE_ROWS + 1 else len(lines_of(self.sections))
            natural = count if full is None else full
            floor = min(natural, MIN_TABLE_ROWS + 1)  # + the column header
            room = max(0, rest - len(gap_lines) - floor) - _OVERLAY_BORDER_ROWS
            # A panel with fewer than two content rows says nothing: leave it out.
            details_rows = min(DETAILS_MAX_ROWS, room)
            with_details = details_fit and details_rows >= _MIN_DETAILS_ROWS
            # When the table overflows it is the panel that must keep its shape:
            # a panel as tall as its content would resize the table window as
            # the cursor moves between a repo heading and a container.
            fixed = False
            if with_details:
                budget = rest - len(gap_lines) - (details_rows + _OVERLAY_BORDER_ROWS)
                if full is None and count <= budget:
                    full = len(lines_of(self.sections))
                fixed = count > budget or (full or 0) > budget
            bottom = self._bottom(
                max(room, MIN_LIST_ROWS), details_rows if with_details else None, fixed=fixed
            )
            if bottom is not None:
                bottom_lines = lines_of(bottom)
            else:
                gap_lines = []
        table_rows = max(0, rest - len(gap_lines) - len(bottom_lines))
        table_lines = lines_of(replace(self.sections, max_rows=table_rows))
        out = [*table_lines, *notice_lines, *gap_lines, *bottom_lines, *hint_lines]
        if len(out) > self.max_height:
            # Even the minimum bottom area does not fit: lose the top, never
            # the hint or the frame's bottom border.
            out = out[len(out) - self.max_height :]
        for index, line in enumerate(out):
            if index:
                yield Segment.line()
            yield from line


def render(
    groups: list[RepoGroup],
    selected: Row | None,
    *,
    now: datetime,
    git_enabled: bool,
    enabled: Sequence[str] | None = None,
    overlay: Overlay | None = None,
    notice: str | None = None,
    folded: frozenset[str] = frozenset(),
    hide_first: Sequence[str] = (),
    hidden_by_preferences: bool = False,
    height: int | None = None,
    show_details: bool = False,
) -> RenderableType:
    """Build the Rich renderable for one dashboard frame.

    Repo sections are rendered in the dashboard body with aligned columns.
    The selected row, heading or container, is marked by its
    :data:`CURSOR_STYLE` highlight alone. Wrapped in a rounded Panel whose
    left-aligned title carries the summary and the clock; the subtitle carries
    a short transient notice and nothing else.

    ``overlay`` is an open action menu or the keybinding help, drawn *below*
    the table so the dashboard it acts on stays on screen. When the frame is
    taller than the terminal the table scrolls to its cursor (keeping at least
    :data:`MIN_TABLE_ROWS` rows) and a menu or picker scrolls with its own
    (see :class:`_FrameBody`). ``height`` is the terminal's height; None draws
    everything whole.

    ``show_details`` draws the details panel for ``selected`` under the table
    — the dashboard's `v` toggle; off by default so plain renders are
    unchanged. An action menu then sits to the right of it; every other
    overlay hides it.

    ``notice`` is a transient message (a rejected key, a view-only row) shown
    in the subtitle, or — longer than :data:`_INLINE_NOTICE_MAX` — wrapped
    right below the table.
    """
    all_containers = [c for g in groups for c in g.containers]
    visible = [c for g in groups if g.prefix not in folded for c in g.containers]
    fields = visible_fields(now, visible, enabled)

    visible_groups = groups
    visible_rows = [(g, c) for g in visible_groups if g.prefix not in folded for c in g.containers]
    widths = _dashboard_column_widths(fields, visible_rows)
    # A notice too long for the bottom border is drawn whole, wrapped, right
    # below the table: a CLI refusal ends in its remedy ("… pass --force"),
    # which an ellipsis on the border would cut. A plain `Text`, not markup: a
    # CLI message may contain `[...]`.
    inline_notice = notice if notice and len(notice) > _INLINE_NOTICE_MAX else None
    sections = _RepoSections(
        visible_groups,
        fields,
        widths,
        selected,
        folded,
        empty=not groups,
        hidden_by_preferences=hidden_by_preferences,
        hide_first=hide_first,
    )
    details = details_for(visible_groups, selected, now) if show_details and groups else None
    n_repos = len({g.prefix for g in groups})
    n_ctr = len(all_containers)
    n_folded = len({g.prefix for g in groups if g.prefix in folded and g.containers})
    folded_note = f" · {n_folded} folded" if n_folded else ""
    git_note = "" if git_enabled else "  ·  [dim](no-git)[/dim]"
    title = (
        f"[bold]🐝 jailbee dashboard[/]  ·  [dim]h/? help[/]"
        f"  ·  {n_repos} repos · {n_ctr} containers{folded_note}{git_note}  ·  {now:%H:%M:%S}"
    )
    # Subtitle is notice-only: a short transient message on the bottom border
    # cannot push the table around. Should the terminal still be narrower than
    # a short notice, it is cut on the right so its start (the verdict) stays.
    subtitle = (
        Text(notice, style="yellow", no_wrap=True, overflow="ellipsis")
        if notice and inline_notice is None
        else None
    )
    return Panel(
        _FrameBody(
            sections,
            Text(inline_notice, style="yellow") if inline_notice is not None else None,
            overlay,
            details,
            None if height is None else max(0, height - _FRAME_BORDER_ROWS),
        ),
        title=title,
        title_align="left",
        subtitle=subtitle,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def parse_key(data: bytes) -> str:
    """Map a raw stdin read to a dashboard key token ('' if unmapped).

    A pure lookup into :data:`KEY_BINDINGS`, so a key cannot be readable
    without also being documented in the help overlay.

    Note the three ways out: ``cancel`` (bare Esc — arrows arrive as
    ``\\x1b[…``) and ``quit`` (``q``) close an open overlay first, while
    ``interrupt`` (Ctrl-C, EOF) always ends the dashboard. Folding them into
    one token would leave Ctrl-C unable to do anything but shut the menu.
    """
    return _KEY_TOKENS.get(data, "")


def _find_group(groups: list[RepoGroup], name: str | None) -> RepoGroup | None:
    for g in groups:
        for c in g.containers:
            if c.name == name:
                return g
    return None


def prompt_target_kind(purpose: str) -> Literal["repo", "container"]:
    """What a prompt's or picker's ``target`` names, read from its ``purpose``.

    Only the ``container-*`` questions are about a container; every other one
    targets a repo prefix. A container may share its name with another repo's
    prefix (container ``alpha-x`` of repo ``alpha`` beside repo ``alpha-x``),
    so a target is never looked up as both.
    """
    return "container" if purpose.startswith("container-") else "repo"


def target_group(
    groups: list[RepoGroup], target: str, kind: Literal["repo", "container"]
) -> RepoGroup | None:
    """The listed repo that ``target`` — a prefix or a container name — belongs to."""
    if kind == "container":
        return _find_group(groups, target)
    return next((g for g in groups if g.prefix == target), None)


_TERMINAL_TITLE_FALLBACK = "🐝 jailbee"


def terminal_title(groups: list[RepoGroup], selected: Row | None) -> str:
    """The xterm/tmux window title for the current selection.

    ``🐝 <repo>/<container>`` on a container row, ``🐝 <repo>`` on a repo
    header, and the bare tool name when nothing is selected or the selected
    container has vanished under the cursor. An orphan group's container shows
    its *full* name, matching the NAME column — there is no known repo prefix
    to have stripped.
    """
    if selected is None:
        return _TERMINAL_TITLE_FALLBACK
    if selected.kind == "repo":
        return f"🐝 {selected.key}"
    group = _find_group(groups, selected.key)
    if group is None:
        return _TERMINAL_TITLE_FALLBACK
    container = next((c for c in group.containers if c.name == selected.key), None)
    if container is None:
        return _TERMINAL_TITLE_FALLBACK
    name = container.name if group.repo_root is None else container.display_name
    return f"🐝 {group.prefix}/{name}"


def set_terminal_title(text: str, *, stream: TextIO) -> None:
    """Write one OSC 2 window-title sequence.

    Best-effort: a terminal that does not implement it drops the sequence
    silently, so there is nothing to detect or guard against.
    """
    stream.write(f"\x1b]2;{text}\x07")
    stream.flush()


@contextmanager
def terminal_title_scope(stream: TextIO) -> Iterator[None]:
    """Save the terminal's own title on entry, restore it on exit.

    Uses the xterm title stack (``CSI 22;2t`` / ``CSI 23;2t``), implemented by
    xterm and tmux and ignored elsewhere. Without the pop the terminal would
    keep jailbee's title after the dashboard quits, since there is no way to
    read the old one back.
    """
    stream.write("\x1b[22;2t")
    stream.flush()
    try:
        yield
    finally:
        stream.write("\x1b[23;2t")
        stream.flush()


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

# The printing verbs long enough to want a pager instead of a pause. `Live`
# repaints the moment the dashboard resumes, so the rest get the pause: without
# one their output is gone before it can be read.
_PAGED_VERBS: frozenset[str] = frozenset({"git diff"})
_OUTPUT_VERBS: frozenset[str] = PRINTING_VERBS - _PAGED_VERBS

DispatchStyle = Literal["paged", "output", "plain"]


def dispatch_style(verb: str) -> DispatchStyle:
    """How the TUI should run ``verb``: through a pager, with a pause, or bare."""
    if verb in _PAGED_VERBS:
        return "paged"
    if verb in _OUTPUT_VERBS:
        return "output"
    return "plain"


def command_needs_pause(typed: str) -> bool:
    """Retain output for noninteractive commands entered in the editor."""
    return typed not in ATTACH_VERBS and not typed.startswith(APPS_RUN_PREFIX)


def pager_argv() -> list[str] | None:
    """The pager to page long output through, or None when the host has none.

    ``$PAGER`` wins (split as a shell word list, so ``PAGER="bat -p"`` works);
    otherwise ``less -R``, which renders the ANSI colour the diff is asked to
    emit, then ``more``.
    """
    env = os.environ.get("PAGER")
    if env:
        return shlex.split(env)
    for candidate in (["less", "-R"], ["more"]):
        if shutil.which(candidate[0]):
            return candidate
    return None


def _egress_panel(overlay: Overlay | None) -> EgressState | None:
    """The Egress panel on screen: itself, or the one behind its question."""
    if isinstance(overlay, EgressState):
        return overlay
    if isinstance(overlay, (TextPrompt, Picker)) and isinstance(overlay.back, EgressState):
        return overlay.back
    return None


def _with_egress_panel(overlay: Overlay | None, panel: EgressState) -> Overlay | None:
    """``overlay`` with the Egress panel it shows (or sits over) swapped for ``panel``."""
    if isinstance(overlay, EgressState):
        return panel
    if isinstance(overlay, (TextPrompt, Picker)):
        return replace(overlay, back=panel)
    return overlay


def _wait_for_return() -> None:
    """Hold the terminal until the user has read the output.

    Called only from inside ``run``'s ``foreground`` helper, where ``Live`` is
    stopped and the terminal is back in cooked mode — so a plain read is
    enough. EOF (piped stdin, Ctrl-D) and Ctrl-C return immediately rather
    than propagating: neither is a reason to take the dashboard down.
    """
    console.print("\n[dim]── press Enter to return to the dashboard ──[/dim]")
    try:
        sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        pass


class _PagerUnavailableError(OSError):
    """The pager process itself could not be started.

    Raised only from the viewer ``Popen`` below — never from the producer's —
    so a caller catching this specifically (rather than bare ``OSError``) can
    tell "the pager is missing" apart from "the producer couldn't even start"
    (e.g. its ``cwd`` has vanished) and fall back to the unpaged path only for
    the former. A bare ``OSError`` out of this function is always the
    producer's.
    """


def _run_paged(argv: list[str], pager: list[str], cwd: Path) -> int:
    """Pipe ``argv``'s stdout into ``pager``; return the command's exit code.

    Two processes rather than a shell string, so there is no quoting to get
    wrong. Stderr stays attached to the terminal: an error message must not be
    swallowed by the pager. The pager owns the terminal until the user quits
    it, which is why the paged path needs no keypress pause of its own.

    ``cwd`` applies to the producer only — it is how a repo with no config file
    is addressed at all (see :class:`RepoTarget`). The pager is a plain viewer
    on a pipe and has no repo of its own.

    Raises a bare ``OSError`` (uncaught here) if the producer itself cannot be
    started — most notably a ``cwd`` that has disappeared out from under a
    dispatch — and :class:`_PagerUnavailableError` if the pager cannot be started,
    so callers do not conflate the two.
    """
    producer = subprocess.Popen(argv, stdout=subprocess.PIPE, cwd=cwd)
    out = producer.stdout
    if out is None:  # unreachable with stdout=PIPE; keeps mypy honest
        return producer.wait()
    try:
        viewer = subprocess.Popen(pager, stdin=out)
    except OSError as exc:
        # The pager vanished between which() and exec. Nothing will ever read
        # the pipe, so kill the producer rather than leaving it blocked on a
        # full one, and let the caller fall back to the unpaged path.
        producer.kill()
        producer.wait()
        raise _PagerUnavailableError(str(exc)) from exc
    finally:
        # The viewer owns the read end now. Keeping this copy open would stop
        # the pager ever seeing EOF, so it would hang on a finished command.
        out.close()
    viewer.wait()
    return producer.wait()


def _dispatch_action(
    target: RepoTarget,
    verb: str,
    name: str,
    *,
    remote: bool = False,
    over_ssh: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
) -> int:
    """Run ``jailbee <verb> <name>`` against ``target``; return its exit code.

    The single dispatch point shared by the inline action menu and the
    quick-action keys, so both reuse the real command's behaviour and the
    target repo's own config. ``verb`` may be multi-token (``"net loose"``,
    ``"pr --open"``, ``"job log --follow"``).

    Every child runs in ``target.cwd()``, and a configured repo additionally
    gets ``--config``: a repo with no config file has no path to pass, so the
    working directory is the only thing that says which repo this is.

    Verbs in :data:`ATTACH_VERBS`, and any verb `_app_menu_verb` composed
    with the :data:`APPS_RUN_PREFIX` (a config-sourced `apps:` entry), gain
    ``--force``; ``--force`` means something different on every other
    command (and most don't accept it), so nothing else gets it.

    The verb's :func:`dispatch_style` decides what happens to its output: a
    pager for the diff (with ``--color`` forced, because the pipe would
    otherwise turn colour off), a keypress pause for the other printing verbs,
    and nothing at all for the rest. A missing or unstartable pager degrades to
    the pause rather than losing the output.

    A remote session (``remote``) never gets a pager: every pager worth the
    name can run commands (`less`'s ``!``, ``v`` and ``|``, `more`'s ``!``),
    and here they would run on the host. The paged verbs fall back to the
    keypress pause instead, and the client's own scrollback does the paging.

    Raises ``OSError`` (uncaught here) if ``target.cwd()`` has disappeared out
    from under the dispatch — the caller (:func:`run`'s ``dispatch``) turns
    that into a notice naming the directory rather than letting it take the
    whole TUI down. That is deliberately *not* caught as "pager failed": see
    :class:`_PagerUnavailableError`.
    """
    action_argv = dashboard_action_argv(
        verb, name, force=verb in ATTACH_VERBS or verb.startswith(APPS_RUN_PREFIX)
    )
    check_dashboard_command(action_argv, ssh_policy, over_ssh=over_ssh)
    argv = ["jailbee", *verb.split(), name, *(target.flags() if not over_ssh else [])]
    if verb in ATTACH_VERBS or verb.startswith(APPS_RUN_PREFIX):
        argv.append("--force")
    style = dispatch_style(verb)
    if (
        over_ssh
        and ssh_policy is not None
        and ssh_policy.gui
        and _is_gui_verb(verb)
        and waypipe_session() is None
    ):
        # The launch prints how to reach the shared RDP display; "plain" would
        # throw that away the moment the dashboard repaints. A waypipe session
        # has nothing to show: the window simply opens on the laptop.
        style = "output"
    if style == "paged" and remote:
        style = "output"
    if style == "paged":
        pager = pager_argv()
        if pager is not None:
            try:
                return _run_paged([*argv, "--color"], pager, target.cwd())
            except _PagerUnavailableError as exc:
                log.debug("pager %s failed: %s", pager, exc)
    rc = subprocess.run(argv, check=False, cwd=target.cwd()).returncode
    if style != "plain":
        _wait_for_return()
    return rc


def _run_cli_foreground(
    target: RepoTarget,
    argv: list[str],
    *,
    style: DispatchStyle,
    remote: bool = False,
    over_ssh: bool = False,
    ssh_policy: RemoteSSHConfig | None = None,
) -> int:
    """Run a dashboard-built ``jailbee <argv>`` against ``target``; return its exit code.

    The counterpart of :func:`_dispatch_action` for entries that are not
    ``<verb> <container>``: repo-level commands (``apply``, ``doctor``) and argv
    carrying a ``--``-guarded answer (``snapshot create -- NAME TAG``).
    ``_dispatch_action`` is deliberately not rebuilt on top of this. Its argv
    order (``--force`` before ``--config``) is pinned by many tests, and nothing
    would change for the user.

    ``argv`` is checked exactly as given, then addressed: ``--config`` goes
    before any ``--``, and nothing is added over SSH (`dashboard_actions.addressed`).
    ``style`` works as in :func:`_dispatch_action`. A remote session never gets
    a pager, because a pager can run host commands, so it gets the pause. A
    pager that cannot start also degrades to the pause.

    Raises :class:`RouteError` before anything runs when the policy refuses
    ``argv``, and ``OSError`` when ``target.cwd()`` has vanished (the caller
    turns that into a notice).
    """
    check_dashboard_command(argv, ssh_policy, over_ssh=over_ssh)
    full = ["jailbee", *dact.addressed(argv, target.flags(), over_ssh=over_ssh)]
    if style == "paged" and (remote or over_ssh):
        style = "output"
    if style == "paged":
        pager = pager_argv()
        if pager is not None:
            try:
                return _run_paged(full, pager, target.cwd())
            except _PagerUnavailableError as exc:
                log.debug("pager %s failed: %s", pager, exc)
    rc = subprocess.run(full, check=False, cwd=target.cwd()).returncode
    if style != "plain":
        _wait_for_return()
    return rc


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
    hide_first = tuple(global_config_or_defaults().dashboard.auto_hide.hide_first)

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

            def dispatch(target: str, verb: str) -> None:
                nonlocal notice, notice_until
                group = _find_group(groups, target)
                if group is None:
                    return
                repo = RepoTarget.of(group)
                if repo is None:
                    return  # an orphan group: no repo root to address a child at
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
                    return
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
                    )
                if prompt.purpose == "new-base":
                    branch = prompt.carry[0]

                    def branch_argv(repo: RepoTarget) -> list[str]:
                        if over_ssh:
                            return ["jailbee", "new", "--background", "--", branch, answer]
                        return new_container_argv(repo, branch, answer)

                    run_new_container(prompt.target, branch, branch_argv)
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
                        hide_first=hide_first,
                        hidden_by_preferences=bool(all_groups) and not groups,
                        height=console.height,
                        show_details=show_details,
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
                        elif key == "enter":
                            next_menu, verb = enter_menu(overlay)
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
                                else:
                                    dispatch(target, verb)
                    continue
                if key == "quit":
                    break
                if key in ("up", "down"):
                    selected = move_selection(rows, selected, -1 if key == "up" else 1)
                    if selected in rows:
                        sel_index = rows.index(selected)
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
